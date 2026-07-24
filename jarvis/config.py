"""Configuration loading: repo defaults overlaid with per-user overrides."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import user_config_file

_REPO_DEFAULT = Path(__file__).resolve().parent.parent / "config" / "default.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass
class AIToolConfig:
    name: str
    kind: str  # "openai-compatible" | "anthropic"
    base_url: str
    model: str
    api_key_env: str = ""


@dataclass
class Config:
    raw: dict = field(default_factory=dict)

    # -- convenience accessors -------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def text_mode(self) -> bool:
        return bool(self.get("assistant.text_mode", False))

    @property
    def model(self) -> str:
        return str(self.get("brain.model", "claude-opus-4-8"))

    @property
    def effort(self) -> str:
        return str(self.get("brain.effort", "low"))

    @property
    def max_tokens(self) -> int:
        return int(self.get("brain.max_tokens", 4096))

    @property
    def max_history_turns(self) -> int:
        return int(self.get("brain.max_history_turns", 20))

    @property
    def allowed_dirs(self) -> list[Path]:
        dirs = self.get("files.allowed_dirs", []) or []
        return [Path(d).expanduser().resolve() for d in dirs]

    @property
    def ai_tools(self) -> list[AIToolConfig]:
        tools = []
        for entry in self.get("ai_tools", []) or []:
            try:
                tools.append(
                    AIToolConfig(
                        name=str(entry["name"]),
                        kind=str(entry.get("kind", "openai-compatible")),
                        base_url=str(entry["base_url"]),
                        model=str(entry.get("model", "")),
                        api_key_env=str(entry.get("api_key_env", "")),
                    )
                )
            except KeyError:
                continue  # skip malformed entries rather than crash at startup
        return tools

    @property
    def google_scopes(self) -> list[str]:
        return list(self.get("google.scopes", []) or [])

    @property
    def google_credentials_file(self) -> str:
        return str(self.get("google.credentials_file", "") or "")

    @property
    def require_confirmation(self) -> bool:
        return bool(self.get("security.require_confirmation", True))

    @property
    def session_grant_minutes(self) -> int:
        return int(self.get("security.session_grant_minutes", 480))


def load_config(explicit_path: str | None = None) -> Config:
    """Load repo defaults, then overlay the user's config (or an explicit file)."""
    data: dict = {}
    if _REPO_DEFAULT.exists():
        data = yaml.safe_load(_REPO_DEFAULT.read_text(encoding="utf-8")) or {}

    override_path = Path(explicit_path) if explicit_path else user_config_file()
    if override_path.exists():
        override = yaml.safe_load(override_path.read_text(encoding="utf-8")) or {}
        if not isinstance(override, dict):
            raise ValueError(f"Config file {override_path} must be a YAML mapping")
        data = _deep_merge(data, override)

    return Config(raw=data)
