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
