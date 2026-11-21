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
    contacts: list[dict[str, Any]] = Field(default_factory=list)
    summary: str | None = None
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
    language: str = "English"


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


# ---------- document analysis + slides ----------
class KeyValue(BaseModel):
    label: str
    value: str  # exactly as printed in the document
    quote: str | None = None


class DocAnalysis(BaseModel):
    headline: str                 # one line answer to what the user asked
    key_values: list[KeyValue] = Field(default_factory=list)
    summary: str


class Slide(BaseModel):
    title: str
    bullets: list[str]


class SlideDeck(BaseModel):
    title: str
    subtitle: str | None = None
    slides: list[Slide]


# ---------- plumber mode (CLAUDE.md v2 §8) ----------
Intent = Literal["create_job", "book_visit", "source_part", "set_dependency", "request_approval", "approval_response",
                 "close_job_invoice", "make_estimate", "correct_detail", "report_delay", "cancel", "status_query", "small_talk", "other"]


class PlumberRoute(BaseModel):
    intent: Intent
    job_ref: str | None = None  # "J24" | "new" | "ambiguous" | null
    entities: dict[str, Any] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)
    clarifying_question: str | None = None


class FactOut(BaseModel):
    value: Any = None
    status: Literal["confirmed", "tentative", "refused", "not_mentioned", "conflicting"] = "not_mentioned"
    evidence_quote: str | None = None


class WindowOut(BaseModel):
    start: str  # ISO 8601 with +05:30
    end: str
    label: str
    evidence_quote: str


class PersonOut(BaseModel):
    name: str | None = None
    phones: list[str] = Field(default_factory=list)  # digits exactly as said
    email: str | None = None
    evidence_quote: str | None = None


# ---------- listening pipeline (CLAUDE.md §3A) ----------
class Heard(BaseModel):
    text: str
    languages: list[str] = Field(default_factory=list)
    low_confidence_spans: list[str] = Field(default_factory=list)
    duration_s: float | None = None
    model: str | None = None
    audio_ref: str | None = None
    source: str = "text"  # app_voice | whatsapp_voice | phone | text


class Action(BaseModel):
    intent: Intent
    job_ref: str | None = None  # "J24" | "new" | "ambiguous" | null
    entities: dict[str, Any] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)
    confidence: float = 1.0


class UnderstandResult(BaseModel):
    actions: list[Action] = Field(default_factory=list)
    clarifying_question: str | None = None
    readback: str = ""


class JobDraft(BaseModel):
    issue: FactOut
    site_address: FactOut
    flat: FactOut
    access_notes: FactOut
    site_contact: PersonOut | None = None
    billing_to: PersonOut | None = None
    approver: PersonOut | None = None
    work_order: FactOut
    plumber_windows: list[WindowOut] = Field(default_factory=list)
