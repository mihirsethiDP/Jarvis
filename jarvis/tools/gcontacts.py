"""Directory lookup: resolve a colleague's name to their email/phone.

Uses the People API's Workspace *Directory* surface (searchDirectoryPeople),
not personal Contacts — personal contacts only cover people the employee has
already saved, which defeats "look up someone I've never emailed."

Real prerequisite, distinct from anything Drive/Gmail/Calendar/Chat need: a
DigitalPaani Workspace admin must set Admin console -> Directory -> Directory
settings -> Sharing settings -> "External Directory Sharing" to "Organization
data and authenticated user basic profile fields". Until that's done, every
lookup returns empty regardless of how correctly this code runs — the tool
says so rather than reporting a plain "not found".
"""

from __future__ import annotations

from anthropic import beta_tool

from . import ToolContext, as_document

_READ_MASK = "names,emailAddresses,phoneNumbers"
_ADMIN_HINT = (
    " If this keeps happening for people you know are in the company, ask your "
    "Workspace admin to check the 'External Directory Sharing' setting — Jarvis "
    "needs it set to share organization data, not just the caller's own profile."
)


def build_tools(ctx: ToolContext) -> list:
    def _people():
        return ctx.google_service("people", "v1")

    @beta_tool
    def find_colleague(name: str) -> str:
        """Look up a colleague's email address and phone number by name in
        the company directory.

        Args:
            name: The colleague's name (or the start of it — matching is
                prefix-based, so partial middle fragments may not match).
        """
        if not ctx.permissions.require("directory_read", "look up colleagues in your company directory"):
            return "The user declined company directory access."
        try:
            resp = _people().people().searchDirectoryPeople(
                query=name, readMask=_READ_MASK,
                sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
            ).execute()
            people = resp.get("people", [])
            ctx.audit.record("tool_call", tool="find_colleague", detail=name)
            if not people:
                return f"No one matching '{name}' found in the company directory." + _ADMIN_HINT
            lines = []
            for p in people[:5]:
                display = (p.get("names") or [{}])[0].get("displayName", name)
                emails = ", ".join(e.get("value", "") for e in p.get("emailAddresses", []))
                phones = ", ".join(ph.get("value", "") for ph in p.get("phoneNumbers", []))
                lines.append(f"- {display}: {emails}" + (f", phone: {phones}" if phones else ""))
            return as_document(f"directory:{name}", "\n".join(lines))
        except Exception as e:
            ctx.audit.record("tool_call", tool="find_colleague", detail=name, ok=False)
            return f"Directory lookup failed: {e}"

    return [find_colleague]
