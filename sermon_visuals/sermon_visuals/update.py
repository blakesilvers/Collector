"""Self-update without git: download the branch ZIP from GitHub and replace the code in place.
config.toml, .env, programs/, assets/ and output/ are never touched."""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

REPO = "blakesilvers/Collector"
BRANCH = "claude/serene-mendel-0btcz1"
SUBDIR = "sermon_visuals"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"

# Paths (relative to the sermon_visuals project folder) that the update replaces.
CODE_PATHS = ("sermon_visuals", "tests", "pyproject.toml", "README.md")


def install_root() -> Path:
    """The project folder that holds config.toml: the parent of the installed package directory."""
    return Path(__file__).resolve().parent.parent


def download(url: str = ZIP_URL) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "sermon-visuals-updater"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def apply_zip(data: bytes, root: Path) -> list[str]:
    """Copy the project's code paths out of a GitHub branch ZIP into root. Returns the paths replaced."""
    replaced: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf, tempfile.TemporaryDirectory() as tmp:
        zf.extractall(tmp)
        top = next(p for p in Path(tmp).iterdir() if p.is_dir())
        src = top / SUBDIR
        if not src.is_dir():
            raise RuntimeError(f"ZIP does not contain {SUBDIR}/")
        for rel in CODE_PATHS:
            s, d = src / rel, root / rel
            if not s.exists():
                continue
            if s.is_dir():
                if d.exists():
                    shutil.rmtree(d)
                shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(s, d)
            replaced.append(rel)
    return replaced


def run_update(reinstall: bool = True) -> int:
    root = install_root()
    print(f"Updating code in {root} from {ZIP_URL}")
    data = download()
    replaced = apply_zip(data, root)
    print("Replaced: " + ", ".join(replaced))
    if reinstall:
        print("Installing any new dependencies...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(root)], check=False)
    print("Done. Your config.toml, .env, programs, assets and output were left alone.")
    print("New settings appear in sermon_visuals/config.example.toml; copy any you want into config.toml.")
    return 0
