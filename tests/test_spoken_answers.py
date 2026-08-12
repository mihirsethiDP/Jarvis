"""How people actually answer out loud.

Both gates matched a spoken answer against a fixed set of exact strings, so
anything padded the way speech is padded fell through to the closed branch.
The user granted access or confirmed an action aloud and Jarvis recorded a
refusal, then asked again — which reads as "it doesn't listen".
"""

from __future__ import annotations

import pytest

from jarvis.security.confirm import _is_yes
from jarvis.security.permissions import classify_answer, normalize_answer


def c(text: str) -> str:
    return classify_answer(normalize_answer(text))


@pytest.mark.parametrize("answer", [
    "yes please", "yes, allow it", "allow it once", "sure go ahead",
    "okay do it", "haan theek hai", "bilkul kar do", "go ahead",
])
def test_natural_grants_are_not_recorded_as_denials(answer):
    assert c(answer) == "once", f"{answer!r} was treated as a refusal"


@pytest.mark.parametrize("answer,expected", [
    ("always allow", "always"),
    ("allow for this session", "session"),
    ("yes for this session please", "session"),
])
def test_scope_is_understood(answer, expected):
    assert c(answer) == expected


@pytest.mark.parametrize("answer", [
    "no", "nahi", "dont allow that", "never allow this", "", "umm", "hmm",
])
def test_refusals_and_noise_still_fail_closed(answer):
    assert c(answer) == "deny"


@pytest.mark.parametrize("answer", ["yes but not always", "allow once but not always"])
def test_a_negated_always_does_not_become_a_permanent_grant(answer):
    # Matching "always" anywhere would silently turn a deliberately limited
    # grant into a standing one.
    assert c(answer) == "once"


# -- the confirmation gate ----------------------------------------------
@pytest.mark.parametrize("answer", [
    "yes", "yes please", "sure", "sure go ahead", "please do", "proceed",
    "absolutely", "alright", "fine", "yup", "correct", "sounds good",
    "um yes", "okay so send it", "haan", "bilkul", "kar dijiye",
    "theek hai", "haan bhej do", "go for it",
])
def test_affirmatives_confirm(answer):
    assert _is_yes(normalize_answer(answer)) is True, f"{answer!r} did not confirm"


@pytest.mark.parametrize("answer", [
    "no", "nahi", "no wait", "yes actually no", "cancel", "stop", "maybe",
    "i think so", "no, send it to Priya instead", "", "hmm", "galat",
    "mat karo", "wait",
])
def test_refusals_and_hedges_never_confirm(answer):
    # Deny wins anywhere, and nothing hedged belongs in a consent gate.
    assert _is_yes(normalize_answer(answer)) is False, f"{answer!r} leaked through"


# -- idioms and re-asking ------------------------------------------------
@pytest.mark.parametrize("answer", [
    "no problem", "no problem go ahead", "koi baat nahi", "yes no problem",
])
def test_affirmative_idioms_containing_a_deny_word_still_confirm(answer):
    # Deny-words were scanned per word anywhere in the answer, so "no problem"
    # cancelled the very action it was agreeing to.
    assert _is_yes(normalize_answer(answer)) is True


@pytest.mark.parametrize("answer", ["no issues at all", "no", "no wait"])
def test_genuine_denials_are_unaffected_by_the_idiom_list(answer):
    assert _is_yes(normalize_answer(answer)) is False


def test_a_garbled_answer_is_asked_again_rather_than_cancelled(audit):
    from conftest import FakeIO
    from jarvis.security.confirm import Confirmer

    io = FakeIO(["mmhm grrk", "yes"])
    assert bool(Confirmer(io, audit).confirm("send_email", "I will send this."))
    assert len(io.asked) == 2, "a mis-transcribed yes should get one more chance"


def test_an_explicit_no_is_not_second_guessed(audit):
    from conftest import FakeIO
    from jarvis.security.confirm import Confirmer

    io = FakeIO(["no"])
    assert not Confirmer(io, audit).confirm("send_email", "I will send this.")
    assert len(io.asked) == 1, "a clear refusal must not be re-asked"


def test_two_unclear_answers_still_fail_closed(audit):
    from conftest import FakeIO
    from jarvis.security.confirm import Confirmer

    io = FakeIO(["mmhm", "shhh"])
    assert not Confirmer(io, audit).confirm("send_email", "I will send this.")


