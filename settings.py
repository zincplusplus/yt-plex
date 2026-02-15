"""Persistent settings via data/settings.json.

Priority: settings.json > env vars > hardcoded defaults.
"""

import json
import os
from pathlib import Path

SETTINGS_FILE = Path("data/settings.json")


def _load() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def get(key: str, env_default: str) -> str:
    """Return settings.json[key] if present, else env_default."""
    data = _load()
    if key in data:
        return data[key]
    return env_default


def save(updates: dict):
    """Merge updates into settings.json."""
    data = _load()
    data.update(updates)
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2) + "\n")
