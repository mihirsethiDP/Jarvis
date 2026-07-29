from __future__ import annotations

from unittest.mock import MagicMock

from jarvis.config import Config
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext
from jarvis.tools import gchat as gchat_mod

from conftest import FakeIO


def make_ctx(tmp_path, audit, answers, fake_service):
    io = FakeIO(answers)
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    ctx = ToolContext(config=Config(raw={}), permissions=pm,
                      confirmer=Confirmer(io, audit), audit=audit)
    ctx.google_service = lambda api, version: fake_service
    return ctx


def test_list_spaces_filters_by_name(tmp_path, audit):
    service = MagicMock()
    service.spaces().list.return_value.execute.return_value = {
        "spaces": [
            {"name": "spaces/A", "displayName": "Plant Ops", "spaceType": "SPACE"},
            {"name": "spaces/B", "displayName": "Random", "spaceType": "SPACE"},
        ]
    }
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    tools = {t.name: t for t in gchat_mod.build_tools(ctx)}
    out = tools["list_chat_spaces"]("plant")
    assert "Plant Ops" in out and "Random" not in out
    assert "untrusted data" in out


def test_read_messages_wraps_sender_and_text(tmp_path, audit):
    service = MagicMock()
    service.spaces().messages().list.return_value.execute.return_value = {
        "messages": [{"createTime": "t", "sender": {"displayName": "Priya"}, "text": "hello"}]
    }
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    tools = {t.name: t for t in gchat_mod.build_tools(ctx)}
    out = tools["read_chat_messages"]("spaces/A")
    assert "Priya" in out and "hello" in out


def test_send_message_requires_confirmation(tmp_path, audit):
    service = MagicMock()
    ctx = make_ctx(tmp_path, audit, ["allow once", "no"], service)
    tools = {t.name: t for t in gchat_mod.build_tools(ctx)}
    out = tools["send_chat_message"]("spaces/A", "hi team")
    assert "Cancelled" in out
    service.spaces().messages().create.assert_not_called()


def test_send_message_only_sends_plain_text_body(tmp_path, audit):
    service = MagicMock()
    ctx = make_ctx(tmp_path, audit, ["allow once", "yes"], service)
    tools = {t.name: t for t in gchat_mod.build_tools(ctx)}
    service.spaces().messages().create.return_value.execute.return_value = {"name": "spaces/A/messages/1"}
    out = tools["send_chat_message"]("spaces/A", "hi team")
    assert "sent" in out.lower()
    service.spaces().messages().create.assert_called_with(
        parent="spaces/A", body={"text": "hi team"}
    )


def test_sender_ids_resolve_to_names_and_cache(tmp_path, audit):
    # Chat gives no displayName under user auth; ids must be resolved or the
    # transcript reads "unknown" for every speaker.
    chat_svc = MagicMock()
    chat_svc.spaces().messages().list.return_value.execute.return_value = {
        "messages": [
            {"createTime": "t1", "sender": {"name": "users/111", "type": "HUMAN"}, "text": "one"},
            {"createTime": "t2", "sender": {"name": "users/111", "type": "HUMAN"}, "text": "two"},
        ]
    }
    people_svc = MagicMock()
    people_svc.people().get.return_value.execute.return_value = {
        "names": [{"displayName": "Priya Rao"}]
    }
    ctx = make_ctx(tmp_path, audit, ["allow once"], chat_svc)
    ctx.google_service = lambda api, v: people_svc if api == "people" else chat_svc

    out = {t.name: t for t in gchat_mod.build_tools(ctx)}["read_chat_messages"]("spaces/A")
    assert "Priya Rao" in out and "unknown" not in out
    assert people_svc.people().get.call_count == 1  # second message used the cache


def test_sender_resolution_falls_back_to_id_on_failure(tmp_path, audit):
    chat_svc = MagicMock()
    chat_svc.spaces().messages().list.return_value.execute.return_value = {
        "messages": [{"createTime": "t", "sender": {"name": "users/999"}, "text": "hi"}]
    }
    people_svc = MagicMock()
    people_svc.people().get.return_value.execute.side_effect = RuntimeError("no directory")
    ctx = make_ctx(tmp_path, audit, ["allow once"], chat_svc)
    ctx.google_service = lambda api, v: people_svc if api == "people" else chat_svc
    out = {t.name: t for t in gchat_mod.build_tools(ctx)}["read_chat_messages"]("spaces/A")
    assert "users/999" in out   # degraded but still attributable


def test_directory_denial_skips_name_lookup(tmp_path, audit):
    chat_svc = MagicMock()
    chat_svc.spaces().messages().list.return_value.execute.return_value = {
        "messages": [{"createTime": "t", "sender": {"name": "users/111"}, "text": "hi"}]
    }
    people_svc = MagicMock()
    ctx = make_ctx(tmp_path, audit, ["allow once"], chat_svc)
    ctx.google_service = lambda api, v: people_svc if api == "people" else chat_svc
    ctx.permissions.set_grant("directory_read", "denied")
    out = {t.name: t for t in gchat_mod.build_tools(ctx)}["read_chat_messages"]("spaces/A")
    people_svc.people().get.assert_not_called()
    assert "users/111" in out
