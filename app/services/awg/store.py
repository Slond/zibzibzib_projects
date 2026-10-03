"""SQLite: clients, sessions, and traffic counters."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_DB_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    public_key TEXT NOT NULL UNIQUE,
    address TEXT NOT NULL,
    conf TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS traffic_state (
    public_key TEXT PRIMARY KEY,
    rx_counter INTEGER NOT NULL,
    tx_counter INTEGER NOT NULL,
    rx_total INTEGER NOT NULL,
    tx_total INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS traffic_samples (
    id INTEGER PRIMARY KEY,
    public_key TEXT NOT NULL,
    ts INTEGER NOT NULL,
    rx_delta INTEGER NOT NULL,
    tx_delta INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON traffic_samples(ts);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
"""


@dataclass
class ClientRow:
    id: int
    name: str
    public_key: str
    address: str
    conf: str
    created_at: int
    revoked_at: int | None


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return secrets.compare_digest(digest.hex(), digest_hex)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.conn.close()

    def set_password(self, password: str) -> None:
        with _DB_LOCK:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES('password', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (hash_password(password),),
            )
            self.conn.commit()

    def has_password(self) -> bool:
        with _DB_LOCK:
            row = self.conn.execute("SELECT value FROM settings WHERE key = 'password'").fetchone()
        return row is not None

    def check_password(self, password: str) -> bool:
        with _DB_LOCK:
            row = self.conn.execute("SELECT value FROM settings WHERE key = 'password'").fetchone()
        if row is None:
            return False
        return verify_password(password, row["value"])

    def new_session(self, now: int | None = None) -> str:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = int(time.time()) if now is None else now
        with _DB_LOCK:
            self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            self.conn.execute(
                "INSERT INTO sessions(token_hash, created_at, expires_at) VALUES (?, ?, ?)",
                (digest, now, now + 7 * 86400),
            )
            self.conn.commit()
        return token

    def session_valid(self, token: str, now: int | None = None) -> bool:
        if not token:
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = int(time.time()) if now is None else now
        with _DB_LOCK:
            row = self.conn.execute(
                "SELECT 1 FROM sessions WHERE token_hash = ? AND expires_at > ?",
                (digest, now),
            ).fetchone()
        return row is not None

    def drop_session(self, token: str) -> None:
        if not token:
            return
        digest = hashlib.sha256(token.encode()).hexdigest()
        with _DB_LOCK:
            self.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
            self.conn.commit()

    def insert_client(
        self,
        *,
        name: str,
        public_key: str,
        address: str,
        conf: str,
        created_at: int,
    ) -> ClientRow:
        with _DB_LOCK:
            cur = self.conn.execute(
                "INSERT INTO clients(name, public_key, address, conf, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, public_key, address, conf, created_at),
            )
            self.conn.commit()
            row_id = int(cur.lastrowid)
        row = self.get_client(row_id)
        if row is None:
            raise RuntimeError("client insert failed")
        return row

    def get_client(self, client_id: int) -> ClientRow | None:
        with _DB_LOCK:
            row = self.conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        return _client(row) if row else None

    def get_by_public_key(self, public_key: str) -> ClientRow | None:
        with _DB_LOCK:
            row = self.conn.execute("SELECT * FROM clients WHERE public_key = ?", (public_key,)).fetchone()
        return _client(row) if row else None

    def list_clients(self, *, active_only: bool = False) -> list[ClientRow]:
        sql = "SELECT * FROM clients"
        if active_only:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY created_at, id"
        with _DB_LOCK:
            rows = self.conn.execute(sql).fetchall()
        return [_client(row) for row in rows]

    def revoke(self, client_id: int, now: int) -> None:
        with _DB_LOCK:
            self.conn.execute(
                "UPDATE clients SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now, client_id),
            )
            self.conn.commit()

    def apply_counter(self, public_key: str, rx: int, tx: int, now: int) -> tuple[int, int]:
        """Fold a daemon counter into totals.

        The first reading becomes the baseline and does not land in the chart:
        those bytes were transferred before the panel was watching. Later
        growth is stored as a sample. A counter that went backwards means the
        daemon restarted, so the new value is the whole delta.
        """
        with _DB_LOCK:
            row = self.conn.execute(
                "SELECT rx_counter, tx_counter, rx_total, tx_total FROM traffic_state WHERE public_key = ?",
                (public_key,),
            ).fetchone()
            if row is None:
                delta_rx, delta_tx = 0, 0
                total_rx, total_tx = rx, tx
            else:
                delta_rx = rx - int(row["rx_counter"]) if rx >= int(row["rx_counter"]) else rx
                delta_tx = tx - int(row["tx_counter"]) if tx >= int(row["tx_counter"]) else tx
                total_rx = int(row["rx_total"]) + delta_rx
                total_tx = int(row["tx_total"]) + delta_tx
            self.conn.execute(
                "INSERT INTO traffic_state(public_key, rx_counter, tx_counter, rx_total, tx_total, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(public_key) DO UPDATE SET "
                "rx_counter = excluded.rx_counter, tx_counter = excluded.tx_counter, "
                "rx_total = excluded.rx_total, tx_total = excluded.tx_total, updated_at = excluded.updated_at",
                (public_key, rx, tx, total_rx, total_tx, now),
            )
            if delta_rx or delta_tx:
                self.conn.execute(
                    "INSERT INTO traffic_samples(public_key, ts, rx_delta, tx_delta) VALUES (?, ?, ?, ?)",
                    (public_key, now, delta_rx, delta_tx),
                )
            self.conn.execute("DELETE FROM traffic_samples WHERE ts < ?", (now - 14 * 86400,))
            self.conn.commit()
        return delta_rx, delta_tx

    def state_map(self) -> dict[str, tuple[int, int]]:
        with _DB_LOCK:
            rows = self.conn.execute("SELECT public_key, rx_total, tx_total FROM traffic_state").fetchall()
        return {row["public_key"]: (int(row["rx_total"]), int(row["tx_total"])) for row in rows}

    def samples_since(self, since: int) -> list[tuple[str, int, int, int]]:
        with _DB_LOCK:
            rows = self.conn.execute(
                "SELECT public_key, ts, rx_delta, tx_delta FROM traffic_samples WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
        return [(row["public_key"], int(row["ts"]), int(row["rx_delta"]), int(row["tx_delta"])) for row in rows]


def _client(row: sqlite3.Row) -> ClientRow:
    return ClientRow(
        id=int(row["id"]),
        name=row["name"],
        public_key=row["public_key"],
        address=row["address"],
        conf=row["conf"],
        created_at=int(row["created_at"]),
        revoked_at=None if row["revoked_at"] is None else int(row["revoked_at"]),
    )
