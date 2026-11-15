"""Load settings from config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_PATHS = [Path("config.toml"), Path.home() / "ad-arta" / "config.toml"]


@dataclass
class TVConfig:
    host: str
    mac: str = ""
    token_file: str = "tv-token.txt"
    input_key: str = ""


@dataclass
class AppleTVConfig:
    identifier: str = ""
    host: str = ""


@dataclass
class Config:
    tv: TVConfig
    appletv: AppleTVConfig = field(default_factory=AppleTVConfig)


def find_config(path: str | None) -> Path:
    if path:
        return Path(path)
    for candidate in DEFAULT_PATHS:
        if candidate.exists():
            return candidate
    raise SystemExit(
        "No config found. Copy config.example.toml to config.toml "
        "or pass --config /path/to/config.toml"
    )


def _section(config_path: Path, raw: dict, name: str, cls):
    values = raw.get(name, {})
    if not isinstance(values, dict):
        raise SystemExit(f"{config_path}: [{name}] must be a section, not a single value")
    unknown = set(values) - {f.name for f in fields(cls)}
    if unknown:
        raise SystemExit(f"{config_path}: unknown key(s) in [{name}]: {', '.join(sorted(unknown))}")
    return cls(**values)


def load_config(path: str | None = None) -> Config:
    config_path = find_config(path)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{config_path}: invalid TOML: {e}") from e

    unknown = set(raw) - {"tv", "appletv"}
    if unknown:
        raise SystemExit(f"{config_path}: unknown section(s): {', '.join(sorted(unknown))}")
    if not isinstance(raw.get("tv"), dict) or not raw["tv"].get("host"):
        raise SystemExit(f"{config_path}: [tv] host is required")

    tv = _section(config_path, raw, "tv", TVConfig)
    # A relative token path is relative to the config file, not the cwd.
    token = Path(tv.token_file).expanduser()
    if not token.is_absolute():
        token = config_path.parent.resolve() / token
    tv.token_file = str(token)

    return Config(tv=tv, appletv=_section(config_path, raw, "appletv", AppleTVConfig))
