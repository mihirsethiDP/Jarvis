from __future__ import annotations

from unittest.mock import MagicMock

from jarvis.config import Config
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext
from jarvis.tools import gcontacts as gcontacts_mod

from conftest import FakeIO

import pytest

from jarvis.tools import directory as directory_mod


@pytest.fixture(autouse=True)
def _clean_directory_cache():
    directory_mod._cache.update(at=0.0, people=None)
    yield
    directory_mod._cache.update(at=0.0, people=None)


def _prime(*entries):
    """Resolution now matches phonetically against the whole cached directory
    rather than Google's prefix-literal search — prime that cache."""
    directory_mod._cache.update(at=float("inf"), people=[
        {"resourceName": f"people/{i}",
         "names": [{"displayName": name}],
         "emailAddresses": [{"value": email}]}
        for i, (name, email) in enumerate(entries)
    ])


def make_ctx(tmp_path, audit, answers, fake_service):
    io = FakeIO(answers)
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    ctx = ToolContext(config=Config(raw={}), permissions=pm,
                      confirmer=Confirmer(io, audit), audit=audit)
    ctx.google_service = lambda api, version: fake_service
    return ctx


def test_find_colleague_matches_phonetic_spellings(tmp_path, audit):
    # The reported failure: "Diksha" for "Deeksha" — same name, different
    # romanization — returned "no directory match" and derailed the request.
    _prime(("Deeksha Chaturvedi", "deeksha.chaturvedi@digitalpaani.com"))
    ctx = make_ctx(tmp_path, audit, ["allow once"], MagicMock())
    tools = {t.name: t for t in gcontacts_mod.build_tools(ctx)}
    out = tools["find_colleague"]("Diksha")
    assert "deeksha.chaturvedi@digitalpaani.com" in out
    assert "AMBIGUOUS" not in out


def test_empty_result_hints_at_admin_dependency(tmp_path, audit):
    service = MagicMock()
    service.people().searchDirectoryPeople.return_value.execute.return_value = {"people": []}
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    tools = {t.name: t for t in gcontacts_mod.build_tools(ctx)}
    out = tools["find_colleague"]("Nobody")
    assert "External Directory Sharing" in out


def test_multiple_matches_forces_a_disambiguation_prompt(tmp_path, audit):
    _prime(("Priya Rao", "priya.rao@digitalpaani.com"),
           ("Priya Nair", "priya.nair@digitalpaani.com"))
    ctx = make_ctx(tmp_path, audit, ["allow once"], MagicMock())
    out = {t.name: t for t in gcontacts_mod.build_tools(ctx)}["find_colleague"]("Priya")
    assert "AMBIGUOUS" in out
    assert "Do NOT choose one yourself" in out
    assert "priya.rao@digitalpaani.com" in out and "priya.nair@digitalpaani.com" in out


def test_single_match_has_no_disambiguation_noise(tmp_path, audit):
    _prime(("Mohit Joshi", "mohit.joshi@digitalpaani.com"))
    ctx = make_ctx(tmp_path, audit, ["allow once"], MagicMock())
    out = {t.name: t for t in gcontacts_mod.build_tools(ctx)}["find_colleague"]("Mohit")
    assert "AMBIGUOUS" not in out
    assert "mohit.joshi@digitalpaani.com" in out
