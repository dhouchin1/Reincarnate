"""Data directory resolution. Directories are created lazily via ensure()."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    home = os.environ.get("REINCARNATE_HOME")
    return Path(home).expanduser() if home else Path.home() / ".reincarnate"


def db_path() -> Path:
    return data_dir() / "reincarnate.db"


def sandbox_dir() -> Path:
    return data_dir() / "sandbox"


def dossiers_dir() -> Path:
    return data_dir() / "dossiers"


def kits_dir() -> Path:
    return data_dir() / "kits"


def ensure(path: Path) -> Path:
    """Create `path` (a directory) if missing and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path
