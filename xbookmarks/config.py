from __future__ import annotations

import os
import tomllib
from pathlib import Path

ENV_FILE = Path(".env")        # git-ignored; holds X_AUTH_TOKEN / X_CT0 on machines without a screen

DEFAULTS: dict = {
    "paths": {"data_dir": "data/xbookmarks", "reports_dir": "reports"},
    "browser": {"headless": True, "channel": "", "executable_path": "", "max_items": 400, "keep_raw": True},
    "links": {"enabled": True, "max_per_run": 60, "timeout": 15},
    "email": {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 465, "username": "", "to": "",
              "send_when_empty": False},
}


def load(path: Path | None) -> dict:
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    candidates = [path] if path else [Path("xbookmarks.local.toml"), Path("xbookmarks.toml")]
    for cand in candidates:
        if cand and cand.exists():
            with cand.open("rb") as fh:
                user = tomllib.load(fh)
            for section, values in user.items():
                cfg.setdefault(section, {}).update(values)
            break
    return cfg


def parse_env(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines (dotenv style): comments, blank lines, ``export``, quotes."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def load_env(path: Path | None = None) -> list[str]:
    """Put the variables of ``.env`` into the process environment and return the names set.

    Variables that are already set win, so a value exported in the shell or saved on a
    cloud environment is never overridden by the file.
    """
    path = path or ENV_FILE
    if not path.exists():
        return []
    names = []
    for key, value in parse_env(path.read_text(encoding="utf-8")).items():
        if key not in os.environ:
            os.environ[key] = value
            names.append(key)
    return names
