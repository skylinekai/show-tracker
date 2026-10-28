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
CREATE TABLE IF NOT EXISTS users (
    id {pk},
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS companies (
    id {pk},
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    domain TEXT,
    linkedin_url TEXT,
    industry TEXT,
    size TEXT,
    employees INTEGER,
    revenue TEXT,
    location TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT,
    researched_at TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
    id {pk},
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    am_person_id TEXT,
    name TEXT,
    first_name TEXT,
    last_name TEXT,
    title TEXT,
    email TEXT,
    email_status TEXT,
    linkedin_url TEXT,
    location TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (company_id, linkedin_url)
);
CREATE TABLE IF NOT EXISTS leads (
    id {pk},
    exhibitor_id INTEGER NOT NULL REFERENCES exhibitors(id) ON DELETE CASCADE,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'review',
    score INTEGER,
    score_notes TEXT,
    subject TEXT,
    opener TEXT,
    sequence_id TEXT,
    send_after TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    approved_by TEXT,
    sent_at TEXT,
    error TEXT,
    reply_label TEXT,
    reply_snippet TEXT,
    replied_at TEXT,
    UNIQUE (show_id, contact_id)
);
CREATE TABLE IF NOT EXISTS discovered_shows (
    id {pk},
    name TEXT NOT NULL,
    url TEXT,
    website TEXT,
    platform TEXT,
    start_date TEXT,
    location TEXT,
    industry TEXT,
    reason TEXT,
    found_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'suggested',
    UNIQUE (name)
);
CREATE TABLE IF NOT EXISTS activity (
    id {pk},
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    ref TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_exh_show ON exhibitors(show_id);
CREATE INDEX IF NOT EXISTS ix_exh_first_seen ON exhibitors(first_seen_at);
CREATE INDEX IF NOT EXISTS ix_runs_show ON runs(show_id);
CREATE INDEX IF NOT EXISTS ix_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS ix_contacts_email ON contacts(email);
CREATE INDEX IF NOT EXISTS ix_activity_created ON activity(created_at);
"""

# Columns added after the first release. Added one by one so existing databases upgrade in place.
MIGRATIONS = [
    ("shows", "prospecting", "INTEGER NOT NULL DEFAULT 0"),
    ("shows", "auto_send", "INTEGER NOT NULL DEFAULT 0"),
    ("shows", "sequence_id", "TEXT"),
    ("exhibitors", "booth_sqft", "INTEGER"),
    ("exhibitors", "booth_dims", "TEXT"),
    ("exhibitors", "hall", "TEXT"),
    ("exhibitors", "company_id", "INTEGER"),
    ("exhibitors", "score", "INTEGER"),
    ("exhibitors", "score_notes", "TEXT"),
    ("exhibitors", "stage", "TEXT"),
    ("exhibitors", "alerted_at", "TEXT"),
]


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
    with _lock:
        with connect() as db:
            for stmt in SCHEMA.format(pk=pk).split(";"):
                if stmt.strip():
                    db.execute(stmt)
        for table, column, decl in MIGRATIONS:
            with connect() as db:
                if column not in _columns(db, table):
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _columns(db: Conn, table: str) -> set[str]:
    if IS_PG:
        rows = db.all("SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))
        return {r["column_name"] for r in rows}
    return {r["name"] for r in db.all(f"PRAGMA table_info({table})")}


def log_activity(kind: str, message: str, ref: str | None = None) -> None:
    """Short entries for the activity feed and daily briefing."""
    with connect() as db:
        db.execute("INSERT INTO activity (kind, message, ref, created_at) VALUES (?, ?, ?, ?)",
                   (kind, message[:500], ref, now()))
