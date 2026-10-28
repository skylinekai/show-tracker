"""Sales proposal web app (phase 2): login, proposal form, uploads, PowerPoint/PDF download.

Run for development:   python server.py
Run in production:     gunicorn -w 2 --timeout 300 server:app
"""
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_file, send_from_directory, session
from PIL import Image, ImageOps

import ai
import brand
import fontsetup
import generate
import library_store as ls
import mailer
import matcher
import pdfimport
import terms as terms_store

BASE = os.path.dirname(os.path.abspath(__file__))
VERSION = "2026-10-28-a"
REFERENCE_IDS = ["b4153b0816c5", "5d8617d93b50", "a30bffb5e51e", "8f398e1cd968", "a5ba89463d3f", "aff0ef894ba3", "44ec9c67bb41", "61c95a26636b", "6a61a9d338d1",
                 "4024f7f71266", "68391c5cb767", "993caba73d87", "feb881ff91fb", "8a001aa6c79c", "260c88175e54", "70202c30a69a", "869376c57b5a", "55dbcc2e31fc",
                 "28695fa73a69", "b07c3d0c803c", "877dc9811d0e", "faa45e12cbbe", "3e9e5ba0181f", "d931cc8db41a", "ccc35b5eee15", "17bd827d4070", "ad6a46d1889c",
                 "0abb64101842", "9c3ef4e42830", "a0ec2a465652", "1b825bff397e", "7bf65b58ded4", "fc0742766639", "b5f792266596", "52e9da263700"]   # the project pages from the company profile
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", os.path.join(BASE, "data")))
LIB_DIR = os.path.abspath(os.environ.get("LIBRARY_DIR", os.path.join(DATA_DIR, "library")))
DB_PATH = os.path.join(DATA_DIR, "app.db")
PROPOSALS_DIR = os.path.join(DATA_DIR, "proposals")
os.makedirs(PROPOSALS_DIR, exist_ok=True)
# Pages that are switched off for now (set ENABLE_QUOTATION=1 / ENABLE_CRITICAL_DATES=1 in Render to bring them back)
FEATURES = {"critical_dates": os.environ.get("ENABLE_CRITICAL_DATES") == "1",
            "mood_board": os.environ.get("ENABLE_MOOD_BOARD") == "1",
            "materials": os.environ.get("ENABLE_MATERIALS") != "0",
            "multi_show": os.environ.get("ENABLE_MULTI_SHOW") == "1"}  # on unless switched off
Image.MAX_IMAGE_PIXELS = 80_000_000

log = logging.getLogger("proposal-app")
logging.basicConfig(level=logging.INFO)

app = Flask(__name__, static_folder=None)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)   # behind Render's proxy: count login attempts per visitor, not for everyone at once


def _secret():
    path = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    return open(path).read().strip()


app.config.update(
    SECRET_KEY=_secret(),
    MAX_CONTENT_LENGTH=600 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",  # set to 1 when served over HTTPS
    PERMANENT_SESSION_LIFETIME=timedelta(hours=10),
)


class UserError(Exception):
    """A problem the salesperson can fix; the message is shown to them."""


def err(message, status=400):
    return jsonify({"error": message}), status


