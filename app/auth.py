"""Email + password accounts.

The first account comes from ADMIN_EMAIL / ADMIN_PASSWORD. If those aren't set, the first person to sign in
with the old APP_PASSWORD becomes the admin, so existing installs keep working.
"""
from __future__ import annotations

import os
import secrets
import string

from werkzeug.security import check_password_hash, generate_password_hash

from .db import connect, now

MIN_PASSWORD = 8
USER_COLS = "id, email, name, job_title, phone, is_admin, created_at, last_login_at, mailbox, sequence_id, briefing"


def secret_key() -> str:
    from . import settings as cfg

    key = os.environ.get("SECRET_KEY") or cfg.internal("_secret_key")
    if not key:
        key = secrets.token_hex(32)
        cfg.set_internal("_secret_key", key)
    return key


def generate_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    alphabet = "".join(c for c in alphabet if c not in "Il1O0o")  # easy to read out
    return "".join(secrets.choice(alphabet) for _ in range(length))


def user_count() -> int:
    with connect() as db:
        return db.one("SELECT COUNT(*) AS n FROM users")["n"]


def create_user(email: str, password: str, name: str = "", is_admin: bool = False,
                job_title: str = "", phone: str = "") -> int:
    email = email.strip().lower()
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"The password needs at least {MIN_PASSWORD} characters.")
    with connect() as db:
        if db.one("SELECT id FROM users WHERE email = ?", (email,)):
            raise ValueError("There's already an account with that email.")
        return db.insert(
            "INSERT INTO users (email, name, job_title, phone, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (email, name.strip() or None, job_title.strip() or None, phone.strip() or None,
             generate_password_hash(password), 1 if is_admin else 0, now()))


def update_user(user_id: int, *, name: str, job_title: str, phone: str, email: str | None = None,
                is_admin: bool | None = None) -> None:
    sets, params = ["name = ?", "job_title = ?", "phone = ?"], [name.strip() or None, job_title.strip() or None,
                                                              phone.strip() or None]
    if email is not None:
        email = email.strip().lower()
        with connect() as db:
            clash = db.one("SELECT id FROM users WHERE email = ? AND id <> ?", (email, user_id))
        if clash:
            raise ValueError("Another account already uses that email.")
        sets.append("email = ?")
        params.append(email)
    if is_admin is not None:
        sets.append("is_admin = ?")
        params.append(1 if is_admin else 0)
    with connect() as db:
        db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params + [user_id])


def ensure_admin() -> None:
    email, password = os.environ.get("ADMIN_EMAIL", ""), os.environ.get("ADMIN_PASSWORD", "")
    if email and password and user_count() == 0:
        with connect() as db:
            db.insert("INSERT INTO users (email, password_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
                      (email.strip().lower(), generate_password_hash(password), now()))


def check_password(user_id: int, password: str) -> bool:
    with connect() as db:
        row = db.one("SELECT password_hash FROM users WHERE id = ?", (user_id,))
    return bool(row) and check_password_hash(row["password_hash"], password or "")


def authenticate(email: str, password: str) -> dict | None:
    email = (email or "").strip().lower()
    with connect() as db:
        user = db.one("SELECT * FROM users WHERE email = ?", (email,))
    if user and check_password_hash(user["password_hash"], password or ""):
        with connect() as db:
            db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now(), user["id"]))
        return user
    legacy = os.environ.get("APP_PASSWORD", "")
    if not user and user_count() == 0 and legacy and email and secrets.compare_digest(password or "", legacy):
        with connect() as db:
            uid = db.insert("INSERT INTO users (email, password_hash, is_admin, created_at, last_login_at) "
                            "VALUES (?, ?, 1, ?, ?)", (email, generate_password_hash(password), now(), now()))
        return get_user(uid)
    return None


def get_user(user_id) -> dict | None:
    if not user_id:
        return None
    with connect() as db:
        return db.one(f"SELECT {USER_COLS} FROM users WHERE id = ?", (user_id,))


def set_password(user_id: int, password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"The password needs at least {MIN_PASSWORD} characters.")
    with connect() as db:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user_id))


def list_users() -> list[dict]:
    with connect() as db:
        return db.all(f"SELECT {USER_COLS} FROM users ORDER BY COALESCE(name, email)")


def update_outreach(user_id: int, *, mailbox: str, sequence_id: str, briefing: bool) -> None:
    with connect() as db:
        db.execute("UPDATE users SET mailbox = ?, sequence_id = ?, briefing = ? WHERE id = ?",
                   (mailbox.strip().lower() or None, sequence_id.strip() or None, 1 if briefing else 0, user_id))


def delete_user(user_id: int, hand_to: int | None = None) -> None:
    """Remove a user; their shows and pending outreach pass to hand_to (or become unassigned)."""
    with connect() as db:
        db.execute("UPDATE shows SET owner_id = ? WHERE owner_id = ?", (hand_to, user_id))
        db.execute("UPDATE leads SET owner_id = ? WHERE owner_id = ? AND status IN ('review', 'approved')",
                   (hand_to, user_id))
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))


def display_name(user: dict | None) -> str:
    if not user:
        return "Unassigned"
    return user.get("name") or user["email"].split("@")[0]
