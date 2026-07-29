from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest

from jarvis.config import Config
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext
from jarvis.tools import gmail as gmail_mod

from conftest import FakeIO


def make_ctx(tmp_path, audit, answers, fake_service):
    io = FakeIO(answers)
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    ctx = ToolContext(config=Config(raw={}), permissions=pm,
                      confirmer=Confirmer(io, audit), audit=audit)
    ctx.google_service = lambda api, version: fake_service
    return ctx


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_extract_text_prefers_plain_over_html():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": b64("<p>hi</p>")}},
            {"mimeType": "text/plain", "body": {"data": b64("hi there")}},
        ],
    }
    assert gmail_mod._extract_text(payload) == "hi there"


def test_extract_text_falls_back_to_html_stripped():
    payload = {"mimeType": "text/html", "body": {"data": b64("<b>bold</b> text")}}
    assert "bold" in gmail_mod._extract_text(payload) and "<b>" not in gmail_mod._extract_text(payload)


def test_extract_text_recurses_nested_multipart():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [{
            "mimeType": "multipart/alternative",
            "parts": [{"mimeType": "text/plain", "body": {"data": b64("nested body")}}],
        }],
    }
    assert gmail_mod._extract_text(payload) == "nested body"


def test_search_email_wraps_results_and_calls_metadata(tmp_path, audit):
    service = MagicMock()
    service.users().messages().list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}]
    }
    service.users().messages().get.return_value.execute.return_value = {
        "payload": {"headers": [
            {"name": "From", "value": "a@b.com"}, {"name": "Subject", "value": "Hi"},
        ]},
        "snippet": "preview text",
    }
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    out = gmail_mod.build_tools(ctx)[0]("pump leak")
    assert "untrusted data" in out
    assert "a@b.com" in out and "preview text" in out
    service.users().messages().get.assert_called_with(
        userId="me", id="m1", format="metadata", metadataHeaders=["From", "Subject", "Date"]
    )


def test_read_email_formats_headers_and_body(tmp_path, audit):
    service = MagicMock()
    service.users().messages().get.return_value.execute.return_value = {
        "payload": {
            "headers": [
                {"name": "From", "value": "boss@x.com"}, {"name": "Subject", "value": "Report"},
            ],
            "mimeType": "text/plain", "body": {"data": b64("please send the report")},
        }
    }
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["read_email"]("m1")
    assert "boss@x.com" in out and "please send the report" in out


