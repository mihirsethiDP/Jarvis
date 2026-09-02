"""The DigitalPaani email house style, served to the drafting model.

Source: the CS team's email-assistant context, built from real Mohit Joshi
emails — the company writes to clients as one voice. What deliberately does
NOT carry over is the hardcoded identity: Jarvis signs as the signed-in user.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jarvis.config import Config
from jarvis.email_style import TEMPLATES, VOICE_RULES, signoff
from jarvis.memory import MemoryStore
from jarvis.security import AuditLog
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext, email_style_tool
from jarvis.tools import directory as directory_mod

from conftest import FakeIO


def test_registry_is_complete_and_consistent():
    assert len(TEMPLATES) >= 24
    for key, (title, tone, variables, body) in TEMPLATES.items():
        assert title and variables and body, key
        assert tone in ("", "Friendly", "Professional", "Formal",
                        "Apologetic", "Urgent"), (key, tone)
        assert "Subject:" in body, key
        assert "[SIGN-OFF]" in body or "[WELCOME SIGN-OFF]" in body, key


def test_fixed_tones_match_the_source_material():
    assert TEMPLATES["welcome_om"][1] == "Friendly"
    assert TEMPLATES["off_track"][1] == "Apologetic"
    assert TEMPLATES["issue_major"][1] == "Urgent"
    assert TEMPLATES["onboarding_update"][1] == "Formal"
    assert TEMPLATES["renewal"][1] == "Urgent"


def test_voice_rules_carry_the_banned_phrases():
    for phrase in ("touch base", "circle back", "synergy",
                   "as per my last", "hope this email finds you well"):
        assert phrase in VOICE_RULES.lower()


def test_signoff_signs_as_the_user_not_the_source_author():
    block = signoff("Mihir Sethi", "9999999999")
    assert "Mihir Sethi" in block and "Mohit Joshi" not in block
    assert block.startswith("Thanks & Regards")
    welcome = signoff("Mihir Sethi", welcome=True)
    assert welcome.startswith("Best regards,")


def test_signoff_omits_lines_with_no_data():
    block = signoff("Mihir Sethi")
    assert "[" not in block and "Ph:" not in block
    assert block.splitlines() == ["Thanks & Regards", "Mihir Sethi", "DigitalPaani"]


# -- the tool -------------------------------------------------------------
@pytest.fixture
def tool(tmp_path, audit, monkeypatch):
    directory_mod._cache.update(at=float("inf"), people=[
        {"resourceName": "people/1",
         "names": [{"displayName": "Mihir Sethi"}],
         "emailAddresses": [{"value": "mihir.sethi@digitalpaani.com"}],
         "phoneNumbers": [{"value": "9876543210"}]},
    ])
    service = MagicMock()
    service.users().getProfile.return_value.execute.return_value = {
        "emailAddress": "mihir.sethi@digitalpaani.com"}
    io = FakeIO([])
    ctx = ToolContext(config=Config(raw={}),
                      permissions=PermissionManager(io, audit, store_path=tmp_path / "p.json"),
                      confirmer=Confirmer(io, audit), audit=audit, memory=MemoryStore(path=tmp_path / "m.json"))
    ctx.google_service = lambda api, version: service
    yield {t.name: t for t in email_style_tool.build_tools(ctx)}["email_template"]
    directory_mod._cache.update(at=0.0, people=None)


def test_payment_template_keeps_the_invoice_table(tool):
    out = tool("payment_followup")
    assert "Date        | Bill No." in out
    assert "Total Amount" in out
    assert "gentle reminder" in out


def test_sender_is_resolved_from_the_signed_in_account(tool):
    out = tool("off_track")
    assert "Mihir Sethi" in out
    assert "9876543210" in out
    assert "Mohit Joshi" not in out, "the source author's identity must never leak"


def test_fixed_tone_is_stated(tool):
    assert "Required tone: Apologetic" in tool("issue_ack_reactive")


def test_escalation_matrix_only_where_relevant(tool):
    assert "Hemant Maheshwari" in tool("issue_major")
    assert "Hemant Maheshwari" not in tool("payment_followup")


def test_unknown_template_suggests_and_lists(tool):
    out = tool("onboarding")
    assert "onboarding_plan" in out and "onboarding_update" in out
    listing = tool("list")
    assert "welcome_om" in listing and "escalation_response" in listing
