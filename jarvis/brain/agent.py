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
from ..memory import MemoryStore
from ..security import AuditLog
from .narration import describe_tool
from .prompts import build_system_prompt


class JarvisAgent:
    def __init__(
        self,
        config: Config,
        tools: list,
        audit: AuditLog,
        *,
        on_status: Callable[..., None] | None = None,
        on_narrate: Callable[[str], None] | None = None,
        memory: MemoryStore | None = None,
        recall_check: Callable[[], bool] | None = None,
        turn_budget=None,
    ):
        self.config = config
        self.tools = tools
        self.audit = audit
        self.on_status = on_status or (lambda *_a: None)
        # Spoken narration. Tool work is where the seconds go, and the user
        # otherwise sits in silence with no idea whether Jarvis is working,
        # stuck, or finished.
        self.on_narrate = on_narrate or (lambda _text: None)
        self._narrated: set[str] = set()
        # What the user switched off at setup, so Jarvis can explain the gap
        # instead of inventing a workaround or denying the ability exists.
        self.denied_capabilities: list[str] = []
        self.client = anthropic.Anthropic()
        self.name = str(config.get("assistant.name", "Jarvis"))
        self.memory = memory
        self._recall_check = recall_check or (lambda: True)
        self._recall_decision: bool | None = None
        self.turn_budget = turn_budget
        self.messages: list[dict] = []

    def _may_recall(self) -> bool:
        """Ask once per session, and only if there is actually something to
        recall — so a fresh install never prompts about an empty memory."""
        if self.memory is None or not self.memory.all():
            return False
        if self._recall_decision is None:
            self._recall_decision = bool(self._recall_check())
        return self._recall_decision

    def _system_prompt(self) -> str:
        """Rebuilt per turn so newly remembered facts — and revocations made
        from a console — take effect immediately."""
        block = self.memory.as_prompt_block() if self._may_recall() else ""
        return build_system_prompt(self.name, block, self.denied_capabilities)

    # ------------------------------------------------------------------
    def run_turn(self, user_text: str) -> str:
        """Run one conversational turn and return the text to speak."""
        # Hard spend brake — checked in code before any API call is made.
        if self.turn_budget is not None and not self.turn_budget.allow():
            self.audit.record("turn", tool="brain", detail="daily limit reached",
                              decision="budget_blocked", ok=False)
            return (
                f"I've reached today's safety limit of "
                f"{self.turn_budget.daily_limit} interactions, so I'm pausing "
                "until tomorrow. Raise brain.daily_turn_limit in my config if "
                "you need more."
            )
        if self.turn_budget is not None:
            self.turn_budget.record()  # count the attempt, not the success
        self._trim_history()
        # A failure can strike mid-tool-loop, after assistant/tool_result pairs
        # were already mirrored; rolling back to the checkpoint (not popping
        # once) guarantees no dangling tool_use is left to 400 every later turn.
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": user_text})
        # Per turn, not per session: the user needs telling every time work
        # starts, not only the first time this tool was ever used.
        self._narrated.clear()
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
            system=self._system_prompt(),
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
            mark = len(self.messages)
            self.messages.append({"role": "assistant", "content": message.content})
            if message.stop_reason == "refusal":
                # Refusal turns are terminal — executing their tool_use blocks
                # would fire side effects the model never confirmed. But the
                # message may *contain* tool_use blocks, and leaving those in
                # history with no matching tool_result makes the API reject
                # every later turn: one refusal used to brick the session
                # until restart. Roll it back instead.
                del self.messages[mark:]
                continue
            # Announce BEFORE running the tools, not after. Reporting a tool
            # once its result is already in hand tells the user nothing during
            # the wait, which is the only time they need telling.
            pending = [b for b in message.content if b.type == "tool_use"]
            for block in pending:
                phrase = describe_tool(block.name, getattr(block, "input", None) or {})
                # "tool:<name>" was published as if it were a *state*; the
                # status page knows no such state and fell back to idle, so
                # the display went quiet exactly when work started.
                self.on_status("working", phrase)
                if block.name not in self._narrated:
                    self._narrated.add(block.name)
                    self.on_narrate(phrase)

            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                self.messages.append(tool_response)

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
