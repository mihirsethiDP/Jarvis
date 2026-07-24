from __future__ import annotations

from jarvis.security.confirm import Confirmer

from conftest import FakeIO


def test_explicit_yes_confirms(audit):
    c = Confirmer(FakeIO(["yes"]), audit)
    assert c.confirm("send_email", "send to a@b.com") is True


def test_anything_else_cancels(audit):
    for answer in ["no", "", "maybe", "hmm what"]:
        c = Confirmer(FakeIO([answer]), audit)
        assert c.confirm("send_email", "send to a@b.com") is False


def test_spoken_yes_with_punctuation_confirms(audit):
    c = Confirmer(FakeIO(["Yes."]), audit)
    assert c.confirm("send_email", "send to a@b.com") is True


def test_disabled_confirmer_passes_through_and_audits(audit, tmp_path):
    c = Confirmer(FakeIO([]), audit, enabled=False)
    assert c.confirm("write_file", "write x") is True
    assert any(e["decision"] == "skipped_disabled" for e in audit.tail(5))


def test_hindi_yes_confirms(audit):
    for answer in ["हाँ", "haan", "Theek hai.", "जी हाँ"]:
        c = Confirmer(FakeIO([answer]), audit)
        assert c.confirm("send_email", "send to a@b.com") is True, answer


def test_hindi_no_cancels(audit):
    for answer in ["नहीं", "nahi", "mat karo", "रहने दो"]:
        c = Confirmer(FakeIO([answer]), audit)
        assert c.confirm("send_email", "send to a@b.com") is False, answer


def test_eof_during_confirmation_cancels(audit):
    c = Confirmer(FakeIO([]), audit)  # FakeIO raises EOFError
    assert c.confirm("send_email", "send to a@b.com") is False
