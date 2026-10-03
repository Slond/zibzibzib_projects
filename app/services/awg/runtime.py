"""Where the panel keeps its database and how it finds the daemon."""

from __future__ import annotations

import os
from pathlib import Path

from app.services.awg.manage import refresh
from app.services.awg.store import Store


def awg_dir() -> Path:
    return Path(os.environ.get("AWG_DIR", "/etc/amneziawg"))


def awg_socket() -> str:
    return os.environ.get("AWG_SOCKET", "/var/run/amneziawg/awg0.sock")


def db_path() -> Path:
    raw = Path(os.environ.get("AWG_DB", "data/awg.db"))
    if raw.is_absolute():
        return raw
    return Path.cwd() / raw


def open_store() -> Store:
    return Store(db_path())


def poll_traffic() -> None:
    """One counter sample. Safe to call when the daemon is not installed yet."""
    store = open_store()
    try:
        refresh(store, awg_socket())
    finally:
        store.close()
