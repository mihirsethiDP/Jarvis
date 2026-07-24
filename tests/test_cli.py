from __future__ import annotations

import pytest

import jarvis.app as app_mod
from jarvis.__main__ import main


@pytest.fixture
def fake_app(monkeypatch):
    calls: dict = {}

    class FakeApp:
        def __init__(self, config, *, force_text=False, with_ui=False):
            calls["text"] = force_text
            calls["ui"] = with_ui

        def run(self):
            calls["ran"] = True

    monkeypatch.setattr(app_mod, "JarvisApp", FakeApp)
    return calls


@pytest.mark.parametrize("argv,text,ui", [
    ([], False, False),
    (["--text"], True, False),
    (["--text", "run"], True, False),      # flag before subcommand must survive
    (["run", "--text"], True, False),
    (["--ui", "run", "--text"], True, True),
    (["run"], False, False),
])
def test_cli_flag_positions(fake_app, argv, text, ui):
    assert main(argv) == 0
    assert fake_app["text"] is text
    assert fake_app["ui"] is ui
    assert fake_app["ran"] is True


def test_run_accepts_config_after_subcommand(fake_app, tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("assistant:\n  name: TestBot\n", encoding="utf-8")
    assert main(["run", "--config", str(cfg)]) == 0
    assert fake_app["ran"] is True
