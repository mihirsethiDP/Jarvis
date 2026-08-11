from __future__ import annotations

import pytest

from jarvis.config import Config
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext
from jarvis.tools.local_files import PathNotAllowed, resolve_safe

from conftest import FakeIO


def make_ctx(tmp_path, audit, allowed=None):
    allowed_dirs = [str(p) for p in (allowed if allowed is not None else [tmp_path / "docs"])]
    for d in allowed_dirs:
        __import__("pathlib").Path(d).mkdir(parents=True, exist_ok=True)
    cfg = Config(raw={"files": {"allowed_dirs": allowed_dirs}})
    io = FakeIO([])
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    return ToolContext(config=cfg, permissions=pm,
                       confirmer=Confirmer(io, audit), audit=audit)


def test_path_inside_allowlist_resolves(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit)
    target = tmp_path / "docs" / "note.txt"
    target.write_text("hi")
    assert resolve_safe(ctx, str(target)) == target.resolve()


def test_traversal_escape_is_blocked(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    with pytest.raises(PathNotAllowed):
        resolve_safe(ctx, str(tmp_path / "docs" / ".." / "secret.txt"))


def test_absolute_path_outside_is_blocked(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit)
    with pytest.raises(PathNotAllowed):
        resolve_safe(ctx, r"C:\Windows\System32\drivers\etc\hosts")


def test_relative_path_anchors_to_first_allowed_dir(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit)
    resolved = resolve_safe(ctx, "sub/report.txt")
    assert resolved == (tmp_path / "docs" / "sub" / "report.txt").resolve()


def test_no_allowed_dirs_blocks_relative_paths(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit, allowed=[])
    # No configured dirs at all -> nothing resolves.
    with pytest.raises(PathNotAllowed):
        resolve_safe(ctx, "anything.txt")


def test_ads_stream_paths_are_rejected(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit)
    with pytest.raises(PathNotAllowed):
        resolve_safe(ctx, str(tmp_path / "docs" / "note.txt:hidden"))


def test_reserved_device_names_are_rejected(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit)
    for name in ["CON", "nul.txt", "COM1.log"]:
        with pytest.raises(PathNotAllowed):
            resolve_safe(ctx, str(tmp_path / "docs" / name))


def test_sandbox_can_import_the_libraries_it_allowlists(tmp_path, audit):
    # numpy/pandas were on the allowlist and advertised for "data crunching",
    # but the child ran with -S, which skips site.py and therefore leaves
    # site-packages off sys.path — so every one of them failed to import.
    from jarvis.config import load_config
    from jarvis.memory import MemoryStore
    from jarvis.security import Confirmer, PermissionManager
    from jarvis.tools import ToolContext, code_sandbox
    from conftest import FakeIO

    io = FakeIO(["allow once", "yes"])
    ctx = ToolContext(config=load_config(), permissions=PermissionManager(io, audit),
                      confirmer=Confirmer(io, audit), audit=audit, memory=MemoryStore())
    run = {t.name: t for t in code_sandbox.build_tools(ctx)}["run_code"]
    out = run("import numpy as np\nprint(int(np.array([1, 2, 3]).sum()))")
    assert "6" in out, out


def test_sandbox_still_refuses_system_access(tmp_path, audit):
    # Dropping -S must not cost any isolation: the AST allowlist rejects
    # these before a subprocess is ever started.
    from jarvis.config import load_config
    from jarvis.memory import MemoryStore
    from jarvis.security import Confirmer, PermissionManager
    from jarvis.tools import ToolContext, code_sandbox
    from conftest import FakeIO

    for src in ("import os\nos.system('echo hi')",
                "import socket\nprint(socket)",
                "import subprocess\nprint(subprocess)",
                "eval('1+1')"):
        io = FakeIO(["allow once", "yes"])
        ctx = ToolContext(config=load_config(), permissions=PermissionManager(io, audit),
                          confirmer=Confirmer(io, audit), audit=audit, memory=MemoryStore())
        run = {t.name: t for t in code_sandbox.build_tools(ctx)}["run_code"]
        assert "won't run that" in run(src), src
