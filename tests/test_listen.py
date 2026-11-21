"""Voice: silence/noise must never become a request (observed: silence → "complaint", "మమ్మీకి call చేయు", our own hint text)."""
from types import SimpleNamespace as NS

import pytest

from app.brain import listen
from app.store.dynamo import MemoryStore, set_store


@pytest.fixture(autouse=True)
def mem():
    set_store(MemoryStore(path=None))


def fake(text, lps, monkeypatch):
    r = NS(text=text, logprobs=[NS(token=w, logprob=lp) for w, lp in zip(text.split() or [text], lps)])
    client = NS(audio=NS(transcriptions=NS(create=lambda **k: r)))
    monkeypatch.setattr("app.brain.client.oai", lambda: client)


@pytest.mark.parametrize("text,lps,kept", [
    ("Call Mummy.", [-0.1, -0.08], True),                                  # clear short command → kept
    ("రేపు సాయంత్రం మమ్మీకి డాక్టర్ అపాయింట్మెంట్", [-0.1, -0.2, -0.1, -0.05, -0.1], True),
    ("complaint.", [-1.14], False),                                        # silence phantom (measured)
    ("మమ్మీకి call చేయు.", [-1.6, -1.2, -2.0], False),                     # silence phantom that looked like a request
    ("context: ### The speaker mixes Telugu", [-0.3] * 6, False),          # our own hint echoed back
    ("email", [-2.73], False),                                             # noise phantom (measured −2.73)
])
def test_phantom_speech_is_rejected(monkeypatch, text, lps, kept):
    fake(text, lps, monkeypatch)
    h = listen.transcribe(b"x" * 5000, "a.webm", "u", "voice")
    assert (h is not None) == kept
