from typing import Literal
from uuid import UUID

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
    mode: Literal["online", "local", "hybrid"] = "online"
    collection_id: UUID | None = None
    source_ids: list[UUID] | None = Field(None, min_length=1, max_length=30)


class CollectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    people: list[str] = Field(default_factory=list, max_length=20)
    events: list[str] = Field(default_factory=list, max_length=20)
    regions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def trimmed_name(cls, value):
        if not value.strip():
            raise ValueError("专题名称不能为空白")
        return value.strip()

    @field_validator("people", "events", "regions")
    @classmethod
    def tags(cls, values):
        if any(not v.strip() or len(v) > 100 for v in values):
            raise ValueError("标签应为 1–100 字")
        return list(dict.fromkeys(v.strip() for v in values))


class SearchFilter(BaseModel):
    source_ids: list[str] = Field(default_factory=list, max_length=30)
    people: list[str] = Field(default_factory=list, max_length=10)
    topics: list[str] = Field(default_factory=list, max_length=10)


class PlannedQuery(BaseModel):
    query: str = Field(min_length=1, max_length=128)
    language: Literal["zh", "en"]
    purpose: str = Field(max_length=120)


class Analysis(BaseModel):
    premise_status: Literal["待核查", "可能错误", "无明显预设"]
    premise_to_check: list[str] = Field(max_length=6)
    subquestions: list[str] = Field(min_length=1, max_length=6)
    search_queries: list[str] = Field(min_length=1, max_length=6)
    query_plan: list[PlannedQuery] = Field(default_factory=list, max_length=3)


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
    premise: Claim = Field(description="必须是含text、kind、citations的对象，不能是字符串或列表")
    conclusions: list[Claim] = Field(max_length=8)
    events: list[Claim] = Field(max_length=12)
    disagreements: list[Claim] = Field(max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class FactCheck(BaseModel):
    fragment: str = Field(min_length=1, max_length=200, description="待审表述中的连续短子句，只含一个事实")
    quotation_ids: list[str] = Field(max_length=4)
    verdict: Literal["支持", "部分支持", "不支持"]
    reason: str = Field(max_length=240, description="公开的证据支持说明，不是内部思维过程")


class QuoteReading(BaseModel):
    quotation_id: str
    literal_meaning: str = Field(max_length=240, description="忠实释读，保留主体、数量、先后与否定")


class QuoteReadings(BaseModel):
    readings: list[QuoteReading] = Field(max_length=8)


class SupportItem(BaseModel):
    claim_index: int
    verdict: Literal["支持", "部分支持", "不支持"]
    reason: str = Field(max_length=500)
    facts: list[FactCheck] = Field(default_factory=list, max_length=6)
    coverage_complete: bool = Field(default=False, description="是否覆盖此条所有重要事实；不完整必须为false")


class SupportReview(BaseModel):
    checks: list[SupportItem]
