from __future__ import annotations

import pytest

from jarvis.config import Config
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext
from jarvis.tools import code_sandbox
from jarvis.tools.code_sandbox import CodeRejected, validate

from conftest import FakeIO


def make_ctx(tmp_path, audit, answers):
    io = FakeIO(answers)
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    return ToolContext(config=Config(raw={}), permissions=pm,
                       confirmer=Confirmer(io, audit), audit=audit)


# ── what must be allowed ────────────────────────────────────────────────
def test_ordinary_data_work_is_allowed():
    validate("import json, statistics\nprint(statistics.mean([1,2,3]))")
    validate("import re\nprint(re.findall(r'\d+', 'a1b22'))")
    validate("from collections import Counter\nprint(Counter('aab'))")


# ── network / system escape attempts ────────────────────────────────────
@pytest.mark.parametrize("src", [
    "import socket",
    "import requests",
    "import urllib.request",
    "import http.client",
    "import subprocess",
    "import os",
    "import sys",
    "import shutil",
    "import ctypes",
    "import winreg",
    "import importlib",
    "from os import system",
    "from subprocess import run",
])
def test_network_and_system_imports_are_refused(src):
    with pytest.raises(CodeRejected):
        validate(src)


# ── dynamic-execution escapes ───────────────────────────────────────────
@pytest.mark.parametrize("src", [
    "eval('1+1')",
    "exec('import os')",
    "compile('x=1','<s>','exec')",
    "__import__('os').system('dir')",
    "getattr(__builtins__, 'eval')",
    "print(().__class__.__bases__)",
    "print((1).__class__.__subclasses__())",
    "f = lambda: None\nprint(f.__globals__)",
])
def test_dynamic_execution_escapes_are_refused(src):
    with pytest.raises(CodeRejected):
        validate(src)


# ── the core requirement: Jarvis must be untouchable ────────────────────
@pytest.mark.parametrize("target", [
    "D:/Jarvis/jarvis/security/permissions.py",
    r"D:\Jarvis\jarvis\tools\gmail.py",
    "client_secret_1234.json",
    "permissions.json",
    "memory.json",
    "audit.jsonl",
    "C:/Users/x/AppData/Roaming/Jarvis/config.yaml",
    "C:/Windows/System32/drivers/etc/hosts",
    "~/.ssh/id_rsa",
    ".env",
])
def test_code_cannot_reference_jarvis_or_system_paths(target):
    with pytest.raises(CodeRejected, match="protected location"):
        validate(f"data = open({target!r}).read()\nprint(data)")


def test_workspace_paths_are_allowed():
    from jarvis.tools.code_sandbox import workspace_dir

    inside = str(workspace_dir() / "data.csv")
    validate(f"print(open({inside!r}).read())")   # its own folder is fine


def test_oversized_source_is_refused():
    with pytest.raises(CodeRejected, match="too long"):
        validate("x = 1\n" * 20000)


def test_syntax_errors_are_refused_not_crashed():
    with pytest.raises(CodeRejected, match="isn't valid Python"):
        validate("def broken(:")


# ── gating behaviour ────────────────────────────────────────────────────
def test_denied_permission_never_validates_or_runs(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit, ["deny"])
    out = {t.name: t for t in code_sandbox.build_tools(ctx)}["run_code"]("print(1)")
    assert "declined" in out


def test_refusal_is_audited_with_the_reason(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit, ["allow once"])
    out = {t.name: t for t in code_sandbox.build_tools(ctx)}["run_code"]("import socket")
    assert "won't run that" in out
    entry = audit.tail(1)[0]
    assert entry["decision"] == "blocked" and entry["ok"] is False


def test_user_must_approve_before_execution(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit, ["allow once", "no"])
    out = {t.name: t for t in code_sandbox.build_tools(ctx)}["run_code"]("print(1)")
    assert "Cancelled" in out


def test_confirmation_shows_the_actual_code(tmp_path, audit):
    io = FakeIO(["allow once", "no"])
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    ctx = ToolContext(config=Config(raw={}), permissions=pm,
                      confirmer=Confirmer(io, audit), audit=audit)
    {t.name: t for t in code_sandbox.build_tools(ctx)}["run_code"]("print('hello world')")
    assert any("print('hello world')" in q for q in io.asked)


# ── end to end, actually executing ──────────────────────────────────────
def test_approved_code_runs_and_returns_output(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit, ["allow once", "yes"])
    out = {t.name: t for t in code_sandbox.build_tools(ctx)}["run_code"](
        "import statistics\nprint(statistics.mean([2, 4, 6]))"
    )
    assert "4" in out
    assert "untrusted data" in out       # output is wrapped, not trusted


def test_runtime_error_is_reported_not_raised(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit, ["allow once", "yes"])
    out = {t.name: t for t in code_sandbox.build_tools(ctx)}["run_code"](
        "print(1/0)"
    )
    assert "ZeroDivisionError" in out


# -- the confirmation's file-access promise must actually hold -----------
# run_code tells the user the program "cannot reach any file outside its own
# workspace folder". Only *protected* paths were enforced, so an ordinary
# document elsewhere on the disk was still reachable and the promise was
# wider than the enforcement.

@pytest.mark.parametrize("src", [
    "open('C:/Users/Mihir/Documents/private.txt').read()",
    "open('../../secrets.txt').read()",
    "import io\nio.open('C:/Windows/win.ini').read()",
    "import pandas as pd\npd.read_csv('C:/Users/Mihir/Documents/pay.csv')",
    "import openpyxl\nopenpyxl.load_workbook('../../x.xlsx')",
])
def test_paths_outside_the_workspace_are_refused(src):
    with pytest.raises(CodeRejected):
        validate(src)


def test_a_runtime_computed_path_is_refused():
    # It cannot be checked in advance, so it cannot be promised.
    with pytest.raises(CodeRejected):
        validate("name = 'a' + '.txt'\nopen(name).read()")


@pytest.mark.parametrize("src", [
    "f = open('report.csv', 'w')\nf.write('x')",
    "import pandas as pd\npd.DataFrame({'a': [1]}).to_csv('out.csv')",
    "import pandas as pd\npd.read_csv('sub/data.csv')",
    r"import re" "\n" r"print(re.findall(r'\d+', 'a1b22'))",
    "print('the ratio is 3:1 overall')",
    "print('meeting at 11:30')",
])
def test_ordinary_relative_work_is_still_allowed(src):
    # A regex literal starts with a backslash without being a path; refusing
    # it would block perfectly normal data work.
    validate(src)
