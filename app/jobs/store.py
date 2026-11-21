"""Job persistence on the shared store (table "jobs"). One persistent job id per job ("J21", "J22", ...)."""
import time
from typing import Any

from app.config import settings
from app.jobs.models import Event, Job
from app.store.dynamo import store


def new_job_id() -> str:
    """Unique, human-friendly id. Uniqueness via the idempotency claim (conditional write in DynamoDB)."""
    n = int(settings().job_id_start)
    existing = [j["job_id"] for j in store().scan("jobs")]
    if existing:
        n = max(n, max(int(x[1:]) for x in existing if x[1:].isdigit()) + 1)
    while not store().claim(f"jobid:J{n}"):
        n += 1
    return f"J{n}"


def save(job: Job) -> Job:
    store().put("jobs", job.job_id, job.model_dump())
    return job


def get(job_id: str) -> Job | None:
    d = store().get("jobs", job_id.upper())
    return Job.model_validate(d) if d else None


def open_jobs(plumber_id: str) -> list[Job]:
    rows = [Job.model_validate(d) for d in store().scan("jobs", plumber_id=plumber_id)]
    return sorted([j for j in rows if j.status not in ("closed", "cancelled")], key=lambda j: j.created_at)


def log(job: Job, kind: str, summary: str, actor: str = "chinku", channel: str = "app", ref: str | None = None,
        simulated: bool = False) -> None:
    job.timeline.append(Event(ts=time.time(), kind=kind, actor=actor, channel=channel, summary=summary, ref=ref, simulated=simulated))


def card(job: Job) -> dict[str, Any]:
    """Compact job card for the chat UI / dashboard (phones masked)."""
    from app.engine.validate import mask_phone
    r = job.roles
    return {"type": "job", "job_id": job.job_id, "status": job.status,
            "issue": job.issue.value, "site": " · ".join(str(x) for x in (job.flat.value, job.site_address.value) if x),
            "site_contact": {"name": r.site_contact.name, "phone": mask_phone(r.site_contact.phone)} if r.site_contact else None,
            "billing_to": (r.billing_to.name or r.billing_to.email) if r.billing_to else None,
            "work_order": r.work_order,
            "plumber_windows": [w.label or f"{w.start}–{w.end}" for w in job.appointment.plumber_available],
            "appointment": job.appointment.state,
            "next_action": job.next_action.model_dump() if job.next_action else None}
