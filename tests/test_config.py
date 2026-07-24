from __future__ import annotations

from jarvis.config import Config, _deep_merge, load_config


def test_deep_merge_overrides_nested_keys():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    override = {"a": {"b": 9}}
    merged = _deep_merge(base, override)
    assert merged == {"a": {"b": 9, "c": 2}, "d": 3}
    assert base["a"]["b"] == 1  # base untouched


def test_dotted_get_with_default():
    cfg = Config(raw={"brain": {"model": "claude-opus-4-8"}})
    assert cfg.get("brain.model") == "claude-opus-4-8"
    assert cfg.get("brain.missing", 42) == 42
    assert cfg.get("nope.nope") is None


def test_packaged_defaults_load(tmp_path):
    # Hermetic: point the override at a nonexistent file so the developer's
    # own %APPDATA%\Jarvis\config.yaml never leaks into the assertion.
    cfg = load_config(str(tmp_path / "no-user-config.yaml"))
    assert cfg.model
    assert cfg.get("assistant.name") == "Jarvis"
    assert cfg.session_grant_minutes > 0


def test_ai_tools_skips_malformed_entries():
    cfg = Config(raw={"ai_tools": [
        {"name": "good", "base_url": "http://x", "model": "m"},
        {"kind": "openai-compatible"},  # missing name/base_url
    ]})
    tools = cfg.ai_tools
    assert len(tools) == 1
    assert tools[0].name == "good"
