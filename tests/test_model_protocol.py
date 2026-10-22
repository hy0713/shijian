"""HTTP protocol fixture for the observed DeepSeek thinking/tool-choice incompatibility."""

import json

import httpx
import pytest
from langchain_core.messages import HumanMessage

from shijian import model as model_module
from shijian.config import Settings
from shijian.schema import Analysis


@pytest.mark.parametrize(
    "first_response", ["valid", "invalid_fields", "wrong_tool", "always_invalid", "truncated"]
)
async def test_structured_output_disables_thinking_in_actual_request(monkeypatch, first_response):
    captured = []
    expected = {
        "premise_status": "待核查",
        "premise_to_check": ["人物"],
        "subquestions": ["时序"],
        "search_queries": ["史思明"],
        "query_plan": [],
    }

    def respond(request):
        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "TEST-ONLY",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "length"
                        if first_response == "truncated" and len(captured) == 1
                        else "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "test-call",
                                    "type": "function",
                                    "function": {
                                        "name": "WrongTool"
                                        if first_response == "wrong_tool" and len(captured) == 1
                                        else "Analysis",
                                        "arguments": '{"premise_status":'
                                        if first_response == "truncated" and len(captured) == 1
                                        else json.dumps(
                                            {**expected, "premise_status": "INVALID"}
                                            if first_response == "always_invalid"
                                            or (first_response == "invalid_fields" and len(captured) == 1)
                                            else expected
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    base_client = httpx.AsyncClient

    class MockClient(base_client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(model_module.httpx, "AsyncClient", MockClient)
    config = Settings(
        _env_file=None,
        deepseek_api_key="TEST-ONLY",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="TEST-ONLY",
    )
    model = model_module.ResearchModel(config)
    try:
        if first_response == "always_invalid":
            with pytest.raises(ValueError, match="一次格式修正"):
                await model.structured(Analysis, "TEST-ONLY", "TEST-ONLY")
            assert len(captured) == 2 and model.format_retries == 1
            return
        actual = await model.structured(Analysis, "TEST-ONLY", "TEST-ONLY")
        assert actual.model_dump() == expected
        if first_response == "truncated":
            assert model.structured_failures[0]["reason"] == "truncated"
        if first_response == "invalid_fields":
            assert model.structured_failures[0]["fields"][0]["field"] == ["premise_status"]
        assert captured[0]["thinking"] == {"type": "disabled"}
        assert captured[0]["tool_choice"]["function"]["name"] == "Analysis"
        await model.choose([HumanMessage(content="TEST-ONLY")], [Analysis])
        assert captured[-1]["tool_choice"] == "required"
        assert captured[-1]["thinking"] == {"type": "disabled"}
        assert [r["stage"] for r in model.request_metrics] == (
            ["Analysis", "choose"]
            if first_response == "valid"
            else ["Analysis", "Analysis.format_retry", "choose"]
        )
        assert sum(r["total_tokens"] for r in model.request_metrics) == len(captured) * 20
        assert all(r["context_chars"] > 0 for r in model.request_metrics)
        assert all(r["input_tokens"] == r["output_tokens"] == 10 for r in model.request_metrics)
        assert model.request_metrics[-1]["observed_cumulative_tokens"] == len(captured) * 20
    finally:
        await model.aclose()


@pytest.mark.parametrize("limit", ["requests", "tokens"])
async def test_budget_stops_before_network_request(limit):
    config = Settings(
        _env_file=None,
        deepseek_api_key="TEST-ONLY",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="TEST-ONLY",
        max_model_requests=1 if limit == "requests" else 10,
        max_model_tokens=20,
    )
    model = model_module.ResearchModel(config)
    usage, calls = {}, []
    model.usage = lambda: usage

    class LocalChain:
        async def ainvoke(self, messages, config):
            calls.append(1)
            usage["TEST-ONLY"] = {"total_tokens": 25}
            return "TEST-ONLY"

    try:
        await model.measured_call("test", [], LocalChain())
        with pytest.raises(model_module.ModelBudgetExceeded):
            await model.measured_call("test.format_retry", [], LocalChain())
        assert len(calls) == len(model.request_metrics) == 1
        assert model.request_metrics[0]["total_tokens"] == 25
    finally:
        await model.aclose()


async def test_large_evaluation_rejected_before_loading_services(monkeypatch):
    from types import SimpleNamespace

    from eval import run

    monkeypatch.setattr(run, "settings", lambda: SimpleNamespace(model_error=lambda: None))
    monkeypatch.setattr(run, "Vectors", lambda _: pytest.fail("must stop before service access"))
    args = SimpleNamespace(
        mode="local",
        ids=None,
        methods=["direct", "rag", "agent"],
        max_runs=3,
        questions="eval/questions.json",
        dry_run=False,
    )
    with pytest.raises(SystemExit, match="90.*尚未调用模型"):
        await run.main_async(args)


def test_five_case_cli_supplies_explicit_budget_without_real_calls(monkeypatch, tmp_path):
    import runpy
    import sys

    from eval import run

    calls = []

    async def inspect_plan(args):
        assert len(args.ids) == args.max_runs == 5
        assert args.methods == ["agent"] and args.source_manifest is None
        calls.append(args)

    monkeypatch.setattr(run, "main_async", inspect_plan)
    monkeypatch.setattr(sys, "argv", ["eval.p0", "--output", str(tmp_path / "test-only")])
    runpy.run_module("eval.p0", run_name="__main__")
    assert len(calls) == 1


async def test_failed_request_reports_unknown_usage_without_secret(caplog):
    model = model_module.ResearchModel(
        Settings(
            _env_file=None,
            deepseek_api_key="TEST-ONLY",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_model="TEST-ONLY",
        )
    )
    events = []

    async def report(row):
        events.append(row)

    class FailingChain:
        async def ainvoke(self, messages, config):
            raise RuntimeError("TEST secret provider body")

    model.on_usage = report
    try:
        with caplog.at_level("INFO", logger="shijian.model"):
            with pytest.raises(RuntimeError, match="TEST secret"):
                await model.measured_call("test", [], FailingChain())
        assert events[0]["total_tokens"] is None
        assert events[0]["observed_cumulative_tokens"] == 0
        assert events[0]["usage_missing_requests"] == 1
        assert events[0]["outcome"] == "failed"
        assert "TEST secret" not in caplog.text + json.dumps(events)
    finally:
        await model.aclose()
