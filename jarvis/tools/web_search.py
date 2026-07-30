"""Web search — for anything past the model's knowledge cutoff.

News, current affairs, "what happened with X", recent prices, a company's
latest announcement. Runs Claude's server-side web search in a fresh
sub-call: Anthropic performs the searches, so nothing here needs a separate
search API key or account.

Two properties matter for Jarvis's threat model:

- The answer, and the pages behind it, are **untrusted internet content**.
  It comes back wrapped as data, so a poisoned page cannot issue
  instructions — same boundary as a Drive document or a Chat message.
- It costs an API call, so it is permission-gated and counted against the
  same daily budget as ordinary turns; it can't become a way around the
  spend brake.
"""

from __future__ import annotations

import anthropic
from anthropic import beta_tool

from . import ToolContext, as_document

# Dynamic-filtering variant — supported on Opus 5/4.8/4.7/4.6 and Sonnet 5/4.6.
# Filtering runs server-side; do NOT also declare code_execution alongside it.
_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
_LEGACY_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}
_MAX_SEARCHES = 5
_MAX_OUTPUT_TOKENS = 2048
_MAX_RESUMES = 3


def build_tools(ctx: ToolContext) -> list:
    @beta_tool
    def search_web(query: str) -> str:
        """Search the web and return an answer with sources. Use for anything
        current or beyond your knowledge: news, current affairs, recent
        events, today's prices, a company's latest announcement.

        Don't use it for things you already know, for the user's own data
        (that's what the Gmail/Drive/Calendar tools are for), or for the
        weather (use get_weather).

        Args:
            query: What to find out, as a complete question.
        """
        question = query.strip()
        if not question:
            return "What should I search for?"
        if not ctx.permissions.require("web_search", "search the web"):
            return "The user declined web search."

        budget = getattr(ctx, "turn_budget", None)
        if budget is not None:
            if not budget.allow():
                return ("That would exceed today's usage limit, so I've stopped "
                        "short of searching.")
            budget.record()

        model = str(ctx.config.get("brain.model", "claude-sonnet-5"))
        client = anthropic.Anthropic()
        messages: list = [{"role": "user", "content": question}]

        def ask(tool_def):
            return client.messages.create(
                model=model,
                max_tokens=_MAX_OUTPUT_TOKENS,
                tools=[{**tool_def, "max_uses": _MAX_SEARCHES}],
                messages=messages,
            )

        try:
            try:
                response = ask(_SEARCH_TOOL)
            except anthropic.BadRequestError:
                # Older models only accept the pre-dynamic-filtering variant.
                response = ask(_LEGACY_SEARCH_TOOL)

            # A long search turn can stop with pause_turn; the server resumes
            # it if we send the partial assistant turn straight back.
            resumes = 0
            while response.stop_reason == "pause_turn" and resumes < _MAX_RESUMES:
                messages.append({"role": "assistant", "content": response.content})
                response = ask(_SEARCH_TOOL)
                resumes += 1
        except anthropic.APIStatusError as e:
            ctx.audit.record("tool_call", tool="search_web", detail=str(e)[:120], ok=False)
            return f"The search failed: {e}"
        except Exception as e:
            ctx.audit.record("tool_call", tool="search_web", detail=str(e)[:120], ok=False)
            return f"The search failed: {e}"

        if response.stop_reason == "refusal":
            return "I can't search for that."

        answer = " ".join(b.text for b in response.content if b.type == "text").strip()

        # Collect sources so the reply can be attributed. On failure the
        # result block's content is an error *object*, not a list of results.
        sources: list[str] = []
        searched = 0
        for block in response.content:
            if block.type != "web_search_tool_result":
                continue
            searched += 1
            results = block.content
            if isinstance(results, list):
                for item in results[:4]:
                    title = getattr(item, "title", "") or ""
                    url = getattr(item, "url", "") or ""
                    if url:
                        sources.append(f"- {title.strip()[:90]} ({url})")

        if not answer:
            return "The search came back empty — try rewording the question."
        body = answer + ("\n\nSources:\n" + "\n".join(sources) if sources else "")
        ctx.audit.record("tool_call", tool="search_web",
                         detail=f"{question[:80]} ({searched} search call(s))",
                         decision="ok")
        return as_document(f"web-search:{question[:60]}", body)

    return [search_web]
