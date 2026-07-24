"""Google Drive tools: search, read, and save documents."""

from __future__ import annotations

import io
from pathlib import Path

from anthropic import beta_tool

from . import ToolContext, as_document
from .local_files import PathNotAllowed, resolve_safe

_MAX_EXPORT_BYTES = 200_000

# Google-native docs are exported to plain text; regular text files download as-is.
_EXPORTABLE = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def _drive(ctx: ToolContext):
    return ctx.google_service("drive", "v3")


def build_tools(ctx: ToolContext) -> list:
    @beta_tool
    def drive_search(query: str, max_results: int = 10) -> str:
        """Search the user's Google Drive for files by name or content keywords.

        Args:
            query: What to look for, e.g. a document title or topic keywords.
            max_results: Maximum number of results to return (1-25).
        """
        if not ctx.permissions.require("drive_read", "search and read your Google Drive"):
            return "The user declined Google Drive access."
        try:
            safe_q = query.replace("\\", "\\\\").replace("'", "\\'")
            resp = _drive(ctx).files().list(
                q=f"(name contains '{safe_q}' or fullText contains '{safe_q}') and trashed = false",
                pageSize=max(1, min(int(max_results), 25)),
                fields="files(id, name, mimeType, modifiedTime, owners(displayName))",
            ).execute()
            files = resp.get("files", [])
            ctx.audit.record("tool_call", tool="drive_search", detail=query)
            if not files:
                return f"No Drive files matched '{query}'."
            lines = [
                f"- {f['name']}  (id: {f['id']}, type: {f['mimeType'].split('.')[-1]}, "
                f"modified: {f.get('modifiedTime', '?')[:10]})"
                for f in files
            ]
            return "Drive results:\n" + "\n".join(lines)
        except Exception as e:  # googleapiclient raises many transport error types
            ctx.audit.record("tool_call", tool="drive_search", detail=query, ok=False)
            return f"Drive search failed: {e}"

    @beta_tool
    def drive_read(file_id: str) -> str:
        """Read the text content of a Google Drive file (Docs, Sheets, Slides,
        or plain-text files). Use drive_search first to find the file id.

        Args:
            file_id: The Drive file id to read.
        """
        if not ctx.permissions.require("drive_read", "search and read your Google Drive"):
            return "The user declined Google Drive access."
        try:
            from googleapiclient.http import MediaIoBaseDownload

            service = _drive(ctx)
            meta = service.files().get(fileId=file_id, fields="id, name, mimeType, size").execute()
            mime = meta["mimeType"]
            name = meta["name"]

            if mime in _EXPORTABLE:
                request = service.files().export_media(fileId=file_id, mimeType=_EXPORTABLE[mime])
            elif mime.startswith("text/") or mime in ("application/json",):
                request = service.files().get_media(fileId=file_id)
            else:
                return (
                    f"'{name}' is {mime}, which I can't read as text. I can read Google "
                    "Docs/Sheets/Slides and plain-text files."
                )

            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done and buf.tell() <= _MAX_EXPORT_BYTES:
                _, done = downloader.next_chunk()
            text = buf.getvalue()[:_MAX_EXPORT_BYTES].decode("utf-8", errors="replace")
            ctx.audit.record("tool_call", tool="drive_read", detail=f"{name} ({file_id})")
            doc = as_document(f"gdrive:{name}", text)
            return doc + ("" if done else "\n[Truncated — document is larger than the read limit.]")
        except Exception as e:
            ctx.audit.record("tool_call", tool="drive_read", detail=file_id, ok=False)
            return f"Drive read failed: {e}"

    @beta_tool
    def drive_save_text(name: str, content: str, folder_id: str = "") -> str:
        """Create a new text file in the user's Google Drive. Asks the user to
        confirm before creating.

        Args:
            name: File name to create, e.g. "meeting-notes.txt".
            content: Full text content of the file.
            folder_id: Optional Drive folder id to place the file in.
        """
        if not ctx.permissions.require("drive_write", "create files in your Google Drive"):
            return "The user declined Google Drive write access."
        summary = f"I will create '{name}' in your Google Drive ({len(content)} characters)."
        if not ctx.confirmer.confirm("drive_save_text", summary):
            return "Cancelled — the user did not confirm the Drive upload."
        try:
            from googleapiclient.http import MediaIoBaseUpload

            body: dict = {"name": name}
            if folder_id:
                body["parents"] = [folder_id]
            media = MediaIoBaseUpload(
                io.BytesIO(content.encode("utf-8")), mimetype="text/plain"
            )
            created = _drive(ctx).files().create(
                body=body, media_body=media, fields="id, webViewLink"
            ).execute()
            ctx.audit.record("tool_call", tool="drive_save_text", detail=name,
                             decision="confirmed")
            return f"Created '{name}' in Drive: {created.get('webViewLink', created['id'])}"
        except Exception as e:
            ctx.audit.record("tool_call", tool="drive_save_text", detail=name, ok=False)
            return f"Drive upload failed: {e}"

    @beta_tool
    def drive_upload(local_path: str, folder_id: str = "") -> str:
        """Upload a local file (from the allowed directories) to Google Drive.
        Asks the user to confirm before uploading.

        Args:
            local_path: Path of the local file to upload.
            folder_id: Optional Drive folder id to place the file in.
        """
        if not ctx.permissions.require("drive_write", "upload files to your Google Drive"):
            return "The user declined Google Drive write access."
        try:
            source = resolve_safe(ctx, local_path)
        except PathNotAllowed as e:
            ctx.audit.record("tool_call", tool="drive_upload", detail=local_path,
                             decision="blocked_path", ok=False)
            return f"Blocked: {e}"
        if not source.is_file():
            return f"'{source}' does not exist or is not a file."

        size_kb = source.stat().st_size // 1024
        summary = f"I will upload {source.name} ({size_kb} KB) from {source.parent} to your Google Drive."
        if not ctx.confirmer.confirm("drive_upload", summary):
            return "Cancelled — the user did not confirm the upload."
        try:
            from googleapiclient.http import MediaFileUpload

            body: dict = {"name": source.name}
            if folder_id:
                body["parents"] = [folder_id]
            media = MediaFileUpload(str(source), resumable=True)
            created = _drive(ctx).files().create(
                body=body, media_body=media, fields="id, webViewLink"
            ).execute()
            ctx.audit.record("tool_call", tool="drive_upload", detail=str(source),
                             decision="confirmed")
            return f"Uploaded '{source.name}': {created.get('webViewLink', created['id'])}"
        except Exception as e:
            ctx.audit.record("tool_call", tool="drive_upload", detail=str(source), ok=False)
            return f"Drive upload failed: {e}"

    return [drive_search, drive_read, drive_save_text, drive_upload]
