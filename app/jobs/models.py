"""Plumber job domain model (CLAUDE.md §5). Schema first: everything else builds on these.
Distinct, never merged: supplier cost / approved customer charge / completed work;
client requested / plumber available / both confirmed; usually stocked / available now / hold offered / reserved."""
from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["confirmed", "tentative", "refused", "not_mentioned", "conflicting"]


class Fact(BaseModel):
    """Every extracted fact carries its evidence (CLAUDE.md §7.2)."""
    value: Any = None
    status: Status = "not_mentioned"
    evidence_quote: str | None = None
    source: str | None = None  # call id / message id / approval id that produced it


class Person(BaseModel):
    name: str | None = None
    phone: str | None = None  # E.164
    email: str | None = None
    whatsapp_joined: bool = False
    language: str = "English"


class Roles(BaseModel):
    site_contact: Person | None = None
    approver: Person | None = None
    billing_to: Person | None = None
    work_order: str | None = None


class Window(BaseModel):
    """A time window, e.g. tomorrow 11:00-13:00 (ISO with +05:30)."""
    start: str
    end: str
    label: str | None = None


class Appointment(BaseModel):
    plumber_available: list[Window] = Field(default_factory=list)
    requested_by_client: Fact = Field(default_factory=Fact)
    confirmed_both: bool = False
    slot: Window | None = None
    state: Literal["not_booked", "proposed", "booked", "pending", "cancelled"] = "not_booked"
    history: list[dict[str, Any]] = Field(default_factory=list)


class Part(BaseModel):
    part_id: str
    description: str
    brand: str | None = None
    model: str | None = None
    size: str | None = None
    qty: int = 1
    max_unit_price: float | None = None


class Offer(BaseModel):
    shop: str
    part_id: str
    exact_model_quoted: str | None = None
    matches_requested: Literal["exact", "different_model", "unknown"] = "unknown"
    stock: Literal["usually_stocked", "available_now", "tomorrow", "none", "unknown"] = "unknown"
    units_available: int | None = None
    unit_price: float | None = None
    mrp: float | None = None
    tax_included: Literal["yes", "no", "unknown"] = "unknown"
    delivery_charge: float | None = None  # None = unknown, never 0 by default
    condition: str | None = None
    condition_met: bool | None = None
    hold: Literal["none", "offered", "confirmed"] = "none"
    hold_until: str | None = None
    pickup_address: str | None = None
    earliest_pickup: str | None = None
    source_call_id: str | None = None
    evidence: dict[str, str] = Field(default_factory=dict)
    checked_at: float | None = None
    superseded_by: str | None = None


class Dependency(BaseModel):
    type: Literal["part_ready_before_visit"] = "part_ready_before_visit"
    part_id: str
    shop: str | None = None
    exact_model: bool = True
    max_unit_price: float | None = None
    then: str = "book_visit"
    state: Literal["waiting", "met", "not_met"] = "waiting"
    reason: str | None = None


class ApprovalRequest(BaseModel):
    id: str
    scope: str
    amount: float
    reason: str | None = None
    sent_to: Person | None = None
    channel: str | None = None
    version: int = 1
    response: Literal["approved", "rejected", "question", "pending"] = "pending"
    responder: str | None = None
    responder_is_designated: bool | None = None
    approved_scope: str | None = None
    approved_amount: float | None = None
    at: float | None = None
    evidence: str | None = None


class InvoiceLine(BaseModel):
    item: str
    qty: float
    unit_rate: float
    amount: float  # computed in code = qty * unit_rate
    source: str | None = None


class Invoice(BaseModel):
    number: str | None = None
    date: str | None = None
    lines: list[InvoiceLine] = Field(default_factory=list)
    labour: float = 0
    tax_rate: float = 0
    total: float = 0  # computed in code
    state: Literal["none", "draft", "awaiting_plumber", "approved", "sent", "delivery_failed", "paid"] = "none"
    send_attempts: list[dict[str, Any]] = Field(default_factory=list)
    idempotency_key: str | None = None


class Event(BaseModel):
    ts: float
    kind: str
    actor: str
    channel: str
    summary: str
    ref: str | None = None  # call id / message id / approval id
    simulated: bool = False


class NextAction(BaseModel):
    what: str
    owner: Literal["chinku", "plumber", "client", "shop", "approver"] = "plumber"
    blocked_by: str | None = None


class Job(BaseModel):
    job_id: str
    plumber_id: str
    status: Literal["open", "booked", "in_progress", "awaiting_approval", "invoicing", "closed", "cancelled"] = "open"
    created_at: float
    issue: Fact = Field(default_factory=Fact)
    site_address: Fact = Field(default_factory=Fact)
    flat: Fact = Field(default_factory=Fact)
    access_notes: Fact = Field(default_factory=Fact)
    photos: list[str] = Field(default_factory=list)
    roles: Roles = Field(default_factory=Roles)
    appointment: Appointment = Field(default_factory=Appointment)
    parts_needed: list[Part] = Field(default_factory=list)
    shop_offers: list[Offer] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    extra_work: list[ApprovalRequest] = Field(default_factory=list)
    invoice: Invoice = Field(default_factory=Invoice)
    timeline: list[Event] = Field(default_factory=list)
    next_action: NextAction | None = None