# ---------------------------------------------------------------- database
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL,
            salt BLOB NOT NULL, pw_hash BLOB NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            name TEXT NOT NULL DEFAULT '', job_title TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', last_login TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS proposals (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL, created TEXT NOT NULL, totals TEXT NOT NULL, summary TEXT NOT NULL);
        """)


init_db()


# ---------------------------------------------------------------- passwords and sessions
def hash_pw(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def create_user(email, password, is_admin=False, name="", job_title="", phone=""):
    salt = secrets.token_bytes(16)
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT INTO users (email, salt, pw_hash, is_admin, name, job_title, phone) VALUES (?,?,?,?,?,?,?)",
                  (email.strip().lower(), salt, hash_pw(password, salt), int(is_admin), name, job_title, phone))


def set_password(email, password):
    salt = secrets.token_bytes(16)
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute("UPDATE users SET salt=?, pw_hash=? WHERE email=?",
                        (salt, hash_pw(password, salt), email.strip().lower()))
        return cur.rowcount == 1


_attempts = {}  # ip -> [count, first_time]
_attempts_lock = threading.Lock()


def throttled(ip):
    with _attempts_lock:
        rec = _attempts.get(ip)
        if rec and time.time() - rec[1] > 300:
            _attempts.pop(ip, None)
            rec = None
        return bool(rec and rec[0] >= 10)


def note_failure(ip):
    with _attempts_lock:
        rec = _attempts.setdefault(ip, [0, time.time()])
        rec[0] += 1


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        user = current_user()
        if user is None:
            return err("Please sign in.", 401)
        g.user = user
        return fn(*a, **kw)
    return wrapper


@app.before_request
def csrf_guard():
    # Cookies are SameSite=Strict; requiring a custom header adds a second layer.
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("X-Requested-With") != "proposal-app":
        return err("Bad request.", 403)


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def me(row):
    return {"email": row["email"], "is_admin": bool(row["is_admin"]), "name": _col(row, "name"), "job_title": _col(row, "job_title"), "phone": _col(row, "phone"), "features": FEATURES}


def _col(row, key):
    """A user detail that older databases may not have yet: never let a missing one stop someone signing in."""
    try:
        return row[key] or ""
    except (IndexError, KeyError):
        return ""


@app.post("/api/login")
def login():
    ip = request.remote_addr or "?"
    if throttled(ip):
        return err("Too many attempts. Please wait a few minutes and try again.", 429)
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    row = db().execute("SELECT * FROM users WHERE email=? AND active=1", (email,)).fetchone()
    salt = row["salt"] if row else b"\0" * 16  # do the same work either way
    good = hmac.compare_digest(hash_pw(password, salt), row["pw_hash"] if row else b"\0" * 32) and row is not None
    if not good:
        note_failure(ip)
        return err("Incorrect email or password.", 401)
    session.clear()
    session["uid"] = row["id"]
    session.permanent = True
    try:
        db().execute("UPDATE users SET last_login=? WHERE id=?", (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), row["id"]))
        db().commit()
    except sqlite3.Error:
        log.exception("could not record the sign-in time")
    return jsonify(me(row))


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def whoami():
    row = current_user()
    return jsonify(me(row)) if row else err("Not signed in.", 401)


def migrate_and_bootstrap():
    """Upgrade older databases, seed the library on first start, create the first admin from env vars."""
    for col, ddl in (("active", "INTEGER NOT NULL DEFAULT 1"), ("name", "TEXT NOT NULL DEFAULT ''"),
                     ("job_title", "TEXT NOT NULL DEFAULT ''"), ("phone", "TEXT NOT NULL DEFAULT ''"),
                     ("last_login", "TEXT NOT NULL DEFAULT ''")):
        try:
            with sqlite3.connect(DB_PATH, timeout=30) as c:
                if col not in [r[1] for r in c.execute("PRAGMA table_info(users)")]:
                    c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
                    log.info("users table: added column %s", col)
        except sqlite3.Error:
            log.exception("could not add the users column %s (carrying on)", col)
    seed = os.path.join(BASE, "library_seed.zip")
    if os.path.exists(seed) and not os.path.exists(os.path.join(LIB_DIR, "library.json")):
        import zipfile
        os.makedirs(LIB_DIR, exist_ok=True)
        zipfile.ZipFile(seed).extractall(LIB_DIR)
        log.info("library seeded")
    if os.path.exists(seed):  # a new stock set shipped with this version replaces the old one, once
        import zipfile
        try:
            ver = zipfile.ZipFile(seed).read("seed_version.txt").decode().strip()
        except KeyError:
            ver = ""
        marker = os.path.join(LIB_DIR, ".seed_version")
        have = open(marker).read().strip() if os.path.exists(marker) else ""
        if ver and have != ver:
            n = ls.replace_categories(LIB_DIR, seed)
            open(marker, "w").write(ver)
            log.info("library stock set replaced (%s items, version %s)", n, ver)
    import zipfile
    for sname, vname, mname, what in (("about_seed.zip", "about_version.txt", ".about_version", "about-the-company pages"),
                                      ("extra_seed.zip", "extra_version.txt", ".extra_version", "extra library pictures")):
        aseed = os.path.join(BASE, sname)   # sets of pictures added to the library once per version (nothing already there is changed)
        if not os.path.exists(aseed):
            continue
        try:
            aver = zipfile.ZipFile(aseed).read(vname).decode().strip()
        except KeyError:
            aver = ""
        amarker = os.path.join(LIB_DIR, mname)
        ahave = open(amarker).read().strip() if os.path.exists(amarker) else ""
        if aver and ahave != aver:
            n = ls.add_missing(LIB_DIR, aseed, cats=("about",) if sname == "about_seed.zip" else None)
            open(amarker, "w").write(aver)
            log.info("%s added (%s, version %s)", what, n, aver)
    rmark = os.path.join(LIB_DIR, ".references_version")   # once: the examples-of-work pages move from Company to References
    if not os.path.exists(rmark):
        try:
            n = ls.move_category(LIB_DIR, REFERENCE_IDS, "references")
            open(rmark, "w").write("1")
            log.info("%s example-of-work pages moved to References", n)
        except Exception:
            log.exception("moving pages to References failed")
    ls.remove_demo_items(LIB_DIR)
    ls.load(LIB_DIR)  # gives older entries ids and previews
    email, pw = os.environ.get("ADMIN_EMAIL", "").strip().lower(), os.environ.get("ADMIN_PASSWORD", "")
    if email and pw:
        with sqlite3.connect(DB_PATH) as c:
            exists = c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
        if exists and os.environ.get("RESET_ADMIN_PASSWORD", "").strip().lower() in ("1", "yes", "true"):
            if len(pw) >= 5:   # recovery: set this on Render, redeploy, sign in, then delete it
                set_password(email, pw)
                with sqlite3.connect(DB_PATH) as c:
                    c.execute("UPDATE users SET active=1, is_admin=1 WHERE email=?", (email,))
                log.info("password reset and admin access restored for %s", email)
        if not exists:
            if len(pw) < 5:
                log.error("ADMIN_PASSWORD must be at least 5 characters, so no admin was created")
            else:
                create_user(email, pw, True)
                log.info("admin account created for %s", email)


migrate_and_bootstrap()


@app.get("/healthz")
def healthz():
    return "ok"


# ---------------------------------------------------------------- user management (admin only)
def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a, **kw):
        if not g.user["is_admin"]:
            return err("Admins only.", 403)
        return fn(*a, **kw)
    return wrapper


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.get("/api/users")
@admin_required
def list_users():
    rows = db().execute("SELECT * FROM users ORDER BY email").fetchall()
    return jsonify([{"id": r["id"], "email": r["email"], "is_admin": bool(r["is_admin"]), "active": bool(r["active"]),
                     "name": _col(r, "name"), "job_title": _col(r, "job_title"), "phone": _col(r, "phone"), "last_login": _col(r, "last_login")} for r in rows])


def clean_profile(body):
    return [str(body.get(k) or "").strip()[:120] for k in ("name", "job_title", "phone")]


def app_url():
    """The web address to put in emails: APP_URL if set, otherwise the address the admin is using."""
    fixed = os.environ.get("APP_URL", "").strip().rstrip("/")
    if fixed:
        return fixed
    scheme = (request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0].strip()
    host = (request.headers.get("X-Forwarded-Host") or request.host).split(",")[0].strip()
    return f"{scheme}://{host}"


def email_login(name, email, password, reset=False):
    """Email someone their sign-in details. Returns (sent, problem)."""
    try:
        subject, text, page = mailer.login_email(name, email, password, app_url(), reset)
        mailer.send(email, subject, text, page)
        return True, ""
    except mailer.MailError as e:
        return False, str(e)
    except Exception:
        log.exception("login email failed")
        return False, "Something went wrong sending the email."


@app.post("/api/users")
@admin_required
def add_user():
    body = request.get_json(silent=True) or {}
    email, pw = str(body.get("email") or "").strip().lower(), str(body.get("password") or "")
    if not EMAIL.match(email) or len(email) > 120:
        return err("Please enter a valid email address.")
    if len(pw) < 5:
        return err("The password needs at least 5 characters.")
    name, job, phone = clean_profile(body)
    if not (name and job and phone):
        return err("Please fill in the name, job title and contact number.")
    try:
        create_user(email, pw, bool(body.get("is_admin")), name, job, phone)
    except sqlite3.IntegrityError:
        return err("That email already has an account.")
    out = {"ok": True, "emailed": False}
    if body.get("send_email") and mailer.configured():
        out["emailed"], out["email_problem"] = email_login(name, email, pw)
    return jsonify(out)


@app.patch("/api/me")
@login_required
def update_me():
    name, job, phone = clean_profile(request.get_json(silent=True) or {})
    if not (name and job and phone):
        return err("Please fill in your name, job title and contact number.")
    con = db()
    con.execute("UPDATE users SET name=?, job_title=?, phone=? WHERE id=?", (name, job, phone, g.user["id"]))
    con.commit()
    return jsonify(me(con.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()))


@app.post("/api/me/password")
@login_required
def change_my_password():
    ip = request.remote_addr or "?"
    if throttled(ip):
        return err("Too many attempts. Please wait a few minutes and try again.", 429)
    body = request.get_json(silent=True) or {}
    cur, new = str(body.get("current") or ""), str(body.get("password") or "")
    row = g.user
    if not hmac.compare_digest(hash_pw(cur, row["salt"]), row["pw_hash"]):
        note_failure(ip)
        return err("Your current password isn't right.", 403)
    if len(new) < 5:
        return err("The new password needs at least 5 characters.")
    set_password(row["email"], new)
    return jsonify({"ok": True})


@app.post("/api/users/<int:uid>/password")
@admin_required
def reset_password(uid):
    pw = str((request.get_json(silent=True) or {}).get("password") or "")
    if len(pw) < 5:
        return err("The password needs at least 5 characters.")
    row = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return err("Not found.", 404)
    set_password(row["email"], pw)
    out = {"ok": True, "emailed": False}
    if (request.get_json(silent=True) or {}).get("send_email") and mailer.configured():
        out["emailed"], out["email_problem"] = email_login(_col(row, "name"), row["email"], pw, reset=True)
    return jsonify(out)


@app.patch("/api/users/<int:uid>")
@admin_required
def edit_user(uid):
    name, job, phone = clean_profile(request.get_json(silent=True) or {})
    if not (name and job and phone):
        return err("Please fill in the name, job title and contact number.")
    con = db()
    if not con.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        return err("Not found.", 404)
    con.execute("UPDATE users SET name=?, job_title=?, phone=? WHERE id=?", (name, job, phone, uid))
    con.commit()
    return jsonify({"ok": True})


@app.delete("/api/users/<int:uid>")
@admin_required
def remove_user(uid):
    if uid == g.user["id"]:
        return err("You can't remove your own account.")
    con = db()
    if not con.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        return err("Not found.", 404)
    # their proposals aren't lost: they pass to the admin doing the removing
    con.execute("UPDATE proposals SET user_id=? WHERE user_id=?", (g.user["id"], uid))
    con.execute("DELETE FROM users WHERE id=?", (uid,))
    con.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- the Terms pages: everyone can preview them, admins change the wording
TERMS_PREVIEW_DIR = os.path.join(DATA_DIR, "terms_preview")


def terms_preview(pages):
    """Pictures of the Terms pages for this wording (made once, then reused). Returns the picture names."""
    key = terms_store.digest(pages)
    d = os.path.join(TERMS_PREVIEW_DIR, key)
    have = sorted(f for f in os.listdir(d) if f.endswith(".png")) if os.path.isdir(d) else []
    if have:
        return key, have
    with _pdf_lock:
        tmp = tempfile.mkdtemp(prefix="terms_")
        try:
            pptx = os.path.join(tmp, "terms.pptx")
            generate.terms_deck(pages, pptx)
            cmd = shlex.split(os.environ.get("SOFFICE_CMD", "soffice")) + ["--headless", "--convert-to", "pdf", "--outdir", tmp, pptx]
            subprocess.run(cmd, check=True, timeout=240, capture_output=True)
            subprocess.run(["pdftoppm", "-png", "-r", "110", os.path.join(tmp, "terms.pdf"), os.path.join(tmp, "p")], check=True, timeout=120)
            os.makedirs(d, exist_ok=True)
            names = sorted(f for f in os.listdir(tmp) if f.startswith("p") and f.endswith(".png"))
            for n in names:
                shutil.move(os.path.join(tmp, n), os.path.join(d, n))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return key, sorted(f for f in os.listdir(d) if f.endswith(".png"))


def terms_urls(pages):
    try:
        key, names = terms_preview(pages)
    except Exception:
        log.exception("terms preview failed")
        return None
    return [f"/api/terms/preview/{key}/{n}" for n in names]


@app.get("/api/terms")
@login_required
def get_terms():
    t = terms_store.load(DATA_DIR)
    return jsonify({"pages": t["pages"], "updated": t["updated"], "custom": t["custom"], "default": terms_store.DEFAULT_PAGES})


@app.put("/api/terms")
@admin_required
def put_terms():
    pages = terms_store.clean_pages((request.get_json(silent=True) or {}).get("pages"))
    if not pages:
        return err("Please add some wording for at least one page.")
    terms_store.save(DATA_DIR, pages)
    return jsonify({"ok": True, "urls": terms_urls(pages)})


@app.get("/api/terms/preview")
@login_required
def terms_preview_saved():
    urls = terms_urls(terms_store.load(DATA_DIR)["pages"])
    return (jsonify({"urls": urls}) if urls else err("The Terms preview couldn't be made just now. Please try again."))


@app.post("/api/terms/preview")
@admin_required
def terms_preview_draft():
    pages = terms_store.clean_pages((request.get_json(silent=True) or {}).get("pages"))
    if not pages:
        return err("Please add some wording to preview.")
    urls = terms_urls(pages)
    return (jsonify({"urls": urls}) if urls else err("The preview couldn't be made just now. Please try again."))


@app.get("/api/terms/preview/<key>/<name>")
@login_required
def terms_preview_file(key, name):
    if not re.fullmatch(r"[0-9a-f]{12}", key) or not re.fullmatch(r"p-\d+\.png", name):
        return err("Not found.", 404)
    p = os.path.join(TERMS_PREVIEW_DIR, key, name)
    return send_file(p, mimetype="image/png", max_age=3600) if os.path.exists(p) else err("Not found.", 404)


def build_deck(pdir, data, sel):
    """Write the proposal input and the material choices to disk, then build the PowerPoint."""
    os.makedirs(pdir, exist_ok=True)
    json.dump(data, open(os.path.join(pdir, "data.json"), "w"))
    json.dump(sel, open(os.path.join(pdir, "selection.json"), "w"))
    full = dict(data)
    picks = matcher.apply_selection(LIB_DIR, sel) if (FEATURES["mood_board"] or FEATURES["materials"]) else {}
    if not FEATURES["mood_board"]:
        picks.pop("mood_board", None)
    if not FEATURES["materials"]:
        picks.pop("materials", None)
    full.update(picks)
    about = [it for it in (ls.get(LIB_DIR, i) for i in list(data.get("about_pages", [])) + list(data.get("reference_pages", []))) if it]   # Company pages, then References
    full["about_files"] = [ls.path_of(LIB_DIR, it) for it in about]
    full["about_names"] = [it["name"] for it in about]
    full["terms"] = terms_store.load(DATA_DIR)["pages"]   # the current wording of the Terms pages
    totals = generate.generate(full, os.path.join(pdir, "proposal.pptx"))
    stale = os.path.join(pdir, "proposal.pdf")
    if os.path.exists(stale):
        os.remove(stale)
    return {k: round(v, 2) for k, v in totals.items()}


# ---------------------------------------------------------------- read a client's website for logo, colours, fonts
_brand_hits = {}  # user id -> recent lookup times (keeps one account from hammering other people's websites)


@app.post("/api/brand")
@login_required
def brand_lookup():
    now = time.time()
    hits = [t for t in _brand_hits.get(g.user["id"], []) if now - t < 3600]
    if len(hits) >= 40:
        return err("That's a lot of website lookups this hour. Please try again a little later.", 429)
    _brand_hits[g.user["id"]] = hits + [now]
    url = (request.get_json(silent=True) or {}).get("url", "")
    try:
        return jsonify(brand.lookup(url))
    except brand.LookupError_ as e:
        return err(str(e), 422)
    except Exception:
        log.exception("brand lookup failed")
        return err("That website couldn't be read.", 422)


# ---------------------------------------------------------------- library
ID_RE = re.compile(r"[0-9a-f]{12}")


@app.get("/api/library")
@login_required
def library_list():
    return jsonify({"categories": [{"key": k, "label": l} for k, l in ls.CATEGORIES], "items": [ls.summary(i) for i in ls.load(LIB_DIR)]})


@app.get("/api/library/<item_id>/thumb")
@login_required
def library_thumb(item_id):
    p = ID_RE.fullmatch(item_id) and ls.thumb_path(LIB_DIR, item_id)
    if not p:
        return err("Not found.", 404)
    resp = send_file(p, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


def describe_into(item):
    """Ask Claude to name and tag a library image. Returns a note if it couldn't."""
    if item["category"] in ("about", "references"):
        return ""
    if not ai.configured():
        return "AI tagging is off, so this image was added with its file name and no tags."
    try:
        d = ai.describe_image(ls.path_of(LIB_DIR, item), item["category"])
        ls.update(LIB_DIR, item["id"], name=d["name"] or None, tags=d["tags"], colours=d["colours"], description=d["description"])
        if item["category"] in ls.CUTOUT_CATS:
            cur = ls.get(LIB_DIR, item["id"]) or item
            if cur.get("height_src") != "manual":
                h = d.get("height_cm") or ls.guess_height(cur.get("name"), ls._aspect(LIB_DIR, cur), cur["category"])
                ls.update(LIB_DIR, item["id"], height_cm=h, height_src="ai" if d.get("height_cm") else "guess")
        return ""
    except ai.AIError as e:
        return f"Added, but automatic tagging failed: {e}"
    except Exception:
        log.exception("tagging failed")
        return "Added, but automatic tagging failed."


