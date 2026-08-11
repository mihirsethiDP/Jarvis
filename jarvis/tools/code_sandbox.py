"""Sandboxed Python execution — the "have Claude write and run code" path.

This is the single most dangerous capability in Jarvis, so it is built as a
narrow, inspected channel rather than a shell. Layers, outermost first:

1. **Off by default.** The `code_run` capability must be granted explicitly;
   the setup wizard describes it in plain terms and a standing denial
   removes the tool from the model's toolset entirely.
2. **AST validation, not regex.** The code is parsed and walked before it
   runs. Only an allowlist of imports is permitted; dynamic-execution
   escapes (eval/exec/compile/__import__/getattr-by-string, dunder attribute
   reach-through) are rejected outright. A denylist of "bad words" would be
   trivially bypassable; an allowlist of syntax is not.
3. **Jarvis cannot be touched by it.** Any literal path referencing the
   Jarvis install, %APPDATA%\\Jarvis, the OAuth client file, the audit log,
   permissions, memory, or the keyring is rejected at validation time. The
   assistant's own code, config and consent records are out of reach by
   construction — this is the "don't let it mess with Jarvis" requirement.
4. **No network.** Every networking module is off the allowlist, so the
   sandbox cannot exfiltrate anything or pull code down.
5. **Confined, disposable workspace.** Execution happens in a dedicated
   scratch directory with the interpreter in isolated mode (`-I`), a hard
   timeout, and an output cap. (`-I` alone: `-S` additionally skipped
   site.py, which left the allowlisted numpy/pandas unimportable.)
6. **The human reads the code first.** Execution is a confirmed side effect:
   the exact source is shown/read back and requires an explicit yes.
7. **Audited.** Every attempt — validated, refused, run, failed — is logged.

Honest limit: this is a language-level sandbox, not an OS-level one. It
raises the bar very high for anything arriving through the assistant, but a
determined local attacker who already runs code as this Windows user does
not need Jarvis. True isolation would need a container or a restricted
token; that is a deliberate future step, noted in docs/SECURITY.md.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from anthropic import beta_tool

from . import ToolContext, as_document, cancelled_by_user
from ..paths import app_data_dir

_TIMEOUT_SECONDS = 20
_MAX_OUTPUT_CHARS = 20_000
_MAX_SOURCE_CHARS = 20_000

# Only these may be imported. Everything absent is refused — including every
# networking, subprocess, filesystem-walking and introspection module.
_ALLOWED_IMPORTS = {
    "json", "csv", "re", "math", "cmath", "statistics", "datetime", "time",
    "collections", "itertools", "functools", "operator", "string", "textwrap",
    "decimal", "fractions", "random", "unicodedata", "base64", "binascii",
    "hashlib", "hmac", "uuid", "io", "typing", "dataclasses", "enum", "copy",
    "heapq", "bisect", "array", "zlib", "gzip", "difflib", "pprint",
    "numpy", "pandas", "openpyxl",
}

# Names that re-open arbitrary execution or reach around the AST checks.
_BANNED_NAMES = {
    "eval", "exec", "compile", "__import__", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "breakpoint", "memoryview", "input",
}
_BANNED_ATTRS = {
    "__globals__", "__builtins__", "__subclasses__", "__bases__", "__mro__",
    "__code__", "__closure__", "__loader__", "__spec__", "__dict__",
    "__getattribute__", "__reduce__", "__class__",
}


class CodeRejected(ValueError):
    """Static validation failed — the code never runs."""


def workspace_dir() -> Path:
    path = app_data_dir() / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _minimal_env() -> dict[str, str]:
    """The smallest environment Python still starts in on Windows.

    Deliberately excludes ANTHROPIC_API_KEY and everything else the parent
    holds — a sandboxed program should not inherit the assistant's secrets
    even though it cannot import `os` to read them.
    """
    keep = ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")
    env = {name: os.environ[name] for name in keep if name in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _protected_fragments() -> list[str]:
    """Path fragments the sandbox must never reference, so generated code
    cannot read or rewrite Jarvis itself."""
    jarvis_pkg = Path(__file__).resolve().parent.parent          # .../jarvis
    return [
        str(jarvis_pkg).lower(),
        str(jarvis_pkg.parent).lower(),                          # the repo/install
        str(app_data_dir()).lower(),                             # config, tokens, audit
        "client_secret", "credentials.json", "token.json",
        "permissions.json", "memory.json", "audit.jsonl", "usage.json",
        ".venv", "site-packages", "appdata\\roaming\\jarvis",
        "\\windows\\", "system32", ".ssh", ".aws", ".env",
    ]


def _looks_like_escaping_path(value: str) -> bool:
    """True for a string that is clearly a path leaving the workspace.

    Deliberately narrow, and applied to the raw string rather than a
    separator-normalised one: a regex literal like r"\\d+" starts with a
    backslash without being a path at all, and flagging it would refuse
    perfectly ordinary data work.
    """
    if value.startswith("\\\\") or value.startswith("//"):
        return True                                    # UNC share
    if len(value) > 2 and value[1] == ":" and value[2] in "\\/":
        return True                                    # C:\... or C:/...
    parts = value.replace("\\", "/").split("/")
    return ".." in parts and len(parts) > 1            # climbs out


def _reject_path(value: str, protected: list[str], workspace: str) -> None:
    """Raise if this path literal is out of bounds."""
    lowered = value.lower().replace("/", "\\")
    if lowered.startswith(workspace):
        return                                   # inside the scratch directory
    for fragment in protected:
        if fragment in lowered:
            raise CodeRejected(
                "it references a protected location "
                f"('{value[:60]}'). Code may only touch its own workspace folder."
            )
    if _looks_like_escaping_path(value):
        raise CodeRejected(
            f"it references '{value[:60]}', which is outside its workspace "
            "folder. Use a plain relative filename."
        )


def _is_open_call(node: ast.Call) -> bool:
    """open(...) or io.open(...) — the only ways in, given the allowlist."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "open"
    return isinstance(func, ast.Attribute) and func.attr == "open"


