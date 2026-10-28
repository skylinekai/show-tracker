"""Email + password accounts.

The first account comes from ADMIN_EMAIL / ADMIN_PASSWORD. If those aren't set, the first person to sign in
with the old APP_PASSWORD becomes the admin, so existing installs keep working.
"""
from __future__ import annotations

import os
import secrets

from werkzeug.security import check_password_hash, generate_password_hash

from .db import connect, now


def secret_key() -> str:
    from . import settings as cfg

    key = os.environ.get("SECRET_KEY") or cfg.internal("_secret_key")
    if not key:
        key = secrets.token_hex(32)
        cfg.set_internal("_secret_key", key)
    return key


def user_count() -> int:
    with connect() as db:
        return db.one("SELECT COUNT(*) AS n FROM users")["n"]


def create_user(email: str, password: str, name: str = "", is_admin: bool = False) -> int:
    email = email.strip().lower()
    with connect() as db:
        if db.one("SELECT id FROM users WHERE email = ?", (email,)):
            raise ValueError("There's already an account with that email.")
        return db.insert("INSERT INTO users (email, name, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                         (email, name.strip() or None, generate_password_hash(password), 1 if is_admin else 0, now()))


def ensure_admin() -> None:
    email, password = os.environ.get("ADMIN_EMAIL", ""), os.environ.get("ADMIN_PASSWORD", "")
    if email and password and user_count() == 0:
        create_user(email, password, is_admin=True)


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
        uid = create_user(email, password, is_admin=True)
        return get_user(uid)
    return None


def get_user(user_id) -> dict | None:
    if not user_id:
        return None
    with connect() as db:
        return db.one("SELECT id, email, name, is_admin FROM users WHERE id = ?", (user_id,))


def set_password(user_id: int, password: str) -> None:
    with connect() as db:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user_id))


def list_users() -> list[dict]:
    with connect() as db:
        return db.all("SELECT id, email, name, is_admin, created_at, last_login_at FROM users ORDER BY email")


def delete_user(user_id: int) -> None:
    with connect() as db:
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