@app.post("/api/library")
@login_required  # salespeople can add a missing piece from the picker; editing and deleting stay admin-only
def library_upload():
    category = request.form.get("category", "")
    fs = request.files.get("file")
    if category not in ls.CAT_KEYS:
        return err("Choose a category first.")
    if not fs or not fs.filename:
        return err("No file received.")
    try:
        im = Image.open(fs.stream)
        im.load()
    except Exception:
        return err(f"'{fs.filename}' couldn't be read as an image (use PNG or JPG).")
    item = ls.add(LIB_DIR, im, category, fs.filename)
    if category == "previous_work":
        ls.ensure_signatures(LIB_DIR)
    note = describe_into(item)
    if category in ls.CUTOUT_CATS and item.get("cutout_failed"):
        note = (note + " " if note else "") + "The background couldn't be removed automatically. Upload a PNG with a transparent background for the best result."
    return jsonify({"item": ls.summary(ls.get(LIB_DIR, item["id"])), "note": note})


@app.get("/api/library/<item_id>/image")
@login_required
def library_image(item_id):
    """The full picture (with its background removed when it has been), for previews."""
    it = ID_RE.fullmatch(item_id) and ls.get(LIB_DIR, item_id)
    if not it:
        return err("Not found.", 404)
    p = ls.path_of(LIB_DIR, it, request.args.get("cut") == "1")
    resp = send_file(p)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.post("/api/library/<item_id>/background")
@login_required
def library_background(item_id):
    """Remove (or restore) the plain background of a picture used as an extra piece on the Materials & Furnishings page."""
    it = ID_RE.fullmatch(item_id) and ls.get(LIB_DIR, item_id)
    if not it:
        return err("Not found.", 404)
    remove = bool((request.get_json(silent=True) or {}).get("remove", True))
    if not remove and it["category"] in ls.CUTOUT_CATS:
        return err("Furniture and lighting always have their background removed.")
    ok = ls.set_background(LIB_DIR, item_id, remove)
    if not ok:
        return err("The background couldn't be removed automatically: it isn't a plain, even colour. Upload a PNG with a transparent background instead.")
    return jsonify({"ok": True})


@app.post("/api/materials/suggest")
@login_required
def materials_suggest():
    """Look at the design renders on the form and pick matching flooring, materials, furniture and lighting from the library."""
    if not ai.configured():
        return err("Suggestions from the renders aren't switched on yet. An admin needs to add the Anthropic API key in Render (Environment > ANTHROPIC_API_KEY).", 503)
    files = [f for f in request.files.getlist("renders") if f and f.filename][:4]
    if not files:
        return err("Add the design renders first, then ask for suggestions.")
    tmp = tempfile.mkdtemp(prefix="suggest_")
    try:
        paths = []
        for n, f in enumerate(files):
            raw = os.path.join(tmp, f"r{n}.bin")
            f.save(raw)
            try:
                if f.filename.lower().endswith(".pdf"):
                    subprocess.run(["pdftoppm", "-jpeg", "-r", "110", "-f", "1", "-l", "1", raw, os.path.join(tmp, f"r{n}")], check=True, timeout=120)
                    got = sorted(x for x in os.listdir(tmp) if x.startswith(f"r{n}-") and x.endswith(".jpg"))
                    if got:
                        paths.append(os.path.join(tmp, got[0]))
                else:
                    Image.open(raw).verify()
                    paths.append(raw)
            except Exception:
                continue
        if not paths:
            return err("Those files couldn't be read as pictures. Use PNG, JPG or PDF renders.")
        cats = {c: [] for c in ("flooring", "finishings", "furniture", "lighting", "lifestyle")}
        for it in ls.load(LIB_DIR):
            if it["category"] in cats and not it.get("only_additional"):
                cats[it["category"]].append(it)
        catalogue = {c: [{"id": i["id"], "name": i["name"], "tags": i.get("tags", [])} for i in rows[:150]] for c, rows in cats.items()}
        try:
            raw = ai.match_materials(paths, catalogue, text(request.form.get("client"), "Client", maxlen=120), [])
        except ai.AIError as e:
            return err(str(e), 502)
        except Exception:
            log.exception("materials suggestion failed")
            return err("The suggestion couldn't be made just now. Please try again.", 502)
        by_id = {i["id"]: i for rows in cats.values() for i in rows}
        pick = lambda key, limit: [ls.summary(by_id[x]) for x in matcher._clean(raw.get(key) if isinstance(raw.get(key), list) else [raw.get(key)], cats[key], limit)]
        return jsonify({"flooring": pick("flooring", 1), "finishings": pick("finishings", 4), "furniture": pick("furniture", 4),
                        "lighting": pick("lighting", 3), "lifestyle": pick("lifestyle", 1), "note": str(raw.get("note") or "")[:240]})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/materials/layout")
