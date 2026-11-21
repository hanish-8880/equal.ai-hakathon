"""Persistence (CLAUDE.md §12.5): create a pending approval on the LIVE server, restart the server process, check the
task and its pending approval are still there, then answer "no" (nothing is sent). Run from the laptop:
python -m eval.persistence_test   → eval/persistence.json"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.config import settings
from scripts import aws_deploy

IST = timezone(timedelta(hours=5, minutes=30))
B = settings().public_base_url
K = settings().chat_key


def home() -> dict:
    return httpx.get(f"{B}/api/home", params={"key": K}, timeout=30).json()


def say(text: str) -> None:
    httpx.post(f"{B}/api/chat", data={"key": K, "text": text}, timeout=30)


def main() -> None:
    marker = f"persistence check {int(time.time())}"
    say(f"Email me a note that says '{marker}'")
    before = None
    for _ in range(30):
        time.sleep(3)
        h = home()
        before = next((n for n in h["needs_you"] if marker in json.dumps(n)), None)
        if before:
            break
    assert before, "no pending approval appeared"
    st = aws_deploy.load()
    rc = aws_deploy.ssh("sudo systemctl restart chinku && sleep 2 && systemctl is-active chinku", st)
    t0 = time.time()
    while True:
        try:
            if httpx.get(f"{B}/health", timeout=5).json().get("ok"):
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    after = next((n for n in home()["needs_you"] if marker in json.dumps(n)), None)
    say("no")  # never send from a test
    out = {"run_at": datetime.now(IST).isoformat(timespec="minutes"), "restart_rc": rc, "back_after_s": round(time.time() - t0, 1),
           "pending_before": bool(before), "pending_after_restart": bool(after), "same_task": bool(after and after["task_id"] == before["task_id"]),
           "task_id": before["task_id"]}
    (Path(__file__).parent / "persistence.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