def test_a_garbled_permission_answer_is_asked_again(audit, tmp_path):
    # memory_recall is asked once per session, so one mis-transcribed answer
    # silently switched memory off for the whole session.
    from conftest import FakeIO
    from jarvis.security.permissions import PermissionManager

    io = FakeIO(["mmhm grrk", "allow once"])
    pm = PermissionManager(io, audit, store_path=tmp_path / "p.json")
    assert pm.require("memory_recall", "use what it remembered") is True
    assert len(io.asked) == 2


def test_an_explicit_permission_denial_is_not_re_asked(audit, tmp_path):
    from conftest import FakeIO
    from jarvis.security.permissions import PermissionManager

    io = FakeIO(["no"])
    pm = PermissionManager(io, audit, store_path=tmp_path / "p.json")
    assert pm.require("memory_recall", "use what it remembered") is False
    assert len(io.asked) == 1


# -- a qualified yes is a correction, not consent ------------------------
# Found by the guardrail audit: _is_yes inspected only the first word or two
# and discarded the rest, so "yes, but send it to Priya instead" confirmed
# the ORIGINAL action — the wrong colleague was emailed and Jarvis reported
# success. The mirror case with a leading "no" was already blocked.

@pytest.mark.parametrize("answer", [
    "yes, but send it to Priya instead",
    "sure, but change the time to 4pm",
    "ok but remove the attachment",
    "fine, use my personal address",
    "proceed with 4pm instead",
    "haan lekin 4 baje",
    "send it to Priya instead",
    "correct the subject line first",
    "yes but make it tomorrow",
])
def test_a_qualified_yes_never_confirms_the_uncorrected_action(answer):
    assert _is_yes(normalize_answer(answer)) is False, (
        f"{answer!r} confirmed the action it was correcting"
    )


@pytest.mark.parametrize("answer", [
    "yes", "yes please", "ok great", "yes thanks", "yes that is right",
    "sure go ahead", "okay so send it", "haan bhej do", "no problem",
])
def test_plain_agreement_still_confirms(answer):
    assert _is_yes(normalize_answer(answer)) is True


def test_the_correction_reaches_the_model(audit):
    from conftest import FakeIO
    from jarvis.security.confirm import Confirmer
    from jarvis.tools import cancelled_by_user

    io = FakeIO(["yes, but send it to Priya instead"])
    result = Confirmer(io, audit).confirm("send_email", "I will email Mohit.")
    assert not result
    assert len(io.asked) == 1, "a clearly-heard correction must not be re-asked"
    relayed = cancelled_by_user(result, "sending that email")
    assert "Priya" in relayed and "correction" in relayed


@pytest.mark.parametrize("answer,expected", [
    ("sure, no problem", "once"),
    ("yes, no worries", "once"),
    ("allow once, no issues", "once"),
    ("haan, koi baat nahi", "once"),
    ("allow for this session, no problem", "session"),
])
def test_grants_containing_an_everyday_idiom_are_not_denials(answer, expected):
    # The idiom fix existed only in the confirmation gate, so these natural
    # answers to a permission question were recorded as refusals.
    assert c(answer) == expected


def test_no_need_is_deliberately_not_treated_as_agreement():
    # Tempting to add, because "always allow, no need to ask again" is a
    # grant — but "no need to send it" is a refusal, and mis-reading that as
    # consent sends mail nobody approved. It stays failing closed.
    assert _is_yes(normalize_answer("no need to send it")) is False


def test_allow_once_covers_the_whole_request(audit, tmp_path):
    # One request calls several tools. "Allow once" set no state at all, so
    # the user was interrogated at every step after already saying yes.
    from conftest import FakeIO
    from jarvis.security.permissions import PermissionManager

    io = FakeIO(["allow once"])
    pm = PermissionManager(io, audit, store_path=tmp_path / "p.json")
    pm.begin_turn()
    assert all(pm.require("drive_read", "search Drive") for _ in range(3))
    assert len(io.asked) == 1

    # ...but it does not leak into the next request.
    pm.begin_turn()
    io.answers = ["no"]
    assert pm.require("drive_read", "search Drive") is False
