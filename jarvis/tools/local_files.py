"""Local file tools — strictly confined to the configured allowed directories."""

from __future__ import annotations

from pathlib import Path

from anthropic import beta_tool

from . import ToolContext, as_document, cancelled_by_user

_MAX_READ_BYTES = 200_000
_MAX_RESULTS = 25

_TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".html",
    ".py", ".js", ".ts", ".log", ".ini", ".cfg", ".toml", ".rst",
}


class PathNotAllowed(ValueError):
    pass


# CON, NUL, COM1… are device names on Windows; "file.txt:stream" is an NTFS
# alternate data stream that would bypass suffix checks and dir listings.
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                   *(f"lpt{i}" for i in range(1, 10))}


def _reject_windows_tricks(path_str: str, resolved: Path) -> None:
    body = path_str[2:] if len(path_str) >= 2 and path_str[1] == ":" else path_str
    if ":" in body:
        raise PathNotAllowed("Paths with NTFS alternate data streams are not allowed.")
    stem = resolved.name.split(".")[0].lower()
    if stem in _RESERVED_NAMES:
        raise PathNotAllowed(f"'{resolved.name}' is a reserved Windows device name.")


def resolve_safe(ctx: ToolContext, path_str: str) -> Path:
    """Resolve a user/model supplied path and verify it stays inside the allowlist.

    Resolution happens *before* the containment check, so `..` and symlink
    escapes are caught; ADS and device-name tricks are rejected outright.
    """
    path = Path(path_str).expanduser()
    allowed = ctx.config.allowed_dirs
    if not path.is_absolute():
        # Relative paths are anchored at the first allowed directory.
        if not allowed:
            raise PathNotAllowed("No allowed directories are configured.")
        path = allowed[0] / path
    resolved = path.resolve()
    _reject_windows_tricks(path_str, resolved)
    for root in allowed:
        try:
            if resolved.is_relative_to(root):
                return resolved
        except ValueError:
            continue
    raise PathNotAllowed(
        f"'{path_str}' is outside the directories Jarvis is allowed to access."
    )


def build_tools(ctx: ToolContext) -> list:
    @beta_tool
    def list_folder(folder: str = "") -> str:
        """List files in a local folder within Jarvis's allowed directories.

        Args:
            folder: Folder path to list. Empty string lists each configured
                allowed directory.
        """
        if not ctx.permissions.require("files_read", "read files in your allowed folders"):
            return "The user declined file access."
        try:
            if not folder:
                roots = ctx.config.allowed_dirs
                ctx.audit.record("tool_call", tool="list_folder", detail="(roots)")
                return "Allowed directories:\n" + "\n".join(str(r) for r in roots)
            target = resolve_safe(ctx, folder)
            entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            lines = [
                f"{'[dir] ' if e.is_dir() else ''}{e.name}" for e in entries[:_MAX_RESULTS * 2]
            ]
            ctx.audit.record("tool_call", tool="list_folder", detail=str(target))
            # File names are attacker-influenceable content — wrap as data.
            return as_document(f"folder:{target}", "\n".join(lines) if lines else "(empty)")
        except PathNotAllowed as e:
            ctx.audit.record("tool_call", tool="list_folder", detail=folder,
                             decision="blocked_path", ok=False)
            return f"Blocked: {e}"
        except OSError as e:
            return f"Error listing folder: {e}"

    @beta_tool
    def search_files(name: str) -> str:
        """Search for files by name across Jarvis's allowed local directories.

        Args:
            name: Full or partial file name to look for (case-insensitive).
        """
        if not ctx.permissions.require("files_read", "search files in your allowed folders"):
            return "The user declined file access."
        needle = name.lower().strip()
        if not needle:
            return "Please provide a file name to search for."
        hits: list[str] = []
        for root in ctx.config.allowed_dirs:
            if not root.exists():
                continue
            try:
                for p in root.rglob("*"):
                    if len(hits) >= _MAX_RESULTS:
                        break
                    if p.is_file() and needle in p.name.lower():
                        hits.append(str(p))
            except (OSError, PermissionError):
                continue
        ctx.audit.record("tool_call", tool="search_files", detail=needle)
        if not hits:
            return f"No files matching '{name}' in the allowed directories."
        return as_document(f"search:{name}", "\n".join(hits))

    @beta_tool
    def read_file(path: str) -> str:
        """Read a local text file from within the allowed directories.

        Args:
            path: Path to the file to read.
        """
        if not ctx.permissions.require("files_read", "read files in your allowed folders"):
            return "The user declined file access."
        try:
            target = resolve_safe(ctx, path)
            if not target.is_file():
                return f"'{target}' does not exist or is not a file."
            if target.suffix.lower() not in _TEXT_SUFFIXES:
                return (
                    f"'{target.name}' is not a plain-text file type I can read directly. "
                    "I can read: " + ", ".join(sorted(_TEXT_SUFFIXES))
                )
            data = target.read_bytes()[:_MAX_READ_BYTES]
            text = data.decode("utf-8", errors="replace")
            truncated = target.stat().st_size > _MAX_READ_BYTES
            ctx.audit.record("tool_call", tool="read_file", detail=str(target))
            doc = as_document(str(target), text)
            return doc + ("\n[Truncated — file is larger than the read limit.]" if truncated else "")
        except PathNotAllowed as e:
            ctx.audit.record("tool_call", tool="read_file", detail=path,
                             decision="blocked_path", ok=False)
            return f"Blocked: {e}"
        except OSError as e:
            return f"Error reading file: {e}"

    @beta_tool
    def write_file(path: str, content: str) -> str:
        """Write a text file inside the allowed directories. Asks the user to
        confirm before writing; overwrites are called out explicitly.

        Args:
            path: Destination file path.
            content: Full text content to write.
        """
        if not ctx.permissions.require("files_write", "create or modify local files"):
            return "The user declined file write access."
        try:
            target = resolve_safe(ctx, path)
        except PathNotAllowed as e:
            ctx.audit.record("tool_call", tool="write_file", detail=path,
                             decision="blocked_path", ok=False)
            return f"Blocked: {e}"
        if target.suffix.lower() not in _TEXT_SUFFIXES:
            # Never write executables, scripts, or shortcuts — text only.
            ctx.audit.record("tool_call", tool="write_file", detail=str(target),
                             decision="blocked_type", ok=False)
            return (
                f"Blocked: '{target.suffix or target.name}' is not a writable text "
                "type. I can write: " + ", ".join(sorted(_TEXT_SUFFIXES))
            )

        exists = target.exists()
        action = "overwrite the existing file" if exists else "create a new file"
        summary = f"I will {action} at {target} ({len(content)} characters)."
        result = ctx.confirmer.confirm("write_file", summary)
        if not result:
            return cancelled_by_user(result, "the file write")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            ctx.audit.record("tool_call", tool="write_file", detail=str(target),
                             decision="confirmed")
            return f"Wrote {len(content)} characters to {target}."
        except OSError as e:
            ctx.audit.record("tool_call", tool="write_file", detail=str(target), ok=False)
            return f"Error writing file: {e}"

    return [list_folder, search_files, read_file, write_file]