@login_required
def materials_layout():
    """The Materials & Furnishings page for a set of choices, as boxes (fractions of the page) for the live preview."""
    sel = matcher.clean_selection(LIB_DIR, request.get_json(silent=True) or {})
    m = matcher.apply_selection(LIB_DIR, sel).get("materials", {})
    by_id = {i["id"]: i for i in ls.load(LIB_DIR)}
    W, H, k = generate.SLIDE_W, generate.SLIDE_H, generate.MAT_K

    def url(i, cut=False):
        if not i:
            return None
        if not cut:
            return f"/api/library/{i}/image"
        try:
            v = int(os.path.getmtime(ls.path_of(LIB_DIR, by_id[i], True)))
        except (OSError, KeyError):
            v = 0
        return f"/api/library/{i}/image?cut=1&v={v}"

    def fr(x, y, w, h):
        return {"x": x / W, "y": y / H, "w": w / W, "h": h / H}
    furn, lights = generate.materials_boxes(m)
    lay = sel.get("layout") or {}
    extras = set(sel.get("additional") or [])
    items = lambda rows: [dict(fr(x, y, w, h), id=i, name=by_id[i]["name"], url=url(i, True), z=z, crop_ok=i in extras, has_cut=bool(by_id[i].get("cutout")) and not by_id[i].get("cutout_failed"), flip=1 if len(lay.get(i) or []) > 5 and lay[i][5] else 0, crop=list(lay[i][6:10]) if len(lay.get(i) or []) >= 10 else [0, 0, 0, 0]) for _p, i, x, y, w, h, z in rows]
    wall = None
    if sel.get("lifestyle"):
        x, y, w, h = generate._px(generate.MAT_WALL)
        wall = dict(fr(x, y, W - x, h), url=url(sel["lifestyle"]), pos=(sel.get("layout") or {}).get("wall") or [0.5, 0.5])
    floor = dict(fr(*generate._px(generate.MAT_FLOOR)), url=url(sel["flooring"])) if sel.get("flooring") else None
    sw = [dict(fr(int(x * k), int(generate.MAT_SWATCH_Y * k), int(generate.MAT_SWATCH_W * k), int(generate.MAT_SWATCH_H * k)), url=url(i))
          for i, x in zip(sel.get("finishings", [])[:4], generate.MAT_SWATCH_X)]
    dots = [dict(fr(x, y, generate.MAT_DOT, generate.MAT_DOT), c=c) for (x, y), c in zip(generate.MAT_DOTS, sel.get("colours") or [])]
    slots = []   # empty places, shown as outlines so you can see where things will go
    if not wall:
        x, y, w, h = generate._px(generate.MAT_WALL)
        slots.append(dict(fr(x, y, W - x, h), label="Feature wall"))
    if not floor:
        slots.append(dict(fr(*generate._px(generate.MAT_FLOOR)), label="Flooring"))
    for n in range(len(sw), 4):
        slots.append(dict(fr(int(generate.MAT_SWATCH_X[n] * k), int(generate.MAT_SWATCH_Y * k), int(generate.MAT_SWATCH_W * k), int(generate.MAT_SWATCH_H * k)), label="Material"))
    for (x, y) in generate.MAT_DOTS[len(dots):]:
        slots.append(dict(fr(x, y, generate.MAT_DOT, generate.MAT_DOT), label="", round=True))
    if not furn:
        fx0, fx1 = generate.FURN_REGION
        slots.append(dict(fr(int(fx0), int(generate.FURN_BASELINE - 2.2e6), int(fx1 - fx0), int(2.2e6)), label="Furniture"))
    if not lights:
        lx0, lx1 = generate.LIGHT_REGION
        slots.append(dict(fr(int(lx0), 0, int(lx1 - lx0), int(1.3e6)), label="Lighting"))
    return jsonify({"aspect": W / H, "wall": wall, "flooring": floor, "swatches": sw, "colours": dots, "furniture": items(furn), "lighting": items(lights), "slots": slots})


@app.patch("/api/library/<item_id>")
@admin_required
def library_edit(item_id):
    b = request.get_json(silent=True) or {}
    if not ID_RE.fullmatch(item_id):
        return err("Not found.", 404)
    tags = b.get("tags")
    if isinstance(tags, str):
        tags = tags.split(",")
    height = b.get("height_cm")
    try:
        height = float(height) if height not in (None, "") else None
    except (TypeError, ValueError):
        return err("Height should be a number of centimetres.")
    it = ls.update(LIB_DIR, item_id, name=b.get("name"), category=b.get("category"), tags=tags, height_cm=height)
    return jsonify(ls.summary(ls.get(LIB_DIR, item_id))) if it else err("Not found.", 404)


@app.post("/api/library/order")
@admin_required
def library_order():
    b = request.get_json(silent=True) or {}
    cat, ids = str(b.get("category", "")), b.get("ids")
    if cat not in ls.CAT_KEYS or not isinstance(ids, list) or len(ids) > 2000:
        return err("Bad request.")
    ls.reorder(LIB_DIR, cat, [i for i in ids if isinstance(i, str) and ID_RE.fullmatch(i)])
    return jsonify({"ok": True})


@app.post("/api/split-pdf")
@login_required
def split_pdf():
    """Break an uploaded PDF into one picture per page, so each page can be reordered or removed on the form."""
    import base64
    fs = request.files.get("file")
    if not fs or not fs.filename or not fs.filename.lower().endswith(".pdf"):
        return err("Please choose a PDF file.")
    if fs.stream.read(5) != b"%PDF-":
        return err("That isn't a valid PDF.")
    fs.stream.seek(0)
    tmp = tempfile.mkdtemp(prefix="split_")
    try:
        src = os.path.join(tmp, "in.pdf")
        fs.save(src)
        stem = safe_name(os.path.splitext(fs.filename)[0]) or "pdf"
        last = "1" if request.form.get("first") else "60"
        subprocess.run(["pdftoppm", "-jpeg", "-r", "150", "-l", last, src, os.path.join(tmp, "p")], check=True, timeout=240)
        files = sorted(f for f in os.listdir(tmp) if f.startswith("p-") and f.endswith(".jpg"))
        if not files:
            return err("No pages could be read from that PDF.")
        total = 0
        try:
            out = subprocess.run(["pdfinfo", src], capture_output=True, text=True, timeout=30).stdout
            total = int(re.search(r"Pages:\s+(\d+)", out).group(1))
        except Exception:
            pass
        pages = [{"name": f"{stem}-{n:02d}.jpg", "data": base64.b64encode(open(os.path.join(tmp, f), "rb").read()).decode()} for n, f in enumerate(files, 1)]
        return jsonify({"pages": pages, "total": total, "note": f"Only the first 60 of {total} pages were used." if total > 60 and last == "60" else ""})
    except subprocess.TimeoutExpired:
        return err("That PDF took too long to read.", 422)
    except Exception:
        log.exception("pdf split failed")
        return err("That PDF couldn't be read.", 422)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.delete("/api/library/<item_id>")
@admin_required
def library_delete(item_id):
    return jsonify({"ok": True}) if ID_RE.fullmatch(item_id) and ls.delete(LIB_DIR, item_id) else err("Not found.", 404)


@app.post("/api/library/<item_id>/retag")
@admin_required
def library_retag(item_id):
    it = ID_RE.fullmatch(item_id) and ls.get(LIB_DIR, item_id)
    if not it:
        return err("Not found.", 404)
    note = describe_into(it)
    return jsonify({"item": ls.summary(ls.get(LIB_DIR, item_id)), "note": note})


# ---------------------------------------------------------------- add pictures from a PDF (review first, then import)
STAGING = os.path.join(DATA_DIR, "staging")
TOKEN_RE = re.compile(r"[0-9a-f]{16}")
CID_RE = re.compile(r"\d{3}-(?:\d{3}|page)")


def clean_staging():
    """Forget review batches that were never finished (older than a day)."""
    if not os.path.isdir(STAGING):
        return
    for d in os.listdir(STAGING):
        p = os.path.join(STAGING, d)
        if os.path.isdir(p) and time.time() - os.path.getmtime(p) > 86400:
            shutil.rmtree(p, ignore_errors=True)


@app.post("/api/library/pdf")
@admin_required
def library_pdf_analyse():
    fs = request.files.get("file")
    mode = "pages" if request.form.get("mode") == "pages" else "images"
    if not fs or not fs.filename or not fs.filename.lower().endswith(".pdf"):
        return err("Please choose a PDF file.")
    if fs.stream.read(5) != b"%PDF-":
        return err(f"'{fs.filename}' isn't a valid PDF.")
    fs.stream.seek(0)
    clean_staging()
    token = uuid.uuid4().hex[:16]
    d = os.path.join(STAGING, token)
    os.makedirs(d, exist_ok=True)
    pdf_path = os.path.join(d, "source.pdf")
    fs.save(pdf_path)
    try:
        res = pdfimport.analyse(pdf_path, os.path.join(d, "out"), mode)
    except pdfimport.PdfError as e:
        shutil.rmtree(d, ignore_errors=True)
        return err(str(e))
    except Exception:
        log.exception("pdf analysis failed")
        shutil.rmtree(d, ignore_errors=True)
        return err("This PDF couldn't be read.", 500)
    finally:
        try:
            os.remove(pdf_path)  # the pictures are extracted, the original isn't needed
        except OSError:
            pass
    json.dump({"name": fs.filename}, open(os.path.join(d, "meta.json"), "w"))
    return jsonify({"token": token, "mode": res["mode"], "pages": res["pages"], "note": res["note"], "name": fs.filename,
                    "items": [{"id": i["id"], "page": i["page"], "width": i["w"], "height": i["h"],
                               "thumb": f"/api/library/pdf/{token}/{i['id']}/thumb"} for i in res["items"]]})


