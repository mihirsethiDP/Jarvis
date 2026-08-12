"""The status page as a control surface, not just a display.

Typed requests, push-to-talk, and answering a confirmation all reach the
assistant over loopback HTTP. Loopback is reachable from any page the
employee happens to have open, so every one of these is same-origin gated —
the same rule as /quit.
"""

from __future__ import annotations

import threading

import pytest

from jarvis.io_channel import SwitchableIO, TextIO, WebIO


class Recorder:
    def __init__(self):
        self.said = []
        self.prompts = []


def test_web_io_blocks_until_the_page_answers():
    r = Recorder()
    io = WebIO(publish_say=r.said.append, publish_prompt=r.prompts.append, timeout=5)

    result = {}
    t = threading.Thread(target=lambda: result.update(answer=io.ask("Send it?")))
    t.start()
    # The prompt reaches the page before any answer exists.
    for _ in range(50):
        if r.prompts:
            break
        threading.Event().wait(0.02)
    assert r.prompts == ["Send it?"]
    io.deliver("yes")
    t.join(timeout=5)
    assert result["answer"] == "yes"


def test_web_io_fails_closed_when_nobody_answers():
    r = Recorder()
    io = WebIO(publish_say=r.said.append, publish_prompt=r.prompts.append, timeout=0.2)
    # Silence on the page must read like silence on the microphone: the
    # security gates treat an empty answer as a refusal.
    assert io.ask("Send it?") == ""


def test_switchable_io_routes_a_typed_turn_to_the_page():
    r = Recorder()
    console = TextIO()
    web = WebIO(publish_say=r.said.append, publish_prompt=r.prompts.append, timeout=0.2)
    io = SwitchableIO(console)

    io.use(web)
    io.say("working on it")
    assert r.said == ["working on it"], "a typed turn was answered on the wrong channel"

    io.use(None)
    assert io.current is console


# -- endpoint gating -----------------------------------------------------
@pytest.fixture
def server():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.server import StateServer

    seen = {"asked": [], "listened": 0, "answered": []}
    s = StateServer(
        port=8763,
        on_ask=lambda t: (seen["asked"].append(t) or True),
        on_listen=lambda: (seen.__setitem__("listened", seen["listened"] + 1) or True),
        on_answer=seen["answered"].append,
    )
    return TestClient(s._app), seen


GOOD = {"Origin": "http://127.0.0.1:8763"}
BAD = {"Origin": "https://evil.example"}


@pytest.mark.parametrize("path,body", [
    ("/ask", {"text": "hello"}),
    ("/listen", {}),
    ("/answer", {"answer": "yes"}),
])
def test_a_foreign_page_cannot_drive_jarvis(server, path, body):
    client, seen = server
    assert client.post(path, json=body, headers=BAD).status_code == 403
    assert seen["asked"] == [] and seen["listened"] == 0 and seen["answered"] == []


def test_our_own_page_can(server):
    client, seen = server
    assert client.post("/ask", json={"text": "what is on my calendar"},
                       headers=GOOD).status_code == 200
    assert client.post("/listen", json={}, headers=GOOD).status_code == 200
    assert client.post("/answer", json={"answer": "yes"}, headers=GOOD).status_code == 200
    assert seen["asked"] == ["what is on my calendar"]
    assert seen["listened"] == 1
    assert seen["answered"] == ["yes"]


def test_empty_and_oversized_requests_are_rejected(server):
    client, _ = server
    assert client.post("/ask", json={"text": "   "}, headers=GOOD).status_code == 400
    assert client.post("/ask", json={"text": "x" * 5000}, headers=GOOD).status_code == 400


def test_a_second_request_while_busy_is_refused_not_queued():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.server import StateServer

    # Two turns interleaved would share one conversation history.
    s = StateServer(port=8763, on_ask=lambda t: False)
    client = TestClient(s._app)
    assert client.post("/ask", json={"text": "hi"}, headers=GOOD).status_code == 409