def validate(source: str) -> None:
    """Raise CodeRejected unless the code is inside the permitted subset."""
    if len(source) > _MAX_SOURCE_CHARS:
        raise CodeRejected(f"the code is too long ({len(source)} characters)")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise CodeRejected(f"it isn't valid Python ({e.msg} on line {e.lineno})")

    protected = _protected_fragments()
    workspace = str(workspace_dir()).lower()

    for node in ast.walk(tree):
        # -- imports: allowlist only, top-level module name --------------
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in _ALLOWED_IMPORTS:
                    raise CodeRejected(f"it imports '{alias.name}', which isn't permitted")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level or root not in _ALLOWED_IMPORTS:
                raise CodeRejected(f"it imports from '{node.module}', which isn't permitted")

        # -- dynamic execution / introspection escapes -------------------
        elif isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            raise CodeRejected(f"it uses '{node.id}', which can bypass these checks")
        elif isinstance(node, ast.Attribute) and node.attr in _BANNED_ATTRS:
            raise CodeRejected(f"it reaches into '{node.attr}', which can bypass these checks")

        # -- file opens must stay inside the workspace --------------------
        # The confirmation tells the user this code "cannot reach any file
        # outside its own workspace folder". Only *protected* paths were
        # actually blocked, so an ordinary document elsewhere on the disk was
        # still reachable and the promise was wider than the enforcement.
        # The child runs with cwd=workspace, so relative paths are already
        # confined; what has to be rejected is anything absolute or climbing.
        elif isinstance(node, ast.Call) and _is_open_call(node):
            target = node.args[0] if node.args else None
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                _reject_path(target.value, protected, workspace)
            elif target is not None:
                raise CodeRejected(
                    "it opens a file whose path is computed at runtime, which "
                    "can't be checked in advance. Use a plain relative filename."
                )

        # -- literal paths pointing at Jarvis or the wider system ---------
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Not every file read goes through open(): pandas.read_csv and
            # openpyxl.load_workbook take a path directly, so every string
            # literal is checked, not just open()'s argument.
            _reject_path(node.value, protected, workspace)