@app.get("/api/library/pdf/<token>/<cid>/thumb")
@admin_required
def library_pdf_thumb(token, cid):
    if not TOKEN_RE.fullmatch(token) or not CID_RE.fullmatch(cid):
        return err("Not found.", 404)
    p = os.path.join(STAGING, token, "out", "thumbs", cid + ".jpg")
    if not os.path.exists(p):
        return err("Not found.", 404)
    resp = send_file(p, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


@app.post("/api/library/pdf/import")
@admin_required
def library_pdf_import():
    b = request.get_json(silent=True) or {}
    token, category = str(b.get("token", "")), str(b.get("category", ""))
    if not TOKEN_RE.fullmatch(token) or category not in ls.CAT_KEYS:
        return err("Bad request.")
    d = os.path.join(STAGING, token)
    try:
        meta = json.load(open(os.path.join(d, "meta.json")))
        manifest = json.load(open(os.path.join(d, "out", "manifest.json")))
    except OSError:
        return err("This review has expired. Please add the PDF again.", 410)
    known = {i["id"]: i for i in manifest["items"]}
    stem = os.path.splitext(meta["name"])[0]
    added = []
    for cid in [c for c in (b.get("ids") or []) if isinstance(c, str) and CID_RE.fullmatch(c) and c in known]:
        try:
            im = Image.open(os.path.join(d, "out", cid + ".jpg"))
            im.load()
        except Exception:
            continue
        page = known[cid]["page"]
        item = ls.add(LIB_DIR, im, category, f"{stem} page {page}.jpg")
        ls.update(LIB_DIR, item["id"], origin=f"{meta['name']}, page {page}")
        added.append(ls.summary(ls.get(LIB_DIR, item["id"])))
    if category == "previous_work":
        ls.ensure_signatures(LIB_DIR)
    shutil.rmtree(d, ignore_errors=True)
    return jsonify({"items": added})


@app.delete("/api/library/pdf/<token>")
@admin_required
def library_pdf_cancel(token):
    if TOKEN_RE.fullmatch(token):
        shutil.rmtree(os.path.join(STAGING, token), ignore_errors=True)
    return jsonify({"ok": True})


@app.get("/api/status")
@admin_required
def system_status():
    counts = {k: 0 for k in ls.CAT_KEYS}
    for i in ls.load(LIB_DIR):
        counts[i["category"]] += 1
    try:
        fonts = [fontsetup.check(n) for n in fontsetup.required_fonts()]
    except Exception:
        fonts = []
    try:
        titles = fontsetup.check_titles()
    except Exception:
        titles = {}
    return jsonify({"ai": {"configured": ai.configured()}, "fonts": fonts, "titles": titles, "mail": {"configured": mailer.configured(), "via": mailer.via()}, "library": counts, "small_library": matcher.SMALL_LIBRARY})


@app.post("/api/status/ai-test")
@admin_required
def ai_test():
    try:
        ai.test_connection()
        return jsonify({"ok": True})
    except ai.AIError as e:
        return err(str(e), 400)


# ---------------------------------------------------------------- change a proposal's mood board / materials
class OldProposal(Exception):
    pass


def _proposal_files(pid):
    pdir = os.path.join(PROPOSALS_DIR, pid)
    try:
        return pdir, json.load(open(os.path.join(pdir, "data.json"))), json.load(open(os.path.join(pdir, "selection.json")))
    except OSError:
        raise OldProposal()


@app.errorhandler(OldProposal)
def old_proposal(_e):
    return err("This proposal was made before editing was possible, so it can\u2019t be opened for changes. Please create it again.", 409)


@app.get("/api/proposals/<pid>/selection")
@login_required
def get_selection(pid):
    if owned(pid) is None:
        return err("Not found.", 404)
    _, _, sel = _proposal_files(pid)
    return jsonify(matcher.details(LIB_DIR, sel))


@app.post("/api/proposals/<pid>/mood-board/suggest")
@login_required
def suggest_mood_board(pid):
    """Find a fresh set of photos (not the ones on screen). Nothing is saved until the salesperson rebuilds."""
    if owned(pid) is None:
        return err("Not found.", 404)
    _, data, _ = _proposal_files(pid)
    exclude = [x for x in ((request.get_json(silent=True) or {}).get("exclude") or []) if isinstance(x, str) and ID_RE.fullmatch(x)]
    renders = [p for s in data["shows"] for p in s["renders"]]
    ids, reasons, method, summary, note = matcher.pick_mood_board(LIB_DIR, renders, data["client"]["name"], exclude)
    if not ids:
        return err("There are no other photos in the Previous work library to suggest. Add more in the Library tab.")
    by_id = {i["id"]: ls.summary(i) for i in ls.load(LIB_DIR)}
    return jsonify({"photos": [dict(by_id[i], reason=reasons.get(i, "")) for i in ids if i in by_id], "method": method, "note": note})


@app.post("/api/proposals/<pid>/selection")
@login_required
def set_selection(pid):
    if owned(pid) is None:
        return err("Not found.", 404)
    pdir, data, old = _proposal_files(pid)
    sel = matcher.clean_selection(LIB_DIR, request.get_json(silent=True) or {})
    sel.update(method="manual", design_summary=old.get("design_summary", ""),
               mood_reasons={k: v for k, v in (request.get_json(silent=True) or {}).get("mood_reasons", {}).items() if isinstance(v, str)})
    try:
        totals = build_deck(pdir, data, sel)
    except Exception:
        log.exception("rebuild failed")
        return err("The proposal couldn't be rebuilt. Please try again.", 500)
    db().execute("UPDATE proposals SET totals=? WHERE id=?", (json.dumps(totals), pid))
    db().commit()
    warn = "" if len(sel["previous_work"]) >= 3 else "The mood board page was left out because it needs at least 3 photos."
    return jsonify({"selection": matcher.details(LIB_DIR, sel), "note": warn})


# ---------------------------------------------------------------- brand fonts for the web app
_FONT_EXT = (".otf", ".ttf", ".woff", ".woff2")


def web_fonts():
    """Europa font files found in the repository (main folder or a fonts/ folder), as [(path, weight, css format)].
    Light is used for body text and Regular for titles and buttons; bold and italic files are not used."""
    found = []
    for folder in (BASE, os.path.join(BASE, "fonts")):
        if os.path.isdir(folder):
            found += [os.path.join(folder, n) for n in sorted(os.listdir(folder)) if n.lower().endswith(_FONT_EXT)]
    europa = [p for p in found if "europa" in os.path.basename(p).lower()]
    out = []
    for p in europa or found:
        n = os.path.basename(p).lower()
        if any(w in n for w in ("bold", "black", "heavy", "italic", "oblique", "semibold")):
            continue
        weight = 300 if "light" in n else 400
        fmt = {"otf": "opentype", "ttf": "truetype", "woff": "woff", "woff2": "woff2"}[n.rsplit(".", 1)[1]]
        out.append((p, weight, fmt))
    return out


@app.get("/skyline-logo.png")
def skyline_logo():
    resp = send_file(os.path.join(BASE, "skyline-logo.png"), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


ICON_FILES = {"favicon.ico": "image/x-icon", "favicon-16.png": "image/png", "favicon-32.png": "image/png",
              "apple-touch-icon.png": "image/png", "icon-192.png": "image/png", "icon-512.png": "image/png"}


@app.get("/favicon.ico")
@app.get("/favicon-16.png")
@app.get("/favicon-32.png")
@app.get("/apple-touch-icon.png")
@app.get("/icon-192.png")
@app.get("/icon-512.png")
def app_icons():
    name = request.path.lstrip("/")
    resp = send_file(os.path.join(BASE, name), mimetype=ICON_FILES[name])
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/manifest.webmanifest")
def web_manifest():   # lets "Add to Home Screen" on an iPhone/Android open it like an app
    resp = jsonify({"name": "Skyline Whitespace Proposal Builder", "short_name": "Proposals", "start_url": "/", "display": "standalone",
                    "background_color": "#f8f9fb", "theme_color": "#1c1b4b",
                    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"}, {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]})
    resp.headers["Content-Type"] = "application/manifest+json"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/brand-fonts.css")
def brand_fonts_css():
    fonts = web_fonts()
    weights = {w for _, w, _ in fonts}
    rules = []
    for p, w, fmt in fonts:
        span = f"{w}" if len(weights) > 1 else "300 400"  # a single file stands in for both weights
        rules.append("@font-face { font-family: 'Europa'; src: url('/brand-fonts/%s') format('%s'); font-weight: %s; font-style: normal; font-display: swap; }"
                     % (os.path.basename(p), fmt, span))
    resp = app.response_class("\n".join(rules), mimetype="text/css")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/brand-fonts/<name>")
def brand_font_file(name):
    for p, _w, _f in web_fonts():
        if os.path.basename(p) == name:  # only the font files found above can be fetched
            resp = send_file(p)
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp
    return err("Not found.", 404)


# ---------------------------------------------------------------- static files
WEB_FILES = {"index.html", "app.js", "style.css"}


def web_path(name):
    """The page files can sit in the main folder (easiest to upload) or in a static/ folder.
    A copy in the main folder wins, so an old static/ folder left behind can't hide an update."""
    for folder in (BASE, os.path.join(BASE, "static")):
        p = os.path.join(folder, name)
        if name in WEB_FILES and os.path.exists(p):
            return p
    return None


@app.get("/")
def index():
    html = open(web_path("index.html"), encoding="utf8").read().replace("__V__", VERSION)
    resp = app.response_class(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/version")
def version():
    return jsonify({"version": VERSION, "library_tab": True, "ai_key_set": ai.configured(), "features": FEATURES})


@app.get("/static/<path:name>")
def static_files(name):
    p = web_path(name)
    if not p:
        return err("Not found.", 404)
    resp = send_file(p)
    resp.headers["Cache-Control"] = "no-cache"  # always check for a newer copy after an update
    return resp


# ---------------------------------------------------------------- upload handling
IMG_EXT = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff"}
# kind -> (longest side in px, keep transparency, PDF allowed)
KINDS = {
    "logo": (1600, True, False),
    "floorplan": (3000, False, True),
    "mood": (3000, False, False),
    "render": (3200, False, True),  # renders and technical drawings share this field; keep line work crisp
    "drawing": (3200, False, True),
    "custom": (3200, False, True),
}


def save_upload(fs, dest_dir, stem, kind):
    """Validate, shrink and store one uploaded file. Returns the stored path."""
    max_side, keep_alpha, pdf_ok = KINDS[kind]
    name = fs.filename or "file"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    os.makedirs(dest_dir, exist_ok=True)
    if ext == "pdf":
        if not pdf_ok:
            raise UserError(f"'{name}': please upload a PNG or JPG for this field, not a PDF.")
        head = fs.stream.read(5)
        fs.stream.seek(0)
        if head != b"%PDF-":
            raise UserError(f"'{name}' isn't a valid PDF.")
        path = os.path.join(dest_dir, f"{stem}.pdf")
        fs.save(path)
        if kind == "floorplan":  # needs a single image: use the first page
            prefix = os.path.join(dest_dir, stem + "_p")
            subprocess.run(["pdftoppm", "-jpeg", "-r", "150", "-f", "1", "-l", "1", path, prefix], check=True, timeout=120)
            first = sorted(f for f in os.listdir(dest_dir) if f.startswith(stem + "_p"))[0]
            return os.path.join(dest_dir, first)
        return path
    if ext not in IMG_EXT:
        raise UserError(f"'{name}': unsupported file type. Use PNG, JPG or PDF"
                        + (" (SVG logos aren't supported yet; please upload a PNG)." if ext == "svg" else "."))
    try:
        im = Image.open(fs.stream)
        im.load()
    except Exception:
        raise UserError(f"'{name}' couldn't be read as an image.")
    im = ImageOps.exif_transpose(im)
    im.thumbnail((max_side, max_side))  # only ever shrinks
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if keep_alpha and has_alpha:
        path = os.path.join(dest_dir, f"{stem}.png")
        im.convert("RGBA").save(path)
    else:
        if has_alpha:
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, "white")
            bg.paste(rgba, mask=rgba.getchannel("A"))
            im = bg
        path = os.path.join(dest_dir, f"{stem}.jpg")
        im.convert("RGB").save(path, quality=88, optimize=True)
    return path


def save_many(files, dest_dir, prefix, kind, origins=None):
    out = []
    for n, fs in enumerate(files, 1):
        if fs and fs.filename:
            p = save_upload(fs, dest_dir, f"{prefix}_{n:02d}", kind)
            got = generate.to_images(p, dest_dir) if p.endswith(".pdf") else [p]  # one image per PDF page
            out += got
            if origins is not None:
                origins += [(n - 1, len(got))] * len(got)   # which uploaded file each picture came from, and how many pictures it gave
    return out


ANNOTATION_COLOURS = ("#000000", "#FFFFFF")   # black or white, for arrows and label text


def clean_notes(raw):
    """Labels and arrows drawn on a render (fractions of the page): only the known shapes, inside the page, with the known colours."""
    out = []
    if not isinstance(raw, list):
        return out
    f = lambda v: min(1.0, max(0.0, float(v)))
    for n in raw[:30]:
        if not isinstance(n, dict):
            continue
        try:
            if n.get("t") == "label":
                txt = "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in str(n.get("text") or "").replace("\r", "").split("\n")[:4])[:160].strip()
                if txt:
                    c = str(n.get("c") or "").upper()
                    out.append({"t": "label", "x": f(n.get("x")), "y": f(n.get("y")), "text": txt,
                                "c": c if c in ANNOTATION_COLOURS else ANNOTATION_COLOURS[0]})
            elif n.get("t") == "arrow":
                c = str(n.get("c") or "").upper()
                out.append({"t": "arrow", "x1": f(n.get("x1")), "y1": f(n.get("y1")), "x2": f(n.get("x2")), "y2": f(n.get("y2")),
                            "c": c if c in ANNOTATION_COLOURS else ANNOTATION_COLOURS[0]})
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- input validation
LINE_KEYS = ["rental_structure", "graphics", "custom_rental", "flooring", "audio_visual", "furniture",
             "installation_dismantle", "project_management", "logistics_handling"]
DETAIL_KEYS = ["custom_rental", "flooring", "audio_visual", "furniture"]   # lines that list their contents as bullet points
DATE_KEYS = ["show_start", "concept_signoff", "quote_signoff", "artwork_deadline", "rush_after", "preshow_prep", "dispatch"]
HEX = re.compile(r"^#?[0-9a-fA-F]{6}$")


def text(v, label, required=False, maxlen=120):
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    if required and not s:
        raise UserError(f"{label} is required.")
    if len(s) > maxlen:
        raise UserError(f"{label} is too long (max {maxlen} characters).")
    return s


def amount(v, label):
    s = str(v if v is not None else "").strip()
    s = re.sub(r"[£€$,\s]", "", s)
    if s == "":
        return None
    try:
        x = float(s)
    except ValueError:
        raise UserError(f"'{label}' should be a number.")
    if not math.isfinite(x) or abs(x) > 1e10:
        raise UserError(f"'{label}' is out of range.")
    return round(x, 2)


def build_show_quote(q, label):
    """A show's quotation -> {"lines": {...}} (standard lines) or {"custom": [...]} (the salesperson's own lines),
    plus an optional "discount" (taken off the subtotal) and "options" (free-text lines with a + or - value, not in the total).
    None if the quotation is empty."""
    q = q or {}
    out = None
    if q.get("custom"):
        rows = []
        for r in (q.get("custom_lines") or [])[:16]:
            name = text(r.get("label"), f"{label}: field name", maxlen=70)
            desc = text(r.get("description"), f"{label}: field description", maxlen=100)
            v = amount(r.get("amount"), name or "Amount")
            if v is None:
                continue  # an empty line (or a name with no amount) doesn't appear
            if not name:
                raise UserError(f"{label}: please give each custom quote field a name.")
            rows.append({"label": name, "description": desc, "amount": v})
        if rows:
            out = {"custom": rows}
    else:
        lines = {}
        for k in LINE_KEYS:
            v = amount((q.get("lines") or {}).get(k), k.replace("_", " ").title())
            if v is not None:
                lines[k] = v
        if lines:
            out = {"lines": lines}
            details = {}
            for k in DETAIL_KEYS:
                if k not in lines:
                    continue
                bullets = [text(x, f"{label}: {k.replace('_', ' ')} details", maxlen=90) for x in str((q.get("details") or {}).get(k) or "").splitlines()]
                bullets = [b for b in bullets if b][:8]
                if bullets:
                    details[k] = bullets
            if details:
                out["details"] = details
    if out is None:
        return None
    subtotal = sum(r["amount"] for r in out["custom"]) if "custom" in out else sum(out["lines"].values())
    disc = amount(q.get("discount"), f"{label}: discount")
    if disc is not None and disc != 0:
        disc = abs(disc)    # typed as 500 or -500, it is always taken off
        if disc > subtotal:
            raise UserError(f"{label}: the discount is more than the subtotal.")
        out["discount"] = disc
    options = []
    for o in (q.get("options") or [])[:8]:
        t = text(o.get("text"), f"{label}: option", maxlen=80)
        v = amount(o.get("amount"), f"{label}: option value")
        if not t:
            if v is not None:
                raise UserError(f"{label}: please give each option a description.")
            continue
        options.append({"text": t, "sign": "-" if o.get("sign") == "-" else "+", "amount": None if v is None else abs(v)})
    if options:
        out["options"] = options
    return out


def build_data(payload, files, up_dir):
    c = payload.get("client") or {}
    client_name = text(c.get("name"), "Client name", required=True)
    logo_fs = files.get("client_logo")
    if not logo_fs or not logo_fs.filename:
        raise UserError("Please upload the client's logo.")
    data = {
        "currency": payload.get("currency") if payload.get("currency") in ("£", "€", "$") else "£",
        "client": {"name": client_name, "website": text(c.get("website"), "Website", maxlen=200),
                   "logo": save_upload(logo_fs, up_dir, "client_logo", "logo")},
        "shows": [],
    }
    shows = payload.get("shows") or []
    if not 1 <= len(shows) <= 6:
        raise UserError("Add between 1 and 6 shows.")
    for i, s in enumerate(shows):
        label = f"Show {i + 1}" if len(shows) > 1 else "Show"
        show = {
            "name": text(s.get("name"), f"{label}: name", required=True, maxlen=80),
            "location": text(s.get("location"), f"{label}: venue/location", required=True, maxlen=100),
            "dates": text(s.get("dates"), f"{label}: dates", required=True, maxlen=60),
            "stand_size": text(s.get("stand_size"), f"{label}: stand size", maxlen=40),
        }
        p = f"show{i}"
        origins = []
        renders = save_many(files.getlist(f"{p}_renders"), up_dir, f"{p}_render", "render", origins)
        if not renders:
            raise UserError(f"{label}: please upload at least one render or drawing.")
        show["renders"] = renders
        rn = s.get("render_notes")   # annotations, one list per uploaded file; used for a file that gave exactly one picture
        if isinstance(rn, list) and g.user["is_admin"]:   # labels and lines are admin-only for now
            notes = [clean_notes(rn[o]) if o < len(rn) and cnt == 1 else [] for o, cnt in origins]
            if any(notes):
                show["render_notes"] = notes
        quote = build_show_quote(s.get("quote"), label)
        if quote:
            show["quote"] = quote
        fs = files.get(f"{p}_floorplan")
        if fs and fs.filename:
            show["floorplan"] = save_upload(fs, up_dir, f"{p}_floorplan", "floorplan")
            box = s.get("booth_box")   # the red box marking the booth: [x, y, width, height] as fractions of the page
            try:
                bx = [min(1.0, max(0.0, float(v))) for v in box] if isinstance(box, list) and len(box) == 4 else None
            except (TypeError, ValueError):
                bx = None
            if bx and bx[2] >= 0.01 and bx[3] >= 0.01:
                show["booth_box"] = bx
            try:   # zoom and pan of the floorplan inside its fixed frame: [zoom, shift x, shift y]
                fv = s.get("floorplan_view")
                z, px_, py_ = [float(v) for v in fv] if isinstance(fv, list) and len(fv) == 3 else (1.0, 0.0, 0.0)
                show["floorplan_view"] = [min(8.0, max(1.0, z)), min(2.0, max(-2.0, px_)), min(2.0, max(-2.0, py_))]
            except (TypeError, ValueError):
                pass
        data["shows"].append(show)
    mood = []
    for k in ("mood_1", "mood_2"):
        fs = files.get(k)
        if fs and fs.filename:
            mood.append(save_upload(fs, up_dir, k, "mood"))
    if mood:
        data["mood_board"] = mood
        views = []
        for v in (payload.get("mood_view") or [])[:2]:   # which part of each picture shows, and how far it is zoomed
            try:
                views.append([min(1.0, max(0.0, float(v[0]))), min(1.0, max(0.0, float(v[1]))), min(4.0, max(1.0, float(v[2])))])
            except (TypeError, ValueError, IndexError):
                views.append([0.5, 0.5, 1.0])
        data["mood_view"] = views[:len(mood)]
    about = []
    for i in (payload.get("about") or [])[:30]:   # the "About the company" pages, in the order chosen
        it = isinstance(i, str) and ID_RE.fullmatch(i) and ls.get(LIB_DIR, i)
        if it and it["category"] == "about" and i not in about:
            about.append(i)
    data["about_pages"] = about
    refs = []
    for i in (payload.get("references") or [])[:40]:   # the "References" pages (examples of our work), in the order chosen
        it = isinstance(i, str) and ID_RE.fullmatch(i) and ls.get(LIB_DIR, i)
        if it and it["category"] == "references" and i not in refs:
            refs.append(i)
    data["reference_pages"] = refs
    pages = payload.get("pages") or {}
    data["include_critical_dates"] = bool(pages.get("critical_dates"))
    data["include_payment_terms"] = bool(pages.get("payment_terms", True))
    if data["include_critical_dates"]:
        dates = payload.get("critical_dates") or {}
        data["critical_dates"] = {k: text(dates.get(k), "Critical date", maxlen=24) for k in DATE_KEYS}
    return data


# ---------------------------------------------------------------- proposals
def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:80] or "proposal"


@app.post("/api/proposals")
@login_required
def create_proposal():
    try:
        payload = json.loads(request.form.get("data") or "{}")
    except ValueError:
        return err("Bad request.")
    pid = uuid.uuid4().hex[:12]
    pdir = os.path.join(PROPOSALS_DIR, pid)
    note = ""
    try:
        data = build_data(payload, request.files, os.path.join(pdir, "uploads"))
        renders = [p for s in data["shows"] for p in s["renders"]]
        sel, note = {}, ""
        if FEATURES["materials"]:  # the salesperson chooses the flooring, materials, furniture and lighting from the library
            sel = matcher.clean_selection(LIB_DIR, payload.get("materials") or {})
            sel.update(method="manual", materials_method="manual", design_summary="", mood_reasons={})
        totals = build_deck(pdir, data, sel)
    except UserError as e:
        shutil.rmtree(pdir, ignore_errors=True)
        return err(str(e))
    except Exception:
        log.exception("proposal generation failed")
        shutil.rmtree(pdir, ignore_errors=True)
        return err("Something went wrong building the proposal. Please try again, or contact support if it keeps happening.", 500)
    shows = ", ".join(s["name"] for s in data["shows"])
    title = f"{data['client']['name']} \u2014 {shows}"
    summary = {"client": data["client"]["name"], "shows": [s["name"] for s in data["shows"]],
               "currency": data["currency"], "renders": sum(len(s["renders"]) for s in data["shows"])}
    db().execute("INSERT INTO proposals (id, user_id, title, created, totals, summary) VALUES (?,?,?,?,?,?)",
                 (pid, g.user["id"], title, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  json.dumps(totals), json.dumps(summary)))
    db().commit()
    out = proposal_row(db().execute("SELECT p.*, u.email FROM proposals p JOIN users u ON u.id=p.user_id WHERE p.id=?", (pid,)).fetchone())
    out["selection"] = matcher.details(LIB_DIR, sel)
    out["note"] = note
    return jsonify(out)


def _summary_of(data):
    return {"client": data["client"]["name"], "shows": [s["name"] for s in data["shows"]],
            "currency": data["currency"], "renders": sum(len(s["renders"]) for s in data["shows"])}


@app.get("/api/proposals/<pid>/edit")
@login_required
def proposal_for_edit(pid):
    """Everything a proposal was made from, so the form can be filled in again for changes."""
    if owned(pid) is None:
        return err("Not found.", 404)
    pdir, data, sel = _proposal_files(pid)
    real = os.path.realpath(pdir)

    def asset(path):
        if not path or not os.path.isfile(path):
            return None
        rel = os.path.relpath(os.path.realpath(path), real)
        return {"url": f"/api/proposals/{pid}/asset?p=" + urllib.parse.quote(rel), "name": os.path.basename(path)}

    shows = []
    for s in data["shows"]:
        q = s.get("quote")
        quote = None
        if q and q.get("custom") is not None and "lines" not in q:
            quote = {"custom": True, "custom_lines": [{"label": r["label"], "description": r.get("description", ""), "amount": f"{r['amount']:.2f}"} for r in q["custom"]]}
        elif q:
            quote = {"custom": False, "lines": {k: f"{v:.2f}" for k, v in q["lines"].items()},
                     "details": {k: "\n".join(v) for k, v in (q.get("details") or {}).items()}}
        if quote:   # older proposals have neither: they load with no discount and no options
            quote["discount"] = f"{q['discount']:.2f}" if q.get("discount") else ""
            quote["options"] = [{"text": o["text"], "sign": o.get("sign", "+"), "amount": "" if o.get("amount") is None else f"{o['amount']:.2f}"} for o in (q.get("options") or [])]
        shows.append({"name": s["name"], "location": s["location"], "dates": s["dates"], "stand_size": s.get("stand_size", ""), "quote": quote,
                      "renders": [a for a in (asset(p) for p in s["renders"]) if a],
                      "render_notes": s.get("render_notes"),
                      "floorplan": asset(s.get("floorplan")), "booth_box": s.get("booth_box"), "floorplan_view": s.get("floorplan_view")})
    return jsonify({"title": owned(pid)["title"], "currency": data["currency"],
                    "client": {"name": data["client"]["name"], "website": data["client"].get("website", ""), "logo": asset(data["client"].get("logo"))},
                    "shows": shows, "mood": [a for a in (asset(p) for p in data.get("mood_board", [])) if a], "mood_view": data.get("mood_view", []),
                    "pages": {"critical_dates": bool(data.get("include_critical_dates")), "payment_terms": bool(data.get("include_payment_terms", True))},
                    "critical_dates": data.get("critical_dates", {}), "materials": matcher.details(LIB_DIR, sel),
                    "about": [i for i in data.get("about_pages", []) if ls.get(LIB_DIR, i)],
                    "references": [i for i in data.get("reference_pages", []) if ls.get(LIB_DIR, i)]})


@app.get("/api/proposals/<pid>/asset")
@login_required
def proposal_asset(pid):
    if owned(pid) is None:
        return err("Not found.", 404)
    real = os.path.realpath(os.path.join(PROPOSALS_DIR, pid))
    full = os.path.realpath(os.path.join(real, request.args.get("p", "")))
    if not full.startswith(real + os.sep) or not os.path.isfile(full):
        return err("Not found.", 404)
    return send_file(full)


@app.post("/api/proposals/<pid>")
@login_required
def update_proposal(pid):
    """Save changes to an existing proposal: the whole form comes back, and the deck is built again."""
    row = owned(pid)
    if row is None:
        return err("Not found.", 404)
    try:
        payload = json.loads(request.form.get("data") or "{}")
    except ValueError:
        return err("Bad request.")
    pdir = os.path.join(PROPOSALS_DIR, pid)
    up = os.path.join(pdir, f"uploads_{int(time.time() * 1000)}")
    backups = {}
    for n in ("data.json", "selection.json", "proposal.pptx"):
        p = os.path.join(pdir, n)
        if os.path.exists(p):
            backups[n] = open(p, "rb").read()

    def restore():
        for n, b in backups.items():
            open(os.path.join(pdir, n), "wb").write(b)
        shutil.rmtree(up, ignore_errors=True)
    try:
        data = build_data(payload, request.files, up)
        sel = {}
        if FEATURES["materials"]:
            sel = matcher.clean_selection(LIB_DIR, payload.get("materials") or {})
            sel.update(method="manual", materials_method="manual", design_summary="", mood_reasons={})
        totals = build_deck(pdir, data, sel)
    except UserError as e:
        restore()
        return err(str(e))
    except Exception:
        log.exception("proposal update failed")
        restore()
        return err("Something went wrong saving the changes. Nothing was changed. Please try again.", 500)
    for d in os.listdir(pdir):  # the pictures from before are no longer needed
        if d.startswith("uploads") and d != os.path.basename(up):
            shutil.rmtree(os.path.join(pdir, d), ignore_errors=True)
    shows = ", ".join(s["name"] for s in data["shows"])
    title = f"{data['client']['name']} \u2014 {shows}"
    db().execute("UPDATE proposals SET title=?, totals=?, summary=? WHERE id=?", (title, json.dumps(totals), json.dumps(_summary_of(data)), pid))
    db().commit()
    out = proposal_row(db().execute("SELECT p.*, u.email FROM proposals p JOIN users u ON u.id=p.user_id WHERE p.id=?", (pid,)).fetchone())
    return jsonify(out)


def proposal_row(r):
    return {"id": r["id"], "title": r["title"], "created": r["created"], "owner": r["email"],
            "totals": json.loads(r["totals"]), "summary": json.loads(r["summary"])}


@app.get("/api/proposals")
@login_required
def list_proposals():
    q = "SELECT p.*, u.email FROM proposals p JOIN users u ON u.id=p.user_id"
    args = ()
    if not g.user["is_admin"]:
        q += " WHERE p.user_id=?"
        args = (g.user["id"],)
    rows = db().execute(q + " ORDER BY p.created DESC LIMIT 200", args).fetchall()
    return jsonify([proposal_row(r) for r in rows])


def owned(pid):
    if not re.fullmatch(r"[0-9a-f]{12}", pid):
        return None
    row = db().execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if row and (g.user["is_admin"] or row["user_id"] == g.user["id"]):
        return row
    return None


_pdf_lock = threading.Lock()


def lighten_bold(pdf_path, factor):
    """LibreOffice fakes bold on Europa Light by thickening the letter outlines. This thins that
    outline (factor 1 = unchanged, 0 = plain Europa Light) so titles read as a light-bold."""
    import pikepdf
    if factor >= 0.999:
        return
    pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)
    changed = False
    for page in pdf.pages:
        ops, mode = [], 0
        for operands, op in pikepdf.parse_content_stream(page):
            name = str(op)
            if name == "Tr" and operands:
                mode = int(operands[0])
                if mode == 2 and factor <= 0.001:
                    operands, mode, changed = [0], 0, True
            elif name == "w" and mode == 2 and operands:
                operands, changed = [float(operands[0]) * factor], True
            ops.append((operands, op))
        if changed:
            page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(ops))
    if changed:
        pdf.save(pdf_path)
    pdf.close()


def ensure_pdf(pdir):
    pptx, pdf = os.path.join(pdir, "proposal.pptx"), os.path.join(pdir, "proposal.pdf")
    with _pdf_lock:  # LibreOffice doesn't like parallel conversions
        if not (os.path.exists(pdf) and os.path.getmtime(pdf) >= os.path.getmtime(pptx)):
            cmd = shlex.split(os.environ.get("SOFFICE_CMD", "soffice")) + [
                "--headless", "--convert-to", "pdf", "--outdir", pdir, pptx]
            subprocess.run(cmd, check=True, timeout=240, capture_output=True)
            try:
                lighten_bold(pdf, float(os.environ.get("TITLE_BOLD_STROKE", "0.35")))
            except Exception:
                log.exception("could not lighten PDF titles")
    return pdf


LAYOUT_LABELS = {"Cover": "Cover", "Inspiration": "Design Vision & Inspiration", "Materials": "Materials & Furnishings", "Floorplan": "Booth Location",
                 "Quote": "Quotation", "Critical Dates": "Critical Dates", "Terms 1": "Payment Terms (1)", "Terms 2": "Payment Terms (2)", "End": "Closing page"}


def slide_labels(pptx_path):
    """A short name for each page: set when the page was made, or from its layout, or its first piece of text."""
    from pptx import Presentation
    out = []
    for i, sl in enumerate(Presentation(pptx_path).slides, 1):
        label = next((sh.name[7:] for sh in sl.shapes if sh.name.startswith("label: ")), "")
        label = label or LAYOUT_LABELS.get(sl.slide_layout.name, "")
        if not label:
            for sh in sl.shapes:
                if sh.has_text_frame and sh.text_frame.text.strip():
                    label = " ".join(sh.text_frame.text.split())[:60]
                    break
        out.append(label or "Page")
    return out


@app.get("/api/proposals/<pid>/pages")
@login_required
def proposal_pages(pid):
    """Preview pictures of every page, in the current order, for the page-order screen."""
    if owned(pid) is None:
        return err("Not found.", 404)
    pdir = os.path.join(PROPOSALS_DIR, pid)
    try:
        pdf = ensure_pdf(pdir)
        tdir = os.path.join(pdir, "pages")
        stamp = os.path.join(tdir, ".stamp")
        if not (os.path.exists(stamp) and os.path.getmtime(stamp) >= os.path.getmtime(pdf)):
            shutil.rmtree(tdir, ignore_errors=True)
            os.makedirs(tdir)
            subprocess.run(["pdftoppm", "-jpeg", "-r", "32", pdf, os.path.join(tdir, "p")], check=True, timeout=240)
            open(stamp, "w").close()
        n = len([f for f in os.listdir(tdir) if f.endswith(".jpg")])
        labels = slide_labels(os.path.join(pdir, "proposal.pptx"))
    except Exception:
        log.exception("page previews failed")
        return err("The page previews couldn't be made. You can still download the PDF.", 500)
    return jsonify({"pages": [{"n": i, "label": labels[i] if i < len(labels) else f"Page {i + 1}"} for i in range(n)], "v": int(os.path.getmtime(pdf))})


@app.get("/api/proposals/<pid>/page/<int:n>")
@login_required
def proposal_page_image(pid, n):
    if owned(pid) is None:
        return err("Not found.", 404)
    tdir = os.path.join(PROPOSALS_DIR, pid, "pages")
    for pat in (f"p-{n + 1:02d}.jpg", f"p-{n + 1:03d}.jpg", f"p-{n + 1}.jpg"):
        p = os.path.join(tdir, pat)
        if os.path.exists(p):
            resp = send_file(p, mimetype="image/jpeg")
            resp.headers["Cache-Control"] = "private, max-age=600"
            return resp
    return err("Not found.", 404)


@app.post("/api/proposals/<pid>/order")
@login_required
def proposal_order(pid):
    """Put the pages in a new order: {order: [current page numbers, in the order wanted]}."""
    if owned(pid) is None:
        return err("Not found.", 404)
    order = (request.get_json(silent=True) or {}).get("order")
    path = os.path.join(PROPOSALS_DIR, pid, "proposal.pptx")
    from pptx import Presentation
    prs = Presentation(path)
    lst = prs.slides._sldIdLst
    ids = list(lst)
    if (not isinstance(order, list) or not order or len(set(order)) != len(order)
            or not all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(ids) for i in order)):
        return err("That page order isn't valid.")
    if order == list(range(len(ids))):
        return jsonify({"ok": True, "changed": False})
    for e in ids:
        lst.remove(e)
    for i in order:   # pages left out of the list are deleted
        lst.append(ids[i])
    for i, e in enumerate(ids):
        if i not in order:
            try:
                prs.part.drop_rel(e.rId)
            except Exception:
                pass
    tmp = path + ".tmp"
    prs.save(tmp)
    os.replace(tmp, path)   # the PDF and previews are made again from the new order
    return jsonify({"ok": True, "changed": True})