def test_send_email_still_confirms(tmp_path, audit):
    service = MagicMock()
    ctx = make_ctx(tmp_path, audit, ["allow once", "no"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["send_email"]("x@y.com", "Subj", "body")
    assert "Cancelled" in out
    service.users().messages().send.assert_not_called()


def test_organize_archive_removes_inbox_label(tmp_path, audit):
    service = MagicMock()
    ctx = make_ctx(tmp_path, audit, ["allow once", "yes"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["organize_email"]("m1", "archive")
    assert "Done" in out
    service.users().messages().modify.assert_called_with(
        userId="me", id="m1", body={"addLabelIds": [], "removeLabelIds": ["INBOX"]}
    )


def test_organize_mark_unread_also_requires_confirmation(tmp_path, audit):
    # Deliberate design choice: even the highest-frequency, fully-reversible
    # organize action (mark read/unread) is confirmed, no special-casing.
    service = MagicMock()
    ctx = make_ctx(tmp_path, audit, ["allow once", "no"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["organize_email"]("m1", "mark_read")
    assert "Cancelled" in out
    service.users().messages().modify.assert_not_called()


def test_organize_custom_label_resolves_id_and_caches(tmp_path, audit):
    service = MagicMock()
    service.users().labels().list.return_value.execute.return_value = {
        "labels": [{"id": "Label_42", "name": "Vendors"}]
    }
    ctx = make_ctx(tmp_path, audit, ["allow once", "yes", "yes"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    tools["organize_email"]("m1", "add_label", label="Vendors")
    tools["organize_email"]("m2", "add_label", label="Vendors")
    service.users().messages().modify.assert_any_call(
        userId="me", id="m1", body={"addLabelIds": ["Label_42"], "removeLabelIds": []}
    )
    # labels().list() fetched once and cached across both calls.
    assert service.users().labels().list.call_count == 1


def test_organize_unknown_label_reports_clearly(tmp_path, audit):
    service = MagicMock()
    service.users().labels().list.return_value.execute.return_value = {"labels": []}
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["organize_email"]("m1", "add_label", label="Nope")
    assert "No label named" in out


def test_organize_trash_uses_trash_endpoint(tmp_path, audit):
    service = MagicMock()
    ctx = make_ctx(tmp_path, audit, ["allow once", "yes"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    tools["organize_email"]("m1", "trash")
    service.users().messages().trash.assert_called_with(userId="me", id="m1")


def test_organize_unknown_action_is_rejected(tmp_path, audit):
    service = MagicMock()
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["organize_email"]("m1", "delete_forever")
    assert "Unknown action" in out


def test_custom_label_named_like_a_system_label_wins_over_the_alias(tmp_path, audit):
    # An account can have a custom label literally named "Trash". Silently
    # treating that as the system TRASH label would trash the message under
    # a confirmation that only ever said "add the label 'Trash'".
    service = MagicMock()
    service.users().labels().list.return_value.execute.return_value = {
        "labels": [{"id": "Label_999_custom_trash", "name": "Trash"}]
    }
    ctx = make_ctx(tmp_path, audit, ["allow once", "yes"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["organize_email"]("m1", "add_label", label="Trash")
    assert "Done" in out
    service.users().messages().modify.assert_called_with(
        userId="me", id="m1",
        body={"addLabelIds": ["Label_999_custom_trash"], "removeLabelIds": []},
    )


def test_label_lookup_is_case_insensitive(tmp_path, audit):
    service = MagicMock()
    service.users().labels().list.return_value.execute.return_value = {
        "labels": [{"id": "Label_1", "name": "Vendors"}]
    }
    ctx = make_ctx(tmp_path, audit, ["allow once", "yes"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["organize_email"]("m1", "add_label", label="vendors")
    assert "Done" in out
    service.users().messages().modify.assert_called_with(
        userId="me", id="m1", body={"addLabelIds": ["Label_1"], "removeLabelIds": []}
    )


def test_label_lookup_failure_is_caught_and_audited(tmp_path, audit):
    service = MagicMock()
    service.users().labels().list.return_value.execute.side_effect = RuntimeError("network down")
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["organize_email"]("m1", "add_label", label="Vendors")
    assert "failed" in out.lower()
    assert any(e["tool"] == "organize_email" and not e["ok"] for e in audit.tail(5))


def test_declined_send_relays_the_correction_to_the_model(tmp_path, audit):
    service = MagicMock()
    ctx = make_ctx(tmp_path, audit, ["allow once", "no, send it to priya instead"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["send_email"]("mohit@x.com", "Subj", "body")
    assert "Cancelled" in out
    assert "priya instead" in out          # the correction rides back
    assert "adjust the action" in out      # with instructions to retry
    service.users().messages().send.assert_not_called()


def _attach_ctx(tmp_path, audit, answers, service):
    io = FakeIO(answers)
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    docs = tmp_path / "docs"; docs.mkdir()
    cfg = Config(raw={"files": {"allowed_dirs": [str(docs)]}})
    ctx = ToolContext(config=cfg, permissions=pm,
                      confirmer=Confirmer(io, audit), audit=audit)
    ctx.google_service = lambda api, version: service
    return ctx, docs, io


def test_send_email_with_attachment_from_allowed_dir(tmp_path, audit):
    service = MagicMock()
    ctx, docs, io = _attach_ctx(tmp_path, audit, ["allow once", "yes"], service)
    report = docs / "report.pdf"
    report.write_bytes(b"%PDF-1.4 fake")
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["send_email"]("priya@x.com", "Report", "attached.", attach_path=str(report))
    assert "sent" in out.lower()
    # the confirmation must name the attachment
    assert any("report.pdf" in q for q in io.asked)
    # and the MIME payload must actually contain it
    _, kwargs = service.users().messages().send.call_args
    raw = base64.urlsafe_b64decode(kwargs["body"]["raw"] + "===")
    assert b"report.pdf" in raw and b"%PDF-1.4" not in raw[:200]  # attached, not inline


def test_attachment_outside_allowlist_is_blocked(tmp_path, audit):
    service = MagicMock()
    ctx, docs, _ = _attach_ctx(tmp_path, audit, ["allow once"], service)
    secret = tmp_path / "outside.txt"
    secret.write_text("secret")
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["send_email"]("x@y.com", "S", "b", attach_path=str(secret))
    assert "Blocked" in out
    service.users().messages().send.assert_not_called()


def test_missing_attachment_is_reported(tmp_path, audit):
    service = MagicMock()
    ctx, docs, _ = _attach_ctx(tmp_path, audit, ["allow once"], service)
    tools = {t.name: t for t in gmail_mod.build_tools(ctx)}
    out = tools["send_email"]("x@y.com", "S", "b", attach_path=str(docs / "nope.pdf"))
    assert "does not exist" in out
    service.users().messages().send.assert_not_called()
