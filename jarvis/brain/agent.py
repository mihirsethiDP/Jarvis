"""The Jarvis brain: a Claude tool-use loop.

Uses the Anthropic SDK's tool runner — each decorated tool executes
automatically, and every tool performs its own permission/confirmation
gating, so a denied action simply comes back to the model as a normal tool
result it must respect.
"""

from __future__ import annotations

from typing import Callable

import anthropic

from ..config import Config
from ..security import AuditLog
from .prompts import build_system_prompt


class JarvisAgent:
    def __init__(
        self,
        config: Config,
        tools: list,
        audit: AuditLog,
        *,
        on_status: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.tools = tools
        self.audit = audit
        self.on_status = on_status or (lambda _state: None)
        self.client = anthropic.Anthropic()
        self.system_prompt = build_system_prompt(
            str(config.get("assistant.name", "Jarvis"))
        )
        self.messages: list[dict] = []

    # ------------------------------------------------------------------
    def run_turn(self, user_text: str) -> str:
        """Run one conversational turn and return the text to speak."""
        self._trim_history()
        # A failure can strike mid-tool-loop, after assistant/tool_result pairs
        # were already mirrored; rolling back to the checkpoint (not popping
        # once) guarantees no dangling tool_use is left to 400 every later turn.
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": user_text})
        self.on_status("thinking")

        try:
            reply = self._run_tool_loop()
        except anthropic.AuthenticationError:
            del self.messages[checkpoint:]
            return (
                "My Claude API key is missing or invalid. "
                "Please set ANTHROPIC_API_KEY and restart me."
            )
        except anthropic.RateLimitError:
            del self.messages[checkpoint:]
            return "I'm being rate limited right now — give me a moment and try again."
        except anthropic.APIConnectionError:
            del self.messages[checkpoint:]
            return "I can't reach the Claude API — please check the network connection."
        except anthropic.APIStatusError as e:
            del self.messages[checkpoint:]
            self.audit.record("error", tool="brain", detail=str(e), ok=False)
            return "Something went wrong talking to my language model. Please try again."

        self.audit.record("turn", tool="brain", detail=user_text[:200])
        return reply

    # ------------------------------------------------------------------
    def _run_tool_loop(self) -> str:
        runner = self.client.beta.messages.tool_runner(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=self.system_prompt,
            thinking={"type": "adaptive"},
            output_config={"effort": self.config.effort},
            tools=self.tools,
            messages=list(self.messages),
        )

        last = None
        for message in runner:
            last = message
            # Mirror the runner's internal history into ours so multi-turn
            # context (including tool calls) survives across turns.
            self.messages.append({"role": "assistant", "content": message.content})
            if message.stop_reason == "refusal":
                # Refusal turns are terminal — executing their tool_use blocks
                # would fire side effects the model never confirmed.
                continue
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                self.messages.append(tool_response)
                for block in message.content:
                    if block.type == "tool_use":
                        self.on_status(f"tool:{block.name}")

        if last is None:
            return "I didn't get a response — please try again."
        if last.stop_reason == "refusal":
            return "I can't help with that request."

        text = " ".join(b.text for b in last.content if b.type == "text").strip()
        return text or "Done."

    # ------------------------------------------------------------------
    def _trim_history(self) -> None:
        """Bound the conversation window, cutting only at plain user turns so
        tool_use/tool_result pairs are never orphaned."""
        max_turns = self.config.max_history_turns
        user_turn_indexes = [
            i
            for i, m in enumerate(self.messages)
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        ]
        if len(user_turn_indexes) <= max_turns:
            return
        cut_at = user_turn_indexes[len(user_turn_indexes) - max_turns]
        self.messages = self.messages[cut_at:]

    def reset(self) -> None:
        self.messages.clear()
