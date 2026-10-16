from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


class DocumentInput(BaseModel):
    title: str = Field(min_length=1, max_length=250)
    author: str | None = Field(None, max_length=250)
    source_type: Literal["历史文献", "现代研究", "通俗文章", "网页", "其他"] = "历史文献"
    chapter: str | None = Field(None, max_length=250)
    edition: str | None = Field(None, max_length=500)
    source_url: HttpUrl | None = None
    raw_text: str = Field(min_length=20, max_length=500_000)
    people: list[str] = Field(default_factory=list, max_length=30)
    topics: list[str] = Field(default_factory=list, max_length=30)
    provenance_note: str = Field(
        default="版本及史源关系未经独立校勘；不自动视为事发时的一手记录。", max_length=1000
    )

    @field_validator("raw_text")
    @classmethod
    def nonempty_original(cls, value):
        if not value.strip():
            raise ValueError("原文不能只有空白")
        return value


class ResearchInput(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    mode: Literal["local", "hybrid"] = "local"


class SearchFilter(BaseModel):
    source_ids: list[str] = Field(default_factory=list, max_length=30)
    people: list[str] = Field(default_factory=list, max_length=10)
    topics: list[str] = Field(default_factory=list, max_length=10)


class Analysis(BaseModel):
    premise_status: Literal["待核查", "可能错误", "无明显预设"]
    premise_to_check: list[str] = Field(max_length=6)
    subquestions: list[str] = Field(min_length=1, max_length=6)
    search_queries: list[str] = Field(min_length=1, max_length=6)


class Assessment(BaseModel):
    sufficient: bool
    premise_status: Literal["支持", "反驳", "未确定"]
    gaps: list[str] = Field(max_length=6)
    next_queries: list[str] = Field(max_length=6)
    summary: str = Field(max_length=1000, description="可展示的证据摘要，不要内部思维过程")


class Citation(BaseModel):
    evidence_id: str
    quote: str = Field(min_length=2, max_length=1000)


class Claim(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["文献记载", "后人解释", "推断", "无法判断"]
    citations: list[Citation] = Field(default_factory=list, max_length=8)


class Answer(BaseModel):
    premise: Claim
    conclusions: list[Claim] = Field(max_length=8)
    events: list[Claim] = Field(max_length=12)
    disagreements: list[Claim] = Field(max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class SupportItem(BaseModel):
    claim_index: int
    verdict: Literal["支持", "部分支持", "不支持"]
    reason: str = Field(max_length=500)


class SupportReview(BaseModel):
    checks: list[SupportItem]
