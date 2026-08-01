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


def test_unconfigured_chat_app_error_gets_actionable_hint(tmp_path, audit):
    # Verbatim shape of the real 404 Google returns when the Chat API app
    # configuration step hasn't been done.
    service = MagicMock()
    service.spaces().messages().create.return_value.execute.side_effect = RuntimeError(
        '<HttpError 404 ... returned "Google Chat app not found. To create a Chat '
        'app, you must turn on the Chat API and configure the app in the Google '
        'Cloud console.">'
    )
    ctx = make_ctx(tmp_path, audit, ["allow once", "yes"], service)
    out = {t.name: t for t in gchat_mod.build_tools(ctx)}["send_chat_message"]("spaces/A", "hi")
    assert "Configuration tab" in out
    assert "Reading Chat works without it" in out


# -- person -> direct message -------------------------------------------
# Reported from real use: "I have a chat/DM with Ranjana Majumdar but Jarvis
# came back stating that he cannot find any space with her." Confirmed against
# the live account: all 30 DMs return displayName=None, so name matching can
# never find one, and spaces.list was reading only the first of three pages.

def _dual_service(chat, people):
    """google_service() dispatches by api name; DMs need both."""
    return lambda api, version: people if api == "people" else chat


def _directory(*entries):
    people = MagicMock()
    people.people().searchDirectoryPeople.return_value.execute.return_value = {
        "people": [
            {"resourceName": f"people/{pid}",
             "names": [{"displayName": name}],
             "emailAddresses": [{"value": email}]}
            for pid, name, email in entries
        ]
    }
    return people


def test_direct_message_is_found_by_person_name(tmp_path, audit):
    chat = MagicMock()
    chat.spaces().findDirectMessage.return_value.execute.return_value = {
        "name": "spaces/0LhmHiAAAAE", "spaceType": "DIRECT_MESSAGE"}
    people = _directory(("113180519160746191627", "Ranjana Majumdar",
                         "ranjana.majumdar@digitalpaani.com"))
    ctx = make_ctx(tmp_path, audit, ["allow once", "allow once"], chat)
    ctx.google_service = _dual_service(chat, people)

    out = {t.name: t for t in gchat_mod.build_tools(ctx)}["find_direct_message"]("Ranjana")
    assert "spaces/0LhmHiAAAAE" in out
    assert "Ranjana Majumdar" in out
    # The People resource id must be handed to Chat as a users/ id.
    chat.spaces().findDirectMessage.assert_called_with(
        name="users/113180519160746191627")


def test_two_people_with_the_same_name_are_never_guessed(tmp_path, audit):
    chat = MagicMock()
    people = _directory(("1", "Priya Rao", "priya.rao@digitalpaani.com"),
                        ("2", "Priya Nair", "priya.nair@digitalpaani.com"))
    ctx = make_ctx(tmp_path, audit, ["allow once", "allow once"], chat)
    ctx.google_service = _dual_service(chat, people)

    out = {t.name: t for t in gchat_mod.build_tools(ctx)}["find_direct_message"]("Priya")
    assert "AMBIGUOUS" in out
    assert "priya.rao@digitalpaani.com" in out and "priya.nair@digitalpaani.com" in out
    chat.spaces().findDirectMessage.assert_not_called()


def test_no_existing_dm_explains_instead_of_dead_ending(tmp_path, audit):
    chat = MagicMock()
    chat.spaces().findDirectMessage.return_value.execute.side_effect = RuntimeError(
        '<HttpError 404 ... "Requested entity was not found.">')
    people = _directory(("9", "Arun Kumar", "arun.kumar@digitalpaani.com"))
    ctx = make_ctx(tmp_path, audit, ["allow once", "allow once"], chat)
    ctx.google_service = _dual_service(chat, people)

    out = {t.name: t for t in gchat_mod.build_tools(ctx)}["find_direct_message"]("Arun")
    assert "no existing Chat direct message" in out
    assert "arun.kumar@digitalpaani.com" in out   # offers a usable alternative


def test_unknown_person_asks_for_more_detail(tmp_path, audit):
    chat = MagicMock()
    people = _directory()
    ctx = make_ctx(tmp_path, audit, ["allow once", "allow once"], chat)
    ctx.google_service = _dual_service(chat, people)
    out = {t.name: t for t in gchat_mod.build_tools(ctx)}["find_direct_message"]("Zoltan")
    assert "company directory" in out
    assert "full name or email" in out


def test_space_listing_follows_pagination(tmp_path, audit):
    # The live account has 274 spaces across 3 pages; reading page one only
    # reported spaces that exist as missing.
    chat = MagicMock()
    pages = [
        {"spaces": [{"name": "spaces/A", "displayName": "Page One Space",
                     "spaceType": "SPACE"}], "nextPageToken": "t1"},
        {"spaces": [{"name": "spaces/B", "displayName": "Plant Ops",
                     "spaceType": "SPACE"}]},
    ]
    chat.spaces().list.return_value.execute.side_effect = pages
    ctx = make_ctx(tmp_path, audit, ["allow once"], chat)
    out = {t.name: t for t in gchat_mod.build_tools(ctx)}["list_chat_spaces"]("plant")
    assert "Plant Ops" in out, "a space on page two was reported as missing"


def test_send_confirms_with_a_human_name_not_a_space_id(tmp_path, audit):
    # Confirming "send to spaces/0LhmHiAAAAE" cannot be checked by a human,
    # which makes the confirmation gate worthless.
    chat = MagicMock()
    chat.spaces().findDirectMessage.return_value.execute.return_value = {
        "name": "spaces/0LhmHiAAAAE", "spaceType": "DIRECT_MESSAGE"}
    chat.spaces().messages().create.return_value.execute.return_value = {
        "name": "spaces/0LhmHiAAAAE/messages/1"}
    people = _directory(("113", "Ranjana Majumdar", "ranjana.majumdar@digitalpaani.com"))
    io = FakeIO(["allow once", "allow once", "allow once", "yes"])
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    ctx = ToolContext(config=Config(raw={}), permissions=pm,
                      confirmer=Confirmer(io, audit), audit=audit)
    ctx.google_service = _dual_service(chat, people)
    tools = {t.name: t for t in gchat_mod.build_tools(ctx)}

    tools["find_direct_message"]("Ranjana")          # caches the human label
    out = tools["send_chat_message"]("spaces/0LhmHiAAAAE", "I'll join at four")
    assert "Ranjana Majumdar" in out
    asked = " ".join(io.asked)
    assert "Ranjana Majumdar" in asked, "the user was asked to confirm an opaque id"
