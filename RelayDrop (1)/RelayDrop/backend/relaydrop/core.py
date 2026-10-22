"""Shared helpers: configuration, SQLite access, hashing and validation (ports of lib/api.ts helpers)."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import sqlite3
import time
import unicodedata
from pathlib import Path

from flask import current_app, g

MIB = 1024 * 1024
DAY = 86_400_000
HEX64 = re.compile(r"^[a-f0-9]{64}$")

MAX_FILES = 20
MAX_FILE_BYTES = 50 * MIB
MAX_TOTAL_BYTES = 100 * MIB
EXPIRY_HOURS = (1, 24, 72, 168)

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, code TEXT NOT NULL UNIQUE, share_token TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL, note TEXT NOT NULL, password_hash TEXT, salt TEXT,
  status TEXT NOT NULL DEFAULT 'staging', created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  total_size INTEGER NOT NULL, file_count INTEGER NOT NULL, download_count INTEGER NOT NULL DEFAULT 0, purged_at INTEGER
);
CREATE INDEX IF NOT EXISTS owner_created ON transfers(owner, created_at);
CREATE INDEX IF NOT EXISTS transfer_expiry ON transfers(expires_at);
CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY, transfer_id TEXT NOT NULL REFERENCES transfers(id) ON DELETE CASCADE,
  object_key TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, mime TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS file_transfer ON files(transfer_id);
CREATE TABLE IF NOT EXISTS download_tokens (
  hash TEXT PRIMARY KEY, transfer_id TEXT NOT NULL REFERENCES transfers(id) ON DELETE CASCADE, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, hits INTEGER NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL, subject TEXT NOT NULL, message TEXT NOT NULL,
  reference TEXT NOT NULL DEFAULT '', transfer_id TEXT, declarations INTEGER NOT NULL DEFAULT 0, signature TEXT NOT NULL DEFAULT '',
  receipt_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', response TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ticket_created ON tickets(created_at);
CREATE TABLE IF NOT EXISTS admin_sessions (hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL);
"""


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra) -> None:
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def fail(status: int, message: str, **extra):
    raise HttpError(status, message, **extra)


def now_ms() -> int:
    return int(time.time() * 1000)


def random_hex(nbytes: int = 32) -> str:
    return secrets.token_hex(nbytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def password_hash(password: str, salt: str) -> str:
    # PBKDF2-SHA256, 310,000 iterations, 32-byte output, salt used as its UTF-8 hex text (same as the original).
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310_000, 32).hex()


def text_field(value, maximum: int, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        fail(400, f"{field} must be at most {maximum} characters.")
    return value.strip()


def password_input(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 256:
        fail(400, "Password must be at most 256 characters.")
    return value


def is_int(value) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)", re.I)


def safe_names(raw: list[str]) -> list[str]:
    """Sanitise filenames: strip paths/control chars, clip to 200 bytes, avoid Windows reserved names, de-duplicate."""
    used: set[str] = set()
    result: list[str] = []
    for i, value in enumerate(raw):
        name = value.encode("utf-8", "replace").decode("utf-8", "replace")
        name = name.replace("\\", "/").split("/")[-1]
        name = unicodedata.normalize("NFC", name)
        name = "".join("_" if (unicodedata.category(c) in ("Cc", "Cf") or c in '<>:"|?*') else c for c in name)
        name = re.sub(r"^[. ]+|[. ]+$", "", name)
        clipped = ""
        for char in name:
            if len((clipped + char).encode("utf-8")) > 200:
                break
            clipped += char
        name = clipped or f"file-{i + 1}"
        if _RESERVED.match(name):
            name = "_" + name
        dot = name.rfind(".")
        base, ext = (name[:dot], name[dot:]) if dot > 0 else (name, "")
        candidate, n = name, 2
        while candidate.lower() in used:
            candidate = f"{base} ({n}){ext}"
            n += 1
        used.add(candidate.lower())
        result.append(candidate)
    return result


# ---------------------------------------------------------------- database
def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = path or current_app.config["DB_PATH"]
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app) -> None:
    app.config["DATA_DIR"].mkdir(parents=True, exist_ok=True)
    app.config["FILES_DIR"].mkdir(parents=True, exist_ok=True)
    conn = connect(app.config["DB_PATH"])
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def one(db: sqlite3.Connection, sql: str, *args):
    row = db.execute(sql, args).fetchone()
    return dict(row) if row else None


def many(db: sqlite3.Connection, sql: str, *args) -> list[dict]:
    return [dict(r) for r in db.execute(sql, args).fetchall()]


# ---------------------------------------------------------------- file storage (replaces the R2 bucket)
def object_path(key: str) -> Path:
    base = current_app.config["FILES_DIR"].resolve()
    path = (base / key).resolve()
    if base not in path.parents:
        fail(400, "Invalid storage key.")
    return path


def delete_objects(keys: list[str]) -> None:
    for key in keys:
        p = object_path(key)
        p.unlink(missing_ok=True)
        Path(str(p) + ".part").unlink(missing_ok=True)
        try:
            p.parent.rmdir()
        except OSError:
            pass


def delete_transfer_dir(transfer_id: str) -> None:
    shutil.rmtree(object_path(transfer_id), ignore_errors=True)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")
