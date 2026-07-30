"""Company-internal tool integrations.

Jarvis can read context from and take actions on internal tools (dashboards,
ticketing, plant ops, …) — but only tools declared in config, only actions
declared per tool, and always **as the signed-in employee**:

- Authentication is per-user: a bearer token for that employee, stored in
  their Windows keyring (`jarvis secrets set tool-<name>-token`). Jarvis never
  holds database credentials or shared service accounts.
- Authorization happens **server-side**, in the tool's own API. If the
  employee's account can't see a record, the API returns 403/404 and Jarvis
  relays that — it does not (and cannot) work around access levels.
- Write actions (any non-GET) additionally require the user's spoken
  confirmation of the exact payload, like every other side effect.
- Read results come back wrapped as untrusted data (prompt-injection
  boundary), because internal records can contain text from anyone.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

import httpx
from anthropic import beta_tool

from . import ToolContext, as_document, cancelled_by_user
from ..config import InternalActionConfig, InternalToolConfig
from ..paths import cli_hint
from ..security import secrets as secret_store

_TIMEOUT = 30.0
_MAX_RESPONSE_CHARS = 100_000
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def token_secret_name(tool_name: str) -> str:
    return f"tool-{tool_name}-token"


# Tool names travel to the Claude API, which requires ^[a-zA-Z0-9_-]{1,128}$ —
# ASCII-only on purpose (\W would let e.g. Devanagari through and 400 every turn).
_BUILTIN_TOOL_NAMES = {
    "list_folder", "search_files", "read_file", "write_file",
    "drive_search", "drive_read", "drive_save_text", "drive_upload",
    "search_email", "read_email", "send_email", "organize_email",
    "list_chat_spaces", "read_chat_messages", "send_chat_message",
    "list_calendar_events", "check_availability",
    "create_calendar_event", "delete_calendar_event",
    "find_colleague", "ask_ai_tool", "ask_claude", "run_code", "search_web", "get_weather", "remember", "forget_fact",
}


def _sanitize_identifier(name: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or "tool"
    return ident if not ident[0].isdigit() else f"t_{ident}"


def _auth_headers(tool: InternalToolConfig) -> dict[str, str] | None:
    """Build auth headers for this employee, or None if not connected."""
    if tool.auth == "none":
        return {}
    token = secret_store.get_secret(token_secret_name(tool.name))
    if not token and tool.api_key_env:
        import os

        token = os.environ.get(tool.api_key_env, "")
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


def _describe_actions(tool: InternalToolConfig) -> str:
    lines = []
    for action in tool.actions:
        params = ", ".join(
            f"{p} ({spec.get('type', 'string')}"
            f"{', optional' if not spec.get('required', True) else ''})"
            for p, spec in action.params.items()
        ) or "none"
        lines.append(f"- {action.name} [{action.kind}]: {action.description} "
                     f"Params: {params}")
    return "\n".join(lines)


def _execute(
    ctx: ToolContext, tool: InternalToolConfig, action: InternalActionConfig, params: dict
) -> str:
    # Split params into path / query / body according to the action spec.
    missing = [p for p, spec in action.params.items()
               if spec.get("required", True) and p not in params]
    if missing:
        return f"Missing required parameters for {action.name}: {', '.join(missing)}."
    unknown = [p for p in params if p not in action.params]
    if unknown:
        return f"Unknown parameters for {action.name}: {', '.join(unknown)}."

    path_params, query, body = {}, {}, {}
    placeholders = set(_PLACEHOLDER.findall(action.path))
    for name, value in params.items():
        where = action.params[name].get("in")
        if name in placeholders or where == "path":
            # Percent-encode everything (incl. "/") so a model-supplied value
            # like "1/../../admin" can't traverse outside the whitelisted path.
            path_params[name] = quote(str(value), safe="")
        elif where == "body":
            body[name] = value
        elif where == "query":
            query[name] = value
        else:  # default: body for mutating methods, query otherwise
            (body if action.method not in ("GET", "HEAD") else query)[name] = value

    unfilled = placeholders - set(path_params)
    if unfilled:
        return f"Missing path parameters: {', '.join(sorted(unfilled))}."
    orphans = set(path_params) - placeholders
    if orphans:
        # A value the request would otherwise silently drop — config bug.
        return (
            f"Config error in {action.name}: parameter(s) {', '.join(sorted(orphans))} "
            f"are declared `in: path` but the path template {action.path!r} has no "
            f"matching placeholder. Fix the {action.name} config."
        )
    try:
        filled_path = action.path.format(**path_params)
    except (KeyError, IndexError, ValueError) as e:
        # Stray braces in the configured path template — a config bug, not a crash.
        return f"The action's path template is malformed ({e}). Fix the {action.name} config."

    headers = _auth_headers(tool)
    if headers is None:
        return (
            f"You're not connected to {tool.name} yet. Run "
            f"{cli_hint(f'secrets set {token_secret_name(tool.name)}')} in a "
            "terminal to store your personal access token."
        )

    url = tool.base_url.rstrip("/") + "/" + filled_path.lstrip("/")
    try:
        resp = httpx.request(
            action.method, url,
            params=query or None,
            json=body or None,
            headers=headers,
            timeout=_TIMEOUT,
            # Some backends answer through a redirect (Google Apps Script's
            # /exec 302s to googleusercontent.com); httpx doesn't follow by
            # default, which would silently return the empty redirect body.
            follow_redirects=True,
        )
    except httpx.HTTPError as e:
        ctx.audit.record("tool_call", tool=f"{tool.name}:{action.name}",
                         detail=str(e), ok=False)
        return f"Could not reach {tool.name}: {e}"

    if resp.status_code in (401, 403):
        ctx.audit.record("tool_call", tool=f"{tool.name}:{action.name}",
                         decision="server_denied", ok=False)
        return (
            f"{tool.name} denied this request ({resp.status_code}): your account "
            "doesn't have access to that. This is enforced by the tool itself "
            "and can't be overridden from here."
        )
    if resp.status_code >= 400:
        ctx.audit.record("tool_call", tool=f"{tool.name}:{action.name}",
                         detail=f"HTTP {resp.status_code}", ok=False)
        return f"{tool.name} returned an error ({resp.status_code}): {resp.text[:300]}"

    text = resp.text[:_MAX_RESPONSE_CHARS]
    truncated = len(resp.text) > _MAX_RESPONSE_CHARS
    ctx.audit.record("tool_call", tool=f"{tool.name}:{action.name}",
                     detail=f"{action.method} {action.path}", decision="ok")
    doc = as_document(f"{tool.name}:{action.name}", text)
    return doc + ("\n[Truncated — response exceeded the read limit.]" if truncated else "")


def make_caller(ctx: ToolContext, tool: InternalToolConfig, public_name: str | None = None):
    """Build the (undecorated) tool function — separated for direct testing."""
    actions = {a.name: a for a in tool.actions}

    def call_internal(action: str, params_json: str = "{}") -> str:
        spec = actions.get(action)
        if spec is None:
            return (f"'{action}' is not an action of {tool.name}. "
                    f"Available: {', '.join(sorted(actions))}.")

        capability = f"tool:{tool.name}:{spec.kind}"
        verb = "read from" if spec.kind == "read" else "take write actions on"
        if not ctx.permissions.require(capability, f"{verb} {tool.description}"):
            return f"The user declined {spec.kind} access to {tool.name}."

        try:
            params = json.loads(params_json) if params_json.strip() else {}
        except json.JSONDecodeError:
            return "params_json must be a valid JSON object."
        if not isinstance(params, dict):
            return "params_json must be a JSON object, not a list or scalar."

        if spec.kind == "write":
            shown = json.dumps(params, ensure_ascii=False)
            shown = shown if len(shown) <= 200 else shown[:200] + "…"
            summary = (f"I will run the write action {spec.name} on {tool.name} "
                       f"with parameters {shown}.")
            result = ctx.confirmer.confirm(
                f"{tool.name}:{spec.name}", summary,
                audit_detail=f"{spec.name} params_keys={sorted(params)}",
            )
            if not result:
                return cancelled_by_user(result, f"{spec.name} on {tool.name}")

        return _execute(ctx, tool, spec, params)

    call_internal.__name__ = public_name or _sanitize_identifier(tool.name)
    call_internal.__doc__ = (
        f"Interact with {tool.description} ({tool.name}), one of the company's "
        "internal tools. You act as the signed-in employee; the tool's own "
        "server-side permissions decide what they can access.\n\n"
        f"Actions:\n{_describe_actions(tool)}\n\n"
        "Args:\n"
        "    action: One of the action names listed above.\n"
        "    params_json: JSON object string with that action's parameters.\n"
    )
    return call_internal


def _build_tool(ctx: ToolContext, tool: InternalToolConfig, public_name: str | None = None):
    return beta_tool(make_caller(ctx, tool, public_name))


def build_tools(ctx: ToolContext) -> list:
    tools = []
    used_names = set(_BUILTIN_TOOL_NAMES)
    for tool_cfg in ctx.config.internal_tools:
        if not tool_cfg.actions:
            continue
        read_denied = ctx.permissions.denied(f"tool:{tool_cfg.name}:read")
        write_denied = ctx.permissions.denied(f"tool:{tool_cfg.name}:write")
        if read_denied and (write_denied or not tool_cfg.has_write_actions):
            continue  # fully denied at setup — the model never sees this tool

        # Duplicate or builtin-shadowing names would 400 every API turn.
        name = _sanitize_identifier(tool_cfg.name)
        if name in used_names:
            suffix = 2
            while f"{name}_{suffix}" in used_names:
                suffix += 1
            print(f"Warning: internal tool '{tool_cfg.name}' collides with an "
                  f"existing tool name — exposed to the model as '{name}_{suffix}'.")
            name = f"{name}_{suffix}"
        used_names.add(name)
        tools.append(_build_tool(ctx, tool_cfg, name))
    return tools
