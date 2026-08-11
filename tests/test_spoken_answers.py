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
