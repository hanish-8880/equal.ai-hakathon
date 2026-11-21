"""Global safety net: tests must NEVER reach real phones, WhatsApp, email or OpenAI.
Every external action is replaced by a function that raises, unless a test explicitly mocks it.
(Added after a test accidentally dispatched a real Omni call to a sample number — DECISIONS.md, Sat 20:05.)"""
import pytest


class RealCallBlocked(RuntimeError):
    pass


def _blocked(name):
    def f(*a, **k):
        raise RealCallBlocked(f"{name} is blocked in tests — mock it explicitly")
    return f


@pytest.fixture(autouse=True)
def no_real_world(monkeypatch):
    from app.actions import email as email_action
    from app.brain import client
    from app.channels import whatsapp
    from app.engine import steps
    from app.voice import omni
    monkeypatch.setattr(omni, "dispatch_call", _blocked("Omni dispatch_call"))
    monkeypatch.setattr(steps, "DISPATCH", _blocked("Omni DISPATCH"))
    monkeypatch.setattr(client, "oai", _blocked("OpenAI client"))
    monkeypatch.setattr(whatsapp, "send", _blocked("WhatsApp send"))
    monkeypatch.setattr(email_action, "send", _blocked("SMTP send"))
    monkeypatch.setattr(steps, "NOTIFY", lambda to, text, audio=None: None)
    from app.jobs import slots
    monkeypatch.setattr(slots, "sync_inbound_agent", lambda force=False: False)  # never edit the live phone agent
    yield
