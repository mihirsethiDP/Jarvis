from __future__ import annotations

from unittest.mock import MagicMock

from jarvis.config import Config
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext
from jarvis.tools import gcontacts as gcontacts_mod

from conftest import FakeIO


def make_ctx(tmp_path, audit, answers, fake_service):
    io = FakeIO(answers)
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    ctx = ToolContext(config=Config(raw={}), permissions=pm,
                      confirmer=Confirmer(io, audit), audit=audit)
    ctx.google_service = lambda api, version: fake_service
    return ctx


def test_find_colleague_uses_directory_source_and_readmask(tmp_path, audit):
    service = MagicMock()
    service.people().searchDirectoryPeople.return_value.execute.return_value = {
        "people": [{
            "names": [{"displayName": "Priya Rao"}],
            "emailAddresses": [{"value": "priya@digitalpaani.com"}],
        }]
    }
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    tools = {t.name: t for t in gcontacts_mod.build_tools(ctx)}
    out = tools["find_colleague"]("Priya")
    assert "priya@digitalpaani.com" in out
    service.people().searchDirectoryPeople.assert_called_with(
        query="Priya", readMask="names,emailAddresses,phoneNumbers",
        sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
    )


def test_empty_result_hints_at_admin_dependency(tmp_path, audit):
    service = MagicMock()
    service.people().searchDirectoryPeople.return_value.execute.return_value = {"people": []}
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    tools = {t.name: t for t in gcontacts_mod.build_tools(ctx)}
    out = tools["find_colleague"]("Nobody")
    assert "External Directory Sharing" in out
