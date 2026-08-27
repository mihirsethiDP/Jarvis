"""Resolving spoken Indian names against the directory.

The measured failure: the user said "Deeksha", STT wrote "Diksha", Google's
prefix-literal search found nothing, and a 30-second request became a
2.5-minute interrogation that ended with the user quitting before the message
sent. There is no single correct Latin spelling of most Indian names, so the
match has to be phonetic, and it has to happen on the FIRST call.
"""

from __future__ import annotations

import pytest

from jarvis.names import phonetic_key, tokens_match
from jarvis.tools import directory as D


@pytest.mark.parametrize("a,b", [
    ("Deeksha", "Diksha"), ("Deeksha", "Dixa"),
    ("Deepak", "Dipak"), ("Verma", "Varma"), ("Warma", "Varma"),
    ("Dikshit", "Dixit"), ("Zoya", "Joya"), ("Chaturvedi", "Caturvedi"),
    ("Khusboo", "Khusbu"), ("Sharma", "Sarma"),
])
def test_romanization_variants_share_a_key(a, b):
    assert phonetic_key(a) == phonetic_key(b), (a, b, phonetic_key(a), phonetic_key(b))


@pytest.mark.parametrize("a,b", [
    ("Deepa", "Deep"),      # different names, not variants
    ("Mansi", "Mohit"),
    ("Ranjana", "Rana"),    # Rana must NOT equal Ranjana at key level
])
def test_distinct_names_stay_distinct(a, b):
    assert phonetic_key(a) != phonetic_key(b)


def test_order_free_token_matching():
    assert tokens_match("Chaturvedi Diksha", "Deeksha Chaturvedi")
    assert not tokens_match("Diksha Sharma", "Deeksha Chaturvedi")


# -- the resolver over a fake directory -----------------------------------
def _person(name, mail=""):
    return {"resourceName": f"people/{abs(hash(name + mail))}",
            "names": [{"displayName": name}],
            "emailAddresses": [{"value": mail}] if mail else []}


PEOPLE = [
    _person("Deeksha Chaturvedi", "deeksha.chaturvedi@digitalpaani.com"),
    _person("Ranjana Majumdar", "ranjana.majumdar@digitalpaani.com"),
    _person("Himanshu Rana", "himanshu.rana@digitalpaani.com"),
    _person("Mansi Jain", "mansi.jain@digitalpaani.com"),
    _person("Mansi Jain", "mansi.jain@ecoinnovision.com"),
    _person("Mansi Jain", "support@ecoinnovision.com"),
    _person("Mohit Joshi", "mohit.joshi@digitalpaani.com"),
]


@pytest.fixture(autouse=True)
def fake_directory(monkeypatch):
    monkeypatch.setattr(D, "_fetch_all", lambda svc: PEOPLE)
    yield
    D._cache.update(at=0.0, people=None)


def names(matches):
    return [D.display_name(p) for p in matches]


def test_the_reported_failure_resolves_first_try():
    matches, confident = D.resolve(None, "Diksha")
    assert names(matches) == ["Deeksha Chaturvedi"]
    assert confident, "a phonetic identity is not a guess and must not interrogate"


def test_full_name_in_either_spelling_and_order():
    for q in ("Diksha Chaturvedi", "deeksha chaturvedi", "Chaturvedi Deeksha"):
        matches, confident = D.resolve(None, q)
        assert (names(matches), confident) == (["Deeksha Chaturvedi"], True), q


def test_a_dropped_letter_recovers_without_inventing_ambiguity():
    # At a flat fuzzy floor, "Ranjna" matched Himanshu RANA alongside RANJANA
    # and manufactured a choice that does not exist.
    matches, confident = D.resolve(None, "Ranjna")
    assert names(matches) == ["Ranjana Majumdar"]
    assert not confident   # still says which name it assumed


def test_colleague_outranks_external_contacts():
    matches, _ = D.resolve(None, "Mansi Jain")
    assert len(matches) == 3
    narrowed, did = D.prefer_colleagues(matches, "digitalpaani.com")
    assert did
    assert [D.email(p) for p in narrowed] == ["mansi.jain@digitalpaani.com"]


def test_two_real_colleagues_stay_ambiguous():
    people = PEOPLE + [_person("Mansi Jain", "mansi.j2@digitalpaani.com")]
    matches = [p for p in people if D.display_name(p) == "Mansi Jain"]
    narrowed, did = D.prefer_colleagues(matches, "digitalpaani.com")
    assert not did, "two genuine colleagues must still be asked about"
    assert len(narrowed) == 4


def test_unknown_names_return_nothing():
    matches, confident = D.resolve(None, "Zoltan")
    assert matches == [] and not confident
