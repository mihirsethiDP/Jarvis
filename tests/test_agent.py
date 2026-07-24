from __future__ import annotations

import anthropic
import httpx
import pytest

from jarvis.brain.agent import JarvisAgent
from jarvis.config import Config
from jarvis.security.audit import AuditLog


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    cfg = Config(raw={"brain": {"max_history_turns": 2}})
    return JarvisAgent(cfg, tools=[], audit=AuditLog(path=tmp_path / "a.jsonl"))


def test_api_error_rolls_back_all_partial_history(agent, monkeypatch):
    agent.messages = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]

    def boom():
        # Simulate a failure mid tool loop, after partial mirroring.
        agent.messages.append({"role": "assistant", "content": [{"type": "tool_use"}]})
        agent.messages.append({"role": "user", "content": [{"type": "tool_result"}]})
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.test"))

    monkeypatch.setattr(agent, "_run_tool_loop", boom)
    reply = agent.run_turn("new question")

    assert "reach" in reply  # the connection-error message
    # No trace of the failed turn — especially no dangling tool_use.
    assert agent.messages == [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]


def test_trim_cuts_only_at_plain_user_turns(agent):
    agent.messages = [
        {"role": "user", "content": "turn one"},
        {"role": "assistant", "content": [{"type": "tool_use"}]},
        {"role": "user", "content": [{"type": "tool_result"}]},  # not a plain turn
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "turn two"},
        {"role": "assistant", "content": "answer two"},
        {"role": "user", "content": "turn three"},
        {"role": "assistant", "content": "answer three"},
    ]
    agent._trim_history()  # max_history_turns=2 -> keep from "turn two"
    assert agent.messages[0] == {"role": "user", "content": "turn two"}
    assert len(agent.messages) == 4
