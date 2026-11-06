from __future__ import annotations

import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "paths": {"data_dir": "data/xbookmarks", "reports_dir": "reports"},
    "browser": {"headless": True, "channel": "", "executable_path": "", "max_items": 400, "keep_raw": True},
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
