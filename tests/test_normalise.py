import pytest

from app.brain.normalise import normalise, words_to_number


@pytest.mark.parametrize("words,val", [
    ("rendu", 2), ("padhihenu", 15), ("moodu vandala yabhai", 350), ("pannendu velu", 12000),
    ("paddenimidi velu aidu vandalu", 18500), ("yabai nalugu velu enimidi vandala enabhai rendu", 54882),
    ("two fifty", 250), ("three fifty", 350), ("three hundred fifty", 350), ("twenty four", 24),
    ("మూడు వందల యాబై", 350), ("రెండు", 2), ("పన్నెండు వేలు", 12000), ("ఆరు లక్షల", 600000),
    ("paanch sau", 500), ("do hazaar", 2000), ("ట్వెంటీ ఫోర్", 24),
])
def test_spoken_numbers(words, val):
    assert words_to_number(words.split()) == val


def test_transcript_from_real_model_output():
    n = normalise("చింకూ, జాబ్ 24 కీ రెండు యాంగిల్ వాల్వ్స్ కావాలి. జాక్వార్ మోడల్ ALD-0517, హాఫ్ ఇంచ్. మూడు వందల యాబై లోపుంటే")
    vals = {(s.kind, s.value) for s in n.spans}
    assert ("number", 2) in vals and ("number", 350) in vals and ("job_ref", "J24") in vals
    assert ("model_code", "ALD-0517") in vals  # exact characters kept
    assert all(s.check for s in n.spans if s.kind in ("number", "model_code"))


def test_money_units_and_lakh():
    n = normalise("valve ₹350 each, hose 300 rupees total, flat 1.5 lakh, pipe 15 mm")
    by = {s.raw: s for s in n.spans if s.kind in ("number", "money")}
    assert by["350"].kind == "money" and by["350"].unit == "each"
    assert by["300"].unit == "INR" and by["1.5 lakh"].value == 150000 and by["15"].unit == "mm"


def test_self_correction_keeps_last():
    n = normalise("angle valve 350 no, 300 each")
    assert n.corrections == [{"from": 350, "to": 300, "raw": "350 no, 300"}]
    n2 = normalise("moodu vandala yabhai kaadu moodu vandalu")
    assert n2.corrections[0]["from"] == 350 and n2.corrections[0]["to"] == 300


def test_job_refs_and_phones():
    n = normalise("Job 24 customer 98480 12321, also J7 and job number 105")
    refs = [s.value for s in n.spans if s.kind == "job_ref"]
    assert refs == ["J24", "J7", "J105"]
    assert [s.value for s in n.spans if s.kind == "phone"] == ["+919848012321"]


def test_plain_words_untouched():
    n = normalise("Srinivas shop ki call chesi price kanukko")
    assert n.text == "Srinivas shop ki call chesi price kanukko" and n.spans == []


def test_ambiguous_short_words_stay_text():
    for t in ["what do you need", "teen age boy", "char minar", "aru gantalaki"]:
        assert normalise(t).spans == [] or all(sp.kind != "number" for sp in normalise(t).spans), t
    assert [s.value for s in normalise("do valves kavali").spans if s.kind == "number"] == [2]
    assert [(s.kind, s.value) for s in normalise("paanch sau rupees").spans] == [("money", 500)]


def test_model_code_digits_not_numbers():
    n = normalise("Jaquar model ALD-0517 rendu kavali")
    assert ("model_code", "ALD-0517") in {(s.kind, s.value) for s in n.spans}
    assert [s.value for s in n.spans if s.kind == "number"] == [2] and "ALD-0517" in n.text