def _imports_summary(source: str) -> str:
    """What the program actually pulls in.

    Reading 600 characters of Python aloud was unverifiable by ear, so the
    confirmation carried no information the user could act on. Naming the
    libraries is something they can actually judge."""
    import ast as _ast

    names: set[str] = set()
    try:
        for node in _ast.walk(_ast.parse(source)):
            if isinstance(node, _ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, _ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
    except SyntaxError:
        return "no importable libraries"
    return ", ".join(sorted(names)) if names else "no external libraries"


def build_tools(ctx: ToolContext) -> list:
    @beta_tool
    def run_code(code: str, purpose: str = "") -> str:
        """Run a short Python program in a locked-down sandbox and return what
        it printed. Use for calculations, parsing, reformatting and data
        crunching that your own tools can't do.

        Hard limits, enforced before anything runs: an allowlist of imports
        (no network, no subprocess, no OS access), no eval/exec/dynamic
        imports, and no access to anything outside the sandbox workspace —
        Jarvis's own code, config, tokens and logs are unreachable. The user
        sees the exact source and must approve it.

        Never use this to bypass a permission, reach a blocked file, probe
        or attack any system, or work around a limit the user set. If asked
        for something like that, refuse and say why.

        Args:
            code: The complete Python program. Print what you want returned.
            purpose: One short line on what it does, for the user's approval.
        """
        source = (code or "").strip()
        if not source:
            return "There was no code to run."

        if not ctx.permissions.require(
            "code_run", "run sandboxed code on this computer"
        ):
            return "The user declined code execution."

        try:
            validate(source)
        except CodeRejected as e:
            # Refusals are audited too: an attempt to reach outside the
            # sandbox is exactly the signal worth keeping.
            ctx.audit.record("tool_call", tool="run_code",
                             detail=f"rejected: {e}", decision="blocked", ok=False)
            return (
                f"I won't run that: {e}. The sandbox allows plain data-processing "
                "Python only — no network, no system access, and nothing outside "
                "its own workspace folder."
            )

        preview = source if len(source) <= 600 else source[:600] + "\n…(truncated)"
        result = ctx.confirmer.confirm(
            "run_code",
            (f"I want to run some code{f' to {purpose}' if purpose else ''}. "
             f"It is:\n{preview}\nIt runs sandboxed, with no network or file "
             "access outside its workspace."),
            audit_detail=f"{len(source)} chars: {purpose[:80]}",
        )
        if not result:
            return cancelled_by_user(result, "running that code")

        workspace = workspace_dir()
        script = workspace / "_jarvis_run.py"
        script.write_text(source, encoding="utf-8")
        try:
            completed = subprocess.run(
                # -I alone: isolated mode already ignores PYTHONPATH, other
                # environment variables and the user site directory, which is
                # the isolation that matters here. -S additionally skipped
                # site.py, which is what puts site-packages on sys.path — so
                # numpy and pandas were allowlisted and advertised for "data
                # crunching" while being impossible to import.
                [sys.executable, "-I", str(script)],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                encoding="utf-8",
                errors="replace",
                # Scrubbed environment: the parent process holds the Claude
                # API key (and whatever else the employee has exported), and
                # a child has no business seeing any of it. Only the few
                # variables Windows needs to start Python at all are passed.
                env=_minimal_env(),
            )
        except subprocess.TimeoutExpired:
            ctx.audit.record("tool_call", tool="run_code",
                             detail="timeout", decision="timeout", ok=False)
            return (f"The code ran longer than {_TIMEOUT_SECONDS} seconds and was "
                    "stopped. Try something smaller.")
        except Exception as e:
            ctx.audit.record("tool_call", tool="run_code", detail=str(e)[:120], ok=False)
            return f"Running the code failed: {e}"
        finally:
            script.unlink(missing_ok=True)

        stdout = (completed.stdout or "")[:_MAX_OUTPUT_CHARS]
        stderr = (completed.stderr or "")[:2000]
        ctx.audit.record("tool_call", tool="run_code",
                         detail=f"exit={completed.returncode} {purpose[:60]}",
                         decision="confirmed", ok=completed.returncode == 0)

        if completed.returncode != 0:
            return as_document("code-error", stderr or "(no error output)")
        return as_document("code-output", stdout or "(the code printed nothing)")

    return [run_code]
