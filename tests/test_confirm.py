from __future__ import annotations

from jarvis.security.confirm import Confirmer

from conftest import FakeIO


def test_explicit_yes_confirms(audit):
    c = Confirmer(FakeIO(["yes"]), audit)
    assert bool(c.confirm("send_email", "send to a@b.com")) is True


def test_anything_else_cancels(audit):
    for answer in ["no", "", "maybe", "hmm what"]:
        c = Confirmer(FakeIO([answer]), audit)
        assert bool(c.confirm("send_email", "send to a@b.com")) is False


def test_spoken_yes_with_punctuation_confirms(audit):
    c = Confirmer(FakeIO(["Yes."]), audit)
    assert bool(c.confirm("send_email", "send to a@b.com")) is True


def test_disabled_confirmer_passes_through_and_audits(audit, tmp_path):
    c = Confirmer(FakeIO([]), audit, enabled=False)
    assert bool(c.confirm("write_file", "write x")) is True
    assert any(e["decision"] == "skipped_disabled" for e in audit.tail(5))


def test_hindi_yes_confirms(audit):
    for answer in ["हाँ", "haan", "Theek hai.", "जी हाँ"]:
        c = Confirmer(FakeIO([answer]), audit)
        assert bool(c.confirm("send_email", "send to a@b.com")) is True, answer


def test_hindi_no_cancels(audit):
    for answer in ["नहीं", "nahi", "mat karo", "रहने दो"]:
        c = Confirmer(FakeIO([answer]), audit)
        assert bool(c.confirm("send_email", "send to a@b.com")) is False, answer


def test_eof_during_confirmation_cancels(audit):
    c = Confirmer(FakeIO([]), audit)  # FakeIO raises EOFError
    assert bool(c.confirm("send_email", "send to a@b.com")) is False


def test_padded_affirmatives_confirm(audit):
    for answer in ["yes please", "haan bhej do", "Yes, go ahead", "ok great"]:
        c = Confirmer(FakeIO([answer]), audit)
        assert bool(c.confirm("send_email", "send to a@b.com")) is True, answer


def test_deny_word_anywhere_wins(audit):
    for answer in ["yes wait", "haan... nahi nahi", "ok but actually cancel", "yes, don't"]:
        c = Confirmer(FakeIO([answer]), audit)
        assert bool(c.confirm("send_email", "send to a@b.com")) is False, answer


def test_declined_with_words_carries_the_correction(audit):
    c = Confirmer(FakeIO(["no, send it to Priya instead"]), audit)
    result = c.confirm("send_email", "send to mohit@x.com")
    assert not result
    assert "Priya instead" in result.correction


def test_plain_refusal_carries_no_correction(audit):
    for answer in ["no", "nahi", "cancel", ""]:
        c = Confirmer(FakeIO([answer]), audit)
        result = c.confirm("send_email", "send to a@b.com")
        assert not result
        assert result.correction == "", answer


def test_hinglish_correction_is_preserved(audit):
    c = Confirmer(FakeIO(["nahi, 4 baje karo"]), audit)
    result = c.confirm("create_calendar_event", "create at 3pm")
    assert not result
    assert "4 baje" in result.correction


def test_rate_limited_action_is_refused_without_asking(audit, tmp_path):
    from jarvis.security.limits import ActionLimiter

    lim = ActionLimiter(caps={"send_email": (1, 5)}, path=tmp_path / "l.json")
    io = FakeIO(["yes", "yes"])
    c = Confirmer(io, audit, limiter=lim)

    assert bool(c.confirm("send_email", "first one")) is True
    result = c.confirm("send_email", "second one")
    assert not result                       # refused outright
    assert len(io.asked) == 1               # the user was never even asked again
    assert any(e["decision"] == "rate_limited" for e in audit.tail(5))


def test_only_confirmed_actions_count_toward_the_limit(audit, tmp_path):
    from jarvis.security.limits import ActionLimiter

    lim = ActionLimiter(caps={"send_email": (1, 5)}, path=tmp_path / "l.json")
    c = Confirmer(FakeIO(["no", "yes"]), audit, limiter=lim)
    c.confirm("send_email", "declined")     # cancelled — shouldn't consume quota
    assert bool(c.confirm("send_email", "allowed")) is True
