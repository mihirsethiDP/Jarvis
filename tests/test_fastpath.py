"""The intent fast-path: frequent commands answered without the LLM.

Hard rule under test: CONSERVATIVE. A wrong fast-path answer costs trust; a
missed one costs seconds. Every ambiguous shape must return None and let the
model handle it exactly as before.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from jarvis.brain import fastpath


# -- chat send: the marquee flow, in the shapes the user actually says ----
@pytest.mark.parametrize("text,person,body", [
    ("message send kardo deeksha ko ki mai kal late aaunga",
     "deeksha", "mai kal late aaunga"),
    ("Deeksha ko message bhejo ki main late aaunga",
     "Deeksha", "main late aaunga"),
    ("yaar, google chat pe message bhejna hai Mansi Jain ko ki mai kal late ho jaunga",
     "Mansi Jain", "mai kal late ho jaunga"),
    ("send a message to Mohit Joshi that the meeting moved to 4",
     "Mohit Joshi", "the meeting moved to 4"),
    # what STT actually writes: Devanagari with की for कि
    ("दीक्षा को मैसेज भेज दो की मैं कल लेट आऊँगा",
     "दीक्षा", "मैं कल लेट आऊँगा"),
    ("मैसेज भेजो रंजना को कि रिपोर्ट कल मिलेगी",
     "रंजना", "रिपोर्ट कल मिलेगी"),
])
def test_chat_send_shapes_match(text, person, body):
    intent = fastpath.match(text)
    assert intent is not None, text
    assert intent["kind"] == "chat_send"
    assert intent["person"] == person
    assert intent["body"] == body


@pytest.mark.parametrize("text", [
    # Names that fail the sanity bound must NOT fast-path.
    "message bhejo sabko ki kal chhutti hai",              # "everyone"
    "message send kardo team ko ki standup cancel",        # a group word
    "message bhejo mere manager aur uske boss ko ki done", # 4+ words
    # Sentences that only look nearby.
    "kal Deeksha ko message bheja tha kya",                # a question about the past
    "email bhejo Deeksha ko ki mai late aaunga",           # email, not chat: model's job
    "draft a message to the client about the delay",       # no ki/that split
])
def test_ambiguous_shapes_fall_through(text):
    assert fastpath.match(text) is None, text


def test_time_and_date_answer_locally():
    now = datetime(2026, 9, 21, 15, 42)
    assert fastpath.match("what time is it")["kind"] == "time"
    assert fastpath.match("kitne baje hai")["kind"] == "time"
    assert fastpath.match("कितने बजे हैं")["kind"] == "time"
    assert fastpath.match("aaj kya date hai")["kind"] == "date"
    assert "3:42" in fastpath.answer_time(False, now)
    assert "15:42" in fastpath.answer_time(True, now)
    assert "September" in fastpath.answer_date(False, now)


def test_ordinary_requests_are_not_time_questions():
    assert fastpath.match("what time is my meeting with Ranjana") is None
    assert fastpath.match("kitne baje meeting hai") is None


# -- model routing ----------------------------------------------------------
@pytest.mark.parametrize("text", [
    "how many unread emails do I have",
    "kitne unread emails hai",
    "check my calendar for tomorrow",
    "kya koi naya message aaya chat pe",
])
def test_read_only_lookups_route_to_the_fast_model(text):
    assert fastpath.wants_fast_model(text), text


@pytest.mark.parametrize("text", [
    "send an email to the client about the delay",
    "check my mail and reply to Ranjana",         # chains into a send
    "summarize this week's emails and draft an update",
    "kal ki meeting cancel kar do",
])
def test_composing_or_chaining_stays_on_the_main_model(text):
    assert not fastpath.wants_fast_model(text), text


# -- execution through the gated tools -------------------------------------
class _App:
    """Just enough of JarvisApp to run its fast-path methods."""

    from jarvis.app import JarvisApp as _J

    _try_fastpath = _J._try_fastpath
    _fastpath_chat_send = _J._fastpath_chat_send

    def __init__(self, tools):
        self._tool_map = tools
        self.narrated = []
        self.config = type("C", (), {"get": staticmethod(
            lambda key, default=None: default)})()

        class _Audit:
            def __init__(self):
                self.entries = []

            def record(self, *a, **k):
                self.entries.append((a, k))

        self.audit = _Audit()

    def _narrate(self, phrase):
        self.narrated.append(phrase)


def test_fastpath_sends_through_the_real_tools():
    calls = []

    def find(person):
        calls.append(("find", person))
        return ("Direct message with Deeksha Chaturvedi: space id "
                "spaces/zqVh_8AAAAE. Use send_chat_message with this space id.")

    def send(space_id, body):
        calls.append(("send", space_id, body))
        return "Message sent to Deeksha Chaturvedi (direct message): spaces/x/messages/1"

    app = _App({"find_direct_message": find, "send_chat_message": send})
    reply = app._try_fastpath("message send kardo deeksha ko ki mai kal late aaunga")
    assert calls == [
        ("find", "deeksha"),
        ("send", "spaces/zqVh_8AAAAE", "mai kal late aaunga"),
    ]
    assert reply == "Sent to Deeksha Chaturvedi (direct message)."


def test_unresolved_person_falls_through_to_the_model():
    app = _App({"find_direct_message": lambda p: "AMBIGUOUS: 2 people match",
                "send_chat_message": lambda s, t: "unused"})
    assert app._try_fastpath("message bhejo Mansi ko ki der ho jayegi") is None


def test_denied_capability_falls_through():
    # Standing denial removes the tool from the map entirely.
    app = _App({})
    assert app._try_fastpath("Deeksha ko message bhejo ki late aaunga") is None


def test_plain_decline_answers_without_the_model():
    app = _App({
        "find_direct_message": lambda p: "Direct message with D: space id spaces/a.",
        "send_chat_message": lambda s, t: "Cancelled — the user did not confirm it.",
    })
    reply = app._try_fastpath("message bhejo Deeksha ko ki late aaunga")
    assert reply == "Okay — not sent."


def test_decline_with_correction_goes_to_the_model():
    app = _App({
        "find_direct_message": lambda p: "Direct message with D: space id spaces/a.",
        "send_chat_message": lambda s, t: (
            'Cancelled — the user did not confirm it. They said: "no, Priya ko". '
            "Treat that as a correction."),
    })
    assert app._try_fastpath("message bhejo Deeksha ko ki late aaunga") is None
