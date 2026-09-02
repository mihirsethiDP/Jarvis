"""Serve the DigitalPaani email house style to the drafting model.

The voice rules ride in the system prompt; the 25 template skeletons would
cost ~4k tokens on every turn, so they live here and are fetched only when an
email is actually being drafted. The tool also resolves the SENDER: the
source material hardcodes one CSM's signature, but Jarvis signs as whoever is
signed in — name and phone from the company directory, role from config.

No permission gate: the output is packaged company style plus the user's own
directory entry, none of which is sensitive to the user themselves.
"""

from __future__ import annotations

from anthropic import beta_tool

from ..email_style import ESCALATION_MATRIX, TEMPLATES, signoff
from . import ToolContext


def build_tools(ctx: ToolContext) -> list:
    sender_cache: dict[str, str] = {}

    def _sender() -> tuple[str, str]:
        """(display name, phone) of the signed-in user, best effort."""
        if "name" in sender_cache:
            return sender_cache["name"], sender_cache.get("phone", "")
        name, phone = "", ""
        try:
            # people/me needs a profile scope Jarvis does not hold (verified:
            # 403). The chain that works with our scopes: Gmail profile gives
            # the user's address, the directory maps address -> name + phone.
            address = ctx.google_service("gmail", "v1").users().getProfile(
                userId="me").execute().get("emailAddress", "").casefold()
            if address and not ctx.permissions.denied("directory_read"):
                from . import directory as _directory

                people_svc = ctx.google_service("people", "v1")
                everyone = _directory._fetch_all(people_svc) or []
                for person in everyone:
                    if _directory.email(person).casefold() == address:
                        name = _directory.display_name(person)
                        phone = (person.get("phoneNumbers") or [{}])[0].get("value", "")
                        break
        except Exception:
            pass
        sender_cache["name"] = name
        sender_cache["phone"] = phone
        return name, phone

    @beta_tool
    def email_template(template: str = "list") -> str:
        """Get the DigitalPaani house template for a client-facing email:
        the skeleton to follow, the required tone, and the details to collect
        from the user. ALWAYS call this before drafting any email to a client.

        Args:
            template: One of the template keys (call with "list" to see all,
                e.g. welcome_om, onboarding_update, off_track, payment_followup,
                mbr, qbr, issue_ack_reactive, issue_major, renewal,
                escalation_response), or "list".
        """
        key = template.strip().lower().replace("-", "_").replace(" ", "_")
        if key in ("list", "", "all"):
            lines = ["Available house templates (key — name, fixed tone):"]
            for k, (title, tone, _vars, _body) in TEMPLATES.items():
                lines.append(f"- {k} — {title}" + (f" (tone: {tone})" if tone else ""))
            lines.append(
                "Pick the closest to the user's request and call again with its key."
            )
            ctx.audit.record("tool_call", tool="email_template", detail="list")
            return "\n".join(lines)

        entry = TEMPLATES.get(key)
        if entry is None:
            close = [k for k in TEMPLATES if key[:5] in k or k[:5] in key]
            ctx.audit.record("tool_call", tool="email_template",
                             detail=f"{key} -> unknown", ok=False)
            return (f"No template named '{template}'."
                    + (f" Closest: {', '.join(close)}." if close else "")
                    + " Call email_template('list') for all of them.")

        title, tone, variables, body = entry
        name, phone = _sender()
        role = str(ctx.config.get("assistant.signature_role", "")).strip()
        is_welcome = key.startswith("welcome")
        sender_block = signoff(name or "[Sender Name]", phone, role,
                               welcome=is_welcome)
        body = body.replace("[SIGN-OFF]", sender_block)
        body = body.replace("[WELCOME SIGN-OFF]", sender_block)
        if name:
            body = body.replace("[Sender Name]", name)

        parts = [
            f"HOUSE TEMPLATE: {title}",
            f"Required tone: {tone}" if tone else
            "Tone: user's choice (Professional / Friendly / Formal / Empathetic / Urgent / Apologetic)",
            "Details to collect from the user (ask at most TWO questions, "
            "then draft with what you have):",
            *(f"  - {v}" for v in variables),
            "",
            "Skeleton to follow (fill [brackets]; keep the structure):",
            body,
        ]
        if key in ("issue_major", "escalation_response"):
            parts += ["", "Real escalation matrix, if the email should include one:",
                      ESCALATION_MATRIX]
        ctx.audit.record("tool_call", tool="email_template", detail=key)
        return "\n".join(parts)

    return [email_template]