@app.get("/api/proposals/<pid>/download/<fmt>")
@login_required
def download(pid, fmt):
    row = owned(pid)
    if row is None or fmt not in ("pptx", "pdf"):
        return err("Not found.", 404)
    if fmt == "pptx" and not g.user["is_admin"]:  # only admins get the editable PowerPoint; everyone else gets the PDF
        return err("Only the PDF is available to download.", 403)
    pdir = os.path.join(PROPOSALS_DIR, pid)
    name = safe_name(json.loads(row["summary"])["client"]) + "-proposal." + fmt
    try:
        path = ensure_pdf(pdir) if fmt == "pdf" else os.path.join(pdir, "proposal.pptx")
    except Exception:
        log.exception("pdf conversion failed")
        return err("The PDF couldn't be created. Please try again.", 500)
    return send_file(path, as_attachment=True, download_name=name)


@app.delete("/api/proposals/<pid>")
@login_required
def delete_proposal(pid):
    if owned(pid) is None:
        return err("Not found.", 404)
    db().execute("DELETE FROM proposals WHERE id=?", (pid,))
    db().commit()
    shutil.rmtree(os.path.join(PROPOSALS_DIR, pid), ignore_errors=True)
    return jsonify({"ok": True})


@app.errorhandler(413)
def too_big(_e):
    return err("That's too much data in one go (limit 600 MB). Try fewer or smaller files.", 413)


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")))
