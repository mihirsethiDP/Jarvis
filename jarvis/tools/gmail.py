"""Gmail tool — send-only, always confirmed out loud before sending."""

from __future__ import annotations

import base64
from email.message import EmailMessage

from anthropic import beta_tool

from . import ToolContext


def build_tools(ctx: ToolContext) -> list:
    @beta_tool
    def send_email(to: str, subject: str, body: str, cc: str = "") -> str:
        """Send an email from the user's Gmail account. The user hears the
        recipient and subject and must explicitly confirm before anything is sent.

        Args:
            to: Recipient email address (comma-separate multiple addresses).
            subject: Email subject line.
            body: Plain-text body of the email.
            cc: Optional CC addresses, comma-separated.
        """
        if not ctx.permissions.require("email_send", "send email from your Gmail account"):
            return "The user declined email access."

        preview = body if len(body) <= 200 else body[:200] + "…"
        summary = (
            f"I will send an email to {to}"
            + (f" (cc {cc})" if cc else "")
            + f' with the subject "{subject}". It begins: "{preview}".'
        )
        if not ctx.confirmer.confirm(
            "send_email", summary,
            audit_detail=f'to={to} cc={cc} subject="{subject}"',  # no body in the log
        ):
            return "Cancelled — the user did not confirm sending the email."

        try:
            msg = EmailMessage()
            msg["To"] = to
            if cc:
                msg["Cc"] = cc
            msg["Subject"] = subject
            msg.set_content(body)
            encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            ctx.google_service("gmail", "v1").users().messages().send(
                userId="me", body={"raw": encoded}
            ).execute()
            ctx.audit.record("tool_call", tool="send_email",
                             detail=f"to={to} subject={subject}", decision="confirmed")
            return f"Email sent to {to}."
        except Exception as e:
            ctx.audit.record("tool_call", tool="send_email", detail=f"to={to}", ok=False)
            return f"Sending failed: {e}"

    return [send_email]
