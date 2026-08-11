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
            # DOMAIN_CONTACT covers the shared contacts an admin publishes —
            # clients, vendors, site engineers. Without it those people are
            # invisible and Jarvis says they are not in the directory.
            resp = _people().people().searchDirectoryPeople(
                query=name, readMask=_READ_MASK,
                sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
                         "DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT"],
                pageSize=30,
            ).execute()
            people = resp.get("people", [])
            # Prefix matching misses a mis-heard name entirely; retry shorter
            # before telling the user the person does not exist.
            if not people and name.split():
                first = name.split()[0]
                for attempt in (first, first[:4]):
                    if len(attempt) >= 3:
                        retry = _people().people().searchDirectoryPeople(
                            query=attempt, readMask=_READ_MASK,
                            sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
                                     "DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT"],
                            pageSize=30,
                        ).execute()
                        people = retry.get("people", [])
                        if people:
                            break
            ctx.audit.record("tool_call", tool="find_colleague",
                             detail=f"{name} -> {len(people)} match(es)")
            if not people:
                return f"No one matching '{name}' found in the company directory." + _ADMIN_HINT
            lines = []
            shown = people[:10]
            for p in shown:
                display = (p.get("names") or [{}])[0].get("displayName", name)
                emails = ", ".join(e.get("value", "") for e in p.get("emailAddresses", []))
                phones = ", ".join(ph.get("value", "") for ph in p.get("phoneNumbers", []))
                lines.append(f"- {display}: {emails}" + (f", phone: {phones}" if phones else ""))
            body = "\n".join(lines)
            if len(people) > 1:
                # Never let the model quietly pick one — "Priya" matching two
                # people must become a question, not a coin flip that emails
                # the wrong colleague.
                body = (
                    f"AMBIGUOUS: {len(people)} people match '{name}'. Do NOT choose "
                    "one yourself. Ask the user which person they mean (read out "
                    "the names, and their team or email if that helps tell them "
                    "apart), then use the address they pick.\n\n" + body
                )
            return as_document(f"directory:{name}", body)
        except Exception as e:
            ctx.audit.record("tool_call", tool="find_colleague", detail=name, ok=False)
            return f"Directory lookup failed: {e}"

    return [find_colleague]
