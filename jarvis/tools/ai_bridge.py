"""Bridge to external AI tools.

Jarvis can forward a prompt to another AI system (an internal LLM gateway, a
vendor model, etc.) — but only systems the company has listed in config, and
only after the user grants access to that specific tool. Responses come back
wrapped as untrusted data.
"""

from __future__ import annotations

import os

import httpx
from anthropic import beta_tool

from . import ToolContext, as_document
from ..config import AIToolConfig

_TIMEOUT = 60.0


def _call_openai_compatible(tool: AIToolConfig, prompt: str) -> str:
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get(tool.api_key_env, "") if tool.api_key_env else ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = httpx.post(
        tool.base_url.rstrip("/") + "/chat/completions",
        json={
            "model": tool.model,
            "messages": [{"role": "user", "content": prompt}],
        },
        headers=headers,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _call_anthropic(tool: AIToolConfig, prompt: str) -> str:
    import anthropic

    # Fail closed: with api_key=None the SDK falls back to ANTHROPIC_API_KEY —
    # which would silently send the company's Jarvis key to an external host.
    if not tool.api_key_env:
        raise RuntimeError(
            f"AI tool '{tool.name}' has no api_key_env configured; refusing to "
            "fall back to the ANTHROPIC_API_KEY environment variable."
        )
    api_key = os.environ.get(tool.api_key_env, "")
    if not api_key:
        raise RuntimeError(
            f"Environment variable '{tool.api_key_env}' for AI tool '{tool.name}' is not set."
        )
    client = anthropic.Anthropic(base_url=tool.base_url or None, api_key=api_key)
    response = client.messages.create(
        model=tool.model or "claude-opus-4-8",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in response.content if b.type == "text")


def build_tools(ctx: ToolContext) -> list:
    configured = {t.name: t for t in ctx.config.ai_tools}
    if not configured:
        return []

    names = ", ".join(sorted(configured))

    def ask_ai_tool(tool_name: str, prompt: str) -> str:
        tool = configured.get(tool_name)
        if tool is None:
            return f"'{tool_name}' is not a configured AI tool. Available: {names}."
        if not ctx.permissions.require(
            f"ai:{tool.name}", f"send prompts to the external AI tool '{tool.name}'"
        ):
            return f"The user declined access to '{tool.name}'."
        # Sending a prompt off-machine is an egress side effect — confirm the
        # specific payload like any other, even with a standing grant.
        preview = prompt if len(prompt) <= 150 else prompt[:150] + "…"
        summary = (
            f"I will send a prompt of {len(prompt)} characters to the external AI "
            f'tool {tool.name}. It begins: "{preview}".'
        )
        if not ctx.confirmer.confirm(
            "ask_ai_tool", summary,
            audit_detail=f"{tool.name}: {len(prompt)} chars",
        ):
            return f"Cancelled — the user did not confirm sending the prompt to '{tool.name}'."
        try:
            if tool.kind == "anthropic":
                answer = _call_anthropic(tool, prompt)
            else:
                answer = _call_openai_compatible(tool, prompt)
            ctx.audit.record("tool_call", tool="ask_ai_tool",
                             detail=f"{tool.name}: {prompt[:120]}")
            return as_document(f"ai-tool:{tool.name}", answer)
        except Exception as e:
            ctx.audit.record("tool_call", tool="ask_ai_tool", detail=tool.name, ok=False)
            return f"The AI tool '{tool.name}' failed: {e}"

    # The docstring drives the tool schema, so it is set dynamically to list
    # the tools this deployment actually has (f-strings can't be docstrings).
    ask_ai_tool.__doc__ = (
        "Forward a prompt to one of the company's approved external AI tools "
        f"and return its answer. Available tools: {names}.\n\n"
        "Args:\n"
        "    tool_name: Which configured AI tool to ask.\n"
        "    prompt: The prompt to send it.\n"
    )
    return [beta_tool(ask_ai_tool)]
