"""Seed 3 SAMPLE bookings for the owner (today / tomorrow / day after) so Today, the week strip and Jobs show data.
Every sample is marked sample=True ("Sample" chip) and every sample customer number is the OWNER's own number — no button on
a sample job can ever call a stranger. Run on the server: python -m scripts.seed_samples"""
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.engine.validate import indian_mobile
from app.jobs import ops
from app.jobs import store as jobs
from app.jobs.models import Fact, Job, NextAction, Person, Window
from app.store.dynamo import store

IST = timezone(timedelta(hours=5, minutes=30))
s = settings()
owner = s.user_whatsapp
own_phone = indian_mobile(owner.replace("whatsapp:", ""))
today = datetime.now(IST).replace(minute=0, second=0, microsecond=0)
SAMPLES = [
    ("Geyser not heating", "Flat 210, Lake View Towers, Madhapur", "Suresh (sample)", 0, 17, "Today 17:00–19:00"),
    ("Bathroom tap leaking", "Flat 12B, Green Park, Kondapur", "Lakshmi (sample)", 1, 10, "Tomorrow 10:00–12:00"),
    ("Flush tank not filling", "House 4-21, Gachibowli", "Ravi (sample)", 2, 15, "Day after 15:00–17:00"),
]
made = []
for issue, site, name, day, hour, label in SAMPLES:
    start = (today + timedelta(days=day)).replace(hour=hour)
    w = {"start": start.isoformat(), "end": (start + timedelta(hours=2)).isoformat(), "label": label}
    j = Job(job_id=jobs.new_job_id(), plumber_id=owner, created_at=time.time(), sample=True, status="booked",
            issue=Fact(value=issue, status="confirmed", evidence_quote="sample data", source="seed"),
            site_address=Fact(value=site, status="confirmed", evidence_quote="sample data", source="seed"))
    j.roles.site_contact = Person(name=name, phone=own_phone)
    j.appointment.plumber_available = [Window(**w)]
    j.appointment.slot = Window(**w)
    j.appointment.state, j.appointment.confirmed_both = "booked", True
    store().claim(ops.slot_key(owner, w["start"]))  # the slot is really taken
    j.next_action = NextAction(what=f"Visit {label}", owner="plumber")
    jobs.log(j, "sample_created", f"SAMPLE booking {label} — demo data, not a real customer", simulated=True)
    jobs.save(j)
    made.append(f"{j.job_id} · {issue} · {label}")
print("seeded:", *made, sep="\n  ")
