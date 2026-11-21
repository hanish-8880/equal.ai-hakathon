"""Pydantic models for every brain input/output. Schema first."""
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------- perceive ----------
class ImageRead(BaseModel):
    image_type: Literal["banner", "passbook", "product", "list", "other"]
    fields: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    unreadable: list[str] = Field(default_factory=list)


class LineItem(BaseModel):
    label: str
    amount: float | None = None
    quantity: float | None = None


class DocRead(BaseModel):
    image_type: Literal["document"] = "document"
    doc_type: str  # invoice / bill / quotation / statement / other
    title: str | None = None
    parties: list[str] = Field(default_factory=list)
    line_items: list[LineItem] = Field(default_factory=list)
    stated_total: float | None = None
    currency: str = "INR"
    notes: str | None = None
    unreadable: list[str] = Field(default_factory=list)


class Transcript(BaseModel):
    text: str
    language: str | None = None


# ---------- router ----------
class RouteResult(BaseModel):
    spec: str | None  # None = not a task / chit-chat
    inputs: dict[str, Any] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)
    clarifying_question: str | None = None
    language: str = "Telugu-English mix"


# ---------- brief writer (CLAUDE.md 8.2) ----------
class MustCollectItem(BaseModel):
    field: str
    question_to_ask: str
    acceptable_answer_example: str


class CallBrief(BaseModel):
    on_behalf_of: str
    callee_name: str | None = None
    callee_role: str
    language: str
    max_minutes: int
    opening_line: str
    goal: str
    must_collect: list[MustCollectItem]
    context_facts: list[str] = Field(default_factory=list)
    do_not_share: list[str] = Field(default_factory=list)
    closing_line: str
    call_script: str


# ---------- extractor (CLAUDE.md 8.3) ----------
FieldStatus = Literal["confirmed", "tentative", "refused", "not_mentioned"]
CallOutcome = Literal["completed", "no_answer", "wrong_number", "callback_requested", "refused", "cut_off"]


class ExtractedField(BaseModel):
    name: str
    value: Any = None
    unit: str | None = None
    status: FieldStatus
    evidence_quote: str | None = None


class Extraction(BaseModel):
    fields: list[ExtractedField]
    call_outcome: CallOutcome
    follow_up_needed: bool

    def as_dict(self) -> dict[str, ExtractedField]:
        return {f.name: f for f in self.fields}


# ---------- reply ----------
class Reply(BaseModel):
    text: str


# ---------- email ----------
class EmailDraft(BaseModel):
    subject: str
    body: str
