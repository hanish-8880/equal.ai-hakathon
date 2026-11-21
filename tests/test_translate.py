"""Language: Chinku's messages in Telugu / Hindi / mix — but a translation that changes any number is never shown."""
import pytest

from app.brain import translate
from app.engine import runner
from app.jobs import prefs
from app.store.dynamo import MemoryStore, set_store

ME = "whatsapp:+919989683607"
SRC = "J24: 2 angle valve ₹700, 1 hose ₹300, labour ₹400. Total ₹1,400.\nSend to Sai Facility against WO-117? (yes / correct it)"


@pytest.fixture(autouse=True)
def mem():
    set_store(MemoryStore(path=None))
    yield


def fake(outputs):
    calls = []

    def f(model, system, user, schema, task_id=None, tag="llm", effort=None):
        calls.append(model)
        return schema(text=outputs[min(len(calls), len(outputs)) - 1])
    return f, calls


def test_good_translation_is_used_and_cached(monkeypatch):
    te = "J24: 2 angle valves ₹700, 1 hose ₹300, కూలీ ₹400. మొత్తం ₹1,400.\nSai Facility కి WO-117 మీద పంపాలా? (yes / correct it)"
    f, calls = fake([te])
    monkeypatch.setattr("app.brain.client.json_call", f)
    assert translate.to_lang(SRC, "te") == te
    assert translate.to_lang(SRC, "te") == te and len(calls) == 1  # second time from cache


def test_translation_that_changes_a_number_falls_back_to_english(monkeypatch):
    bad = "J24: 2 angle valves ₹700, 1 hose ₹300, కూలీ ₹400. మొత్తం ₹1,500. (yes / correct it)"  # 1,400 → 1,500
    f, calls = fake([bad, bad])
    monkeypatch.setattr("app.brain.client.json_call", f)
    assert translate.to_lang(SRC, "hi") == SRC and len(calls) == 2  # fast model, brain model, then English


def test_answer_hint_must_survive():
    assert not translate.safe("Book it? (yes / no)", "బుక్ చేయాలా?")
    assert translate.safe("Book it? (yes / no)", "బుక్ చేయాలా? (yes / no)")


def test_english_users_and_already_translated_text_skip_the_model(monkeypatch):
    monkeypatch.setattr("app.brain.client.json_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert translate.to_lang(SRC, "en") == SRC
    assert translate.to_lang("మీ పని బుక్ అయ్యింది J24", "te") == "మీ పని బుక్ అయ్యింది J24"


def test_deliver_shows_messages_in_the_plumbers_language(monkeypatch):
    import app.main as m
    prefs.set_language(ME, "te-en")
    monkeypatch.setattr(translate, "to_lang", lambda text, code: f"[{code}] {text}")
    monkeypatch.setattr(m, "_web_preferred", lambda u: True)
    m.deliver(ME, "Booked ✅ J24")
    from app.store.dynamo import store
    assert store().events(f"chat:{ME}")[-1]["payload"]["text"] == "[te-en] Booked ✅ J24"


@pytest.mark.parametrize("w", ["yes", "avunu", "హా", "అవును", "pampu", "haan", "theek hai", "bhejo", "हाँ", "भेजो"])
def test_yes_words(w):
    assert runner.YES.match(w)


@pytest.mark.parametrize("w", ["no", "vaddu", "వద్దు", "nahi", "नहीं", "mat"])
def test_no_words(w):
    assert runner.NO.match(w) and not runner.YES.match(w)


def test_english_lines_inside_a_telugu_message_are_translated(monkeypatch):
    monkeypatch.setattr(translate, "cached", lambda text, code: None)
    seen = []

    def fake(model, system, user, schema, task_id=None, tag="llm", effort=None):
        seen.append(user)
        return schema(text="1) పేజీని టైప్ చేస్తాను\n2) మీకు ఇమెయిల్ రాస్తాను")
    monkeypatch.setattr("app.brain.client.json_call", fake)
    msg = "పేజీని ట్రాన్స్‌క్రైబ్ చేయి · రేపు 9:00కి రిమైండ్\n1) type out the page exactly\n2) write the email to you"
    out = translate.to_lang(msg, "te")
    assert seen == ["1) type out the page exactly\n2) write the email to you"]
    assert out.splitlines()[0] == msg.splitlines()[0] and "1) పేజీని" in out and "9:00" in out
