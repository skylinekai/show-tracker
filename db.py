"""Tiny database layer: SQLite locally, Postgres on Render (set DATABASE_URL).

Queries are written with `?` placeholders; they are rewritten to `%s` for Postgres.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///showtracker.db")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    id {pk},
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    start_date TEXT,
    location TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    first_checked_at TEXT,
    last_checked_at TEXT,
    last_count INTEGER,
    last_error TEXT,
    next_edition_url TEXT,
    parent_show_id INTEGER
);
CREATE TABLE IF NOT EXISTS exhibitors (
    id {pk},
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    booth TEXT,
    description TEXT,
    website TEXT,
    profile_url TEXT,
    likely_junk INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    removed_at TEXT,
    in_first_snapshot INTEGER NOT NULL DEFAULT 0,
    pushed_at TEXT,
    UNIQUE (show_id, name_key)
);
CREATE TABLE IF NOT EXISTS runs (
    id {pk},
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    found INTEGER,
    added INTEGER,
    removed INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_exh_show ON exhibitors(show_id);
CREATE INDEX IF NOT EXISTS ix_exh_first_seen ON exhibitors(first_seen_at);
CREATE INDEX IF NOT EXISTS ix_runs_show ON runs(show_id);
"""


class Conn:
    def __init__(self, raw):
        self.raw = raw

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if IS_PG else sql

    def execute(self, sql: str, params: tuple | list = ()):
        cur = self.raw.cursor()
        cur.execute(self._sql(sql), tuple(params))
        return cur

    def all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        cur = self.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def insert(self, sql: str, params: tuple | list = ()) -> int:
        if IS_PG:
            return self.execute(sql + " RETURNING id", params).fetchone()[0]
        return self.execute(sql, params).lastrowid


def _connect():
    if IS_PG:
        import psycopg2  # installed on Render via requirements.txt

        return psycopg2.connect(DATABASE_URL)
    path = DATABASE_URL.replace("sqlite:///", "", 1)
    raw = sqlite3.connect(path, timeout=30, check_same_thread=False)
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA journal_mode = WAL")
    return raw


@contextmanager
def connect():
    raw = _connect()
    try:
        yield Conn(raw)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def init_db() -> None:
    pk = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with _lock, connect() as db:
        for stmt in SCHEMA.format(pk=pk).split(";"):
            if stmt.strip():
                db.execute(stmt)
