"""Cancel one job (kept, not deleted) with a reason on its timeline. Run on the server: python -m scripts.cancel_job J33 "reason" """
import sys

from app.jobs import store as jobs
from app.jobs.models import NextAction

j = jobs.get(sys.argv[1])
assert j, "job not found"
j.status = "cancelled"
j.next_action = NextAction(what="Cancelled", owner="plumber")
jobs.log(j, "job_cancelled", sys.argv[2], actor="chinku", channel="code")
jobs.save(j)
print(j.job_id, j.status, "|", sys.argv[2])
