"""HTTP protocol fixture for the observed DeepSeek thinking/tool-choice incompatibility."""

import json

import httpx

from shijian import model as model_module
from shijian.config import Settings
from shijian.schema import Analysis


async def test_structured_output_disables_thinking_in_actual_request(monkeypatch):
    captured = []
    expected = {
        "premise_status": "待核查",
        "premise_to_check": ["人物"],
        "subquestions": ["时序"],
        "search_queries": ["史思明"],
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
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "test-call",
                                    "type": "function",
                                    "function": {"name": "Analysis", "arguments": json.dumps(expected)},
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
        actual = await model.structured(Analysis, "TEST-ONLY", "TEST-ONLY")
        assert actual.model_dump() == expected
        assert captured[0]["thinking"] == {"type": "disabled"}
        assert captured[0]["tool_choice"]["function"]["name"] == "Analysis"
    finally:
        await model.aclose()
