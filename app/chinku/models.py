"""Plan / Task / Result for Chinku (CLAUDE.md §5, §8)."""
from typing import Any, Literal

from pydantic import BaseModel, Field

TOOLS = ("compare", "analyze", "transcribe_image", "extract", "search", "map", "call", "draft_email", "send_email", "remind",
         "remember", "recall", "compute", "scam_check", "reply")


class Step(BaseModel):
    id: str
    tool: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    needs_approval: bool = False
    output_name: str | None = None


class Suggested(BaseModel):
    label: str
    instruction: str


class CannotDo(BaseModel):
    what: str
    why: str
    best_alternative: str | None = None


class Plan(BaseModel):
    understood: str
    steps: list[Step] = Field(default_factory=list)
    clarifying_question: str | None = None
    suggested_actions: list[Suggested] = Field(default_factory=list)
    cannot_do: list[CannotDo] = Field(default_factory=list)
    confidence: float = 0.0


class StepState(BaseModel):
    status: Literal["pending", "running", "waiting", "done", "failed", "skipped"] = "pending"
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    preview: dict[str, Any] | None = None  # exactly what will go to whom (approval card)


class Task(BaseModel):
    task_id: str
    user: str
    request: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    plan: Plan
    steps: dict[str, StepState] = Field(default_factory=dict)
    status: Literal["planned", "running", "waiting", "done", "partial", "failed", "cancelled"] = "planned"
    created_at: float
    updated_at: float
