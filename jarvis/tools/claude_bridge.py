"""Ask Claude directly — drafting, summarising, rewriting, explaining.

This is Jarvis handing a self-contained prompt to a *fresh* Claude context
and reading the answer back. It is deliberately NOT Claude Code:

- No tools are passed, so the sub-call cannot read files, run shell
  commands, call APIs, or reach the network beyond the model itself.
- No conversation history goes with it — the caller must put everything
  needed into the prompt, which also keeps company context from leaking
  into a request that didn't need it.
- The answer comes back wrapped as untrusted data, exactly like a fetched
  document, so a prompt-injected reply carries no authority.

Voice-triggered arbitrary code execution is the thing every other layer in
Jarvis exists to prevent; routing through a shell-capable agent would undo
that in one hop. Drafting help is the useful part, and this is it.
"""

from __future__ import annotations

import anthropic
from anthropic import beta_tool

from . import ToolContext, as_document

_MAX_OUTPUT_TOKENS = 2048


def build_tools(ctx: ToolContext) -> list:
    @beta_tool
    def ask_claude(prompt: str) -> str:
        """Ask Claude a self-contained question — drafting, summarising,
        rewriting, brainstorming, explaining. Use when the user wants text
        produced or reasoned about rather than an action taken.

        The prompt gets a fresh context with no tools and no history, so
        include everything needed to answer inside it. Don't use this for
        anything Jarvis's own tools already do (mail, calendar, files) —
        call those directly instead.

        Args:
            prompt: The complete, self-contained prompt to send.
        """
        text = prompt.strip()
        if not text:
            return "The prompt was empty."
        if not ctx.permissions.require(
            "ask_claude", "send a prompt to Claude on your behalf"
        ):
            return "The user declined."

        # Counts against the same daily brake as ordinary turns, so this
        # can't become a side channel around the spend limit.
        budget = getattr(ctx, "turn_budget", None)
        if budget is not None:
            if not budget.allow():
                return ("That would exceed today's usage limit, so I've stopped "
                        "short of sending it.")
            budget.record()

        try:
            client = anthropic.Anthropic()
            response = client.messages.create(
                model=str(ctx.config.get("brain.model", "claude-sonnet-5")),
                max_tokens=_MAX_OUTPUT_TOKENS,
                messages=[{"role": "user", "content": text}],
            )
            answer = "".join(b.text for b in response.content if b.type == "text").strip()
        except anthropic.APIStatusError as e:
            ctx.audit.record("tool_call", tool="ask_claude", detail=str(e)[:120], ok=False)
            return f"Asking Claude failed: {e}"
        except Exception as e:
            ctx.audit.record("tool_call", tool="ask_claude", detail=str(e)[:120], ok=False)
            return f"Asking Claude failed: {e}"

        ctx.audit.record("tool_call", tool="ask_claude",
                         detail=f"{len(text)} chars in, {len(answer)} out", decision="ok")
        return as_document("claude", answer or "(no answer)")

    return [ask_claude]
