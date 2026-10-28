"""Web interface for Show Tracker."""
from __future__ import annotations

import csv
import io
import logging
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template, request, session,
                   url_for)
from markupsafe import Markup

from . import ai, amplemarket, auth, jobs, notify, prospecting, tracker
from . import settings as cfg
from .db import connect, init_db, log_activity, now
from .scrapers import PLATFORM_LABELS, detect_platform

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

NEW_WINDOW_DAYS = 7
APP_VERSION = "2026-10-28-b"

app = Flask(__name__)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))

PUBLIC = {"login", "healthz", "static", "amplemarket_webhook"}


# ---------------------------------------------------------------- auth
@app.before_request
def load_user():
    g.user = auth.get_user(session.get("uid"))
    if request.endpoint in PUBLIC or request.endpoint is None:
        return None
    if not g.user:
        return redirect(url_for("login", next=request.full_path if request.method == "GET" else None))
    if request.method == "POST" and request.endpoint not in PUBLIC:
        token = request.form.get("_csrf") or request.headers.get("X-CSRF")
        if not token or not secrets.compare_digest(token, session.get("csrf", "")):
            abort(400, "Your session expired. Go back, refresh the page and try again.")
    return None


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    review = 0
    if getattr(g, "user", None):
        with connect() as db:
            review = db.one("SELECT COUNT(*) AS n FROM leads WHERE status = 'review'")["n"]
    field = Markup(f'<input type="hidden" name="_csrf" value="{session["csrf"]}">')
    return {"csrf": session["csrf"], "csrf_field": field, "user": getattr(g, "user", None),
            "review_count": review, "version": APP_VERSION}


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.user or not g.user["is_admin"]:
            abort(403)
        return fn(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        user = auth.authenticate(request.form.get("email", ""), request.form.get("password", ""))
        if user:
            session.clear()
            session.permanent = True
            session["uid"] = user["id"]
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        error = "That email and password don't match."
        time.sleep(0.5)
    needs_setup = auth.user_count() == 0 and not os.environ.get("APP_PASSWORD")
    return render_template("login.html", error=error, needs_setup=needs_setup,
                           email=request.form.get("email", ""))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- helpers
def _since(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


@app.template_filter("ago")
def ago(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    days = int(secs // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


@app.template_filter("nicedate")
def nicedate(d: str | None) -> str:
    if not d:
        return ""
    try:
        return date.fromisoformat(d[:10]).strftime("%-d %b %Y")
    except ValueError:
        return d


@app.template_filter("thousands")
def thousands(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return ""


def _status(show: dict) -> tuple[str, str]:
    if tracker.is_running(show["id"]):
        return "Checking", "run"
    if not show["active"]:
        return "Paused", "muted"
    if show["last_checked_at"] is None:
        return "Queued", "muted"
    if show["last_error"] and not show["last_count"]:
        return "Error", "bad"
    if show["last_error"]:
        return "Warning", "warn"
    return "Live", "ok"


def _bg(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def _run_in_background(show_id: int) -> None:
    _bg(tracker.check_show, show_id)


def _back(default: str):
    ref = request.form.get("_next") or request.referrer
    return redirect(ref if ref else default)


def _sequences() -> list[dict]:
    """Amplemarket sequences, cached for 10 minutes."""
    cached = cfg.internal("_sequences_cache")
    if cached and time.time() - cached.get("at", 0) < 600:
        return cached["items"]
    if not amplemarket.configured():
        return []
    try:
        items = [s for s in amplemarket.list_sequences() if s["status"] in ("active", "paused", "")]
    except amplemarket.AmplemarketError as exc:
        flash(f"Couldn't load sequences from Amplemarket: {exc}", "bad")
        return []
    cfg.set_internal("_sequences_cache", {"at": time.time(), "items": items})
    return items


# ---------------------------------------------------------------- shows
@app.get("/")
def dashboard():
    since = _since(NEW_WINDOW_DAYS)
    with connect() as db:
        shows = db.all(
            "SELECT s.*, "
            " (SELECT COUNT(*) FROM exhibitors e WHERE e.show_id = s.id AND e.removed_at IS NULL AND e.likely_junk = 0) AS active_count, "
            " (SELECT COUNT(*) FROM exhibitors e WHERE e.show_id = s.id AND e.in_first_snapshot = 0 "
            "    AND e.likely_junk = 0 AND e.first_seen_at >= ?) AS new_count, "
            " (SELECT COUNT(*) FROM leads l WHERE l.show_id = s.id AND l.status IN ('sent', 'replied')) AS sent_count "
            "FROM shows s", (since,))
        totals = db.one("SELECT COUNT(*) AS n, COUNT(DISTINCT name_key) AS companies FROM exhibitors "
                        "WHERE removed_at IS NULL AND likely_junk = 0")
        lead_stats = db.one(
            "SELECT SUM(CASE WHEN status IN ('sent','replied') AND sent_at >= ? THEN 1 ELSE 0 END) AS sent, "
            "SUM(CASE WHEN status = 'replied' AND replied_at >= ? THEN 1 ELSE 0 END) AS replied FROM leads",
            (_since(30), _since(30)))
        activity = db.all("SELECT * FROM activity ORDER BY id DESC LIMIT 12")
        suggestions = db.one("SELECT COUNT(*) AS n FROM discovered_shows WHERE status = 'suggested'")["n"]
    for s in shows:
        s["days_until"] = prospecting.days_until(s["start_date"])
        s["status"], s["status_class"] = _status(s)
        s["platform_label"] = PLATFORM_LABELS.get(s["platform"], s["platform"])
    shows.sort(key=lambda s: (s["days_until"] is None, (s["days_until"] or 0) < 0, abs(s["days_until"] or 0), s["name"].lower()))
    stats = {
        "shows": sum(1 for s in shows if s["active"]),
        "exhibitors": totals["n"] or 0,
        "new": sum(s["new_count"] for s in shows),
        "sent": lead_stats["sent"] or 0,
        "replied": lead_stats["replied"] or 0,
    }
    return render_template("dashboard.html", shows=shows, stats=stats, window=NEW_WINDOW_DAYS, activity=activity,
                           suggestions=suggestions, setup=jobs.briefing_data()["setup"])


@app.post("/shows")
def create_show():
    url = (request.form.get("url") or "").strip()
    if not url:
        return redirect(url_for("dashboard"))
    show_id = tracker.add_show(url, name=request.form.get("name", ""), start_date=request.form.get("start_date", ""),
                               location=request.form.get("location", ""))
    if request.form.get("discovered_id"):
        with connect() as db:
            db.execute("UPDATE discovered_shows SET status = 'added' WHERE id = ?", (request.form["discovered_id"],))
    _run_in_background(show_id)
    return redirect(url_for("show_page", show_id=show_id))


EXH_VIEWS = {
    "all": ("All", "e.removed_at IS NULL AND e.likely_junk = 0"),
    "new": ("New", "e.in_first_snapshot = 0 AND e.likely_junk = 0 AND e.first_seen_at >= :since"),
    "queued": ("Prospects", "e.stage = 'queued'"),
    "skipped": ("Skipped", "e.stage IN ('skipped', 'low_score', 'not_found', 'no_contacts')"),
    "removed": ("Dropped out", "e.removed_at IS NOT NULL"),
    "junk": ("Not companies", "e.likely_junk = 1 AND e.removed_at IS NULL"),
}


@app.get("/shows/<int:show_id>")
def show_page(show_id: int):
    view = request.args.get("view", "all")
    if view not in EXH_VIEWS:
        view = "all"
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort", "booth" if view in ("all", "queued") else "name")
    since = _since(NEW_WINDOW_DAYS)
    with connect() as db:
        show = db.one("SELECT * FROM shows WHERE id = ?", (show_id,))
        if not show:
            abort(404)
        counts = {}
        for key, (_, cond) in EXH_VIEWS.items():
            counts[key] = db.one(f"SELECT COUNT(*) AS n FROM exhibitors e WHERE e.show_id = ? AND {cond.replace(':since', '?')}",
                                 (show_id, since) if ":since" in cond else (show_id,))["n"]
        where = EXH_VIEWS[view][1].replace(":since", "?")
        params: list = [show_id] + ([since] if ":since" in EXH_VIEWS[view][1] else [])
        if q:
            where += " AND (LOWER(e.name) LIKE ? OR LOWER(COALESCE(e.booth, '')) LIKE ?)"
            params += [f"%{q.lower()}%"] * 2
        order = {"booth": "COALESCE(e.booth_sqft, 0) DESC, e.name", "score": "COALESCE(e.score, -1) DESC, e.name",
                 "new": "e.first_seen_at DESC, e.name"}.get(sort, "e.name")
        rows = db.all(
            "SELECT e.*, (SELECT COUNT(*) FROM leads l WHERE l.exhibitor_id = e.id) AS lead_count, "
            "(SELECT MAX(status) FROM leads l WHERE l.exhibitor_id = e.id AND l.status IN ('sent','replied')) AS lead_sent "
            f"FROM exhibitors e WHERE e.show_id = ? AND {where} ORDER BY {order}", params)
        runs = db.all("SELECT * FROM runs WHERE show_id = ? ORDER BY id DESC LIMIT 10", (show_id,))
        parent = db.one("SELECT id, name FROM shows WHERE id = ?", (show["parent_show_id"],)) if show["parent_show_id"] else None
        child = db.one("SELECT id, name FROM shows WHERE parent_show_id = ?", (show_id,))
        funnel = db.one(
            "SELECT SUM(CASE WHEN stage IS NOT NULL THEN 1 ELSE 0 END) AS looked, "
            "SUM(CASE WHEN stage = 'queued' THEN 1 ELSE 0 END) AS prospects FROM exhibitors WHERE show_id = ?", (show_id,))
        lead_counts = {r["status"]: r["n"] for r in db.all(
            "SELECT status, COUNT(*) AS n FROM leads WHERE show_id = ? GROUP BY status", (show_id,))}
    show["status"], show["status_class"] = _status(show)
    show["days_until"] = prospecting.days_until(show["start_date"])
    show["platform_label"] = PLATFORM_LABELS.get(show["platform"], show["platform"])
    conf = cfg.all_settings()
    window = None
    if show["days_until"] is not None:
        window = {"opens_in": show["days_until"] - conf["window_start_days"],
                  "closes_in": show["days_until"] - conf["window_end_days"]}
    return render_template("show.html", show=show, rows=rows, view=view, q=q, sort=sort, counts=counts, runs=runs,
                           views=EXH_VIEWS, window_days=NEW_WINDOW_DAYS, parent=parent, child=child,
                           funnel=funnel, lead_counts=lead_counts, outreach_window=window,
                           sequences=_sequences() if show["prospecting"] else [], conf=conf)


@app.post("/shows/<int:show_id>/check")
def check_now(show_id: int):
    _run_in_background(show_id)
    return _back(url_for("show_page", show_id=show_id))


@app.post("/shows/<int:show_id>/edit")
def edit_show(show_id: int):
    url = (request.form.get("url") or "").strip()
    with connect() as db:
        db.execute("UPDATE shows SET name = ?, start_date = ?, location = ?, url = ?, platform = ? WHERE id = ?",
                   (request.form.get("name", "").strip() or "Untitled show", request.form.get("start_date") or None,
                    request.form.get("location", "").strip() or None, url, detect_platform(url), show_id))
    flash("Show details saved.", "ok")
    return redirect(url_for("show_page", show_id=show_id))


@app.post("/shows/<int:show_id>/prospecting")
def show_prospecting(show_id: int):
    on = request.form.get("prospecting") == "1"
    with connect() as db:
        db.execute("UPDATE shows SET prospecting = ?, auto_send = ?, sequence_id = ? WHERE id = ?",
                   (1 if on else 0, 1 if request.form.get("auto_send") == "1" else 0,
                    request.form.get("sequence_id") or None, show_id))
        name = db.one("SELECT name FROM shows WHERE id = ?", (show_id,))["name"]
    if on and request.form.get("was_on") != "1":
        log_activity("prospecting", f"{g.user['email']} turned on prospecting for {name}", f"show:{show_id}")
        flash("Prospecting is on. The best-fit exhibitors are researched first, within your daily limit; "
              "they'll appear in Outreach as they're ready.", "ok")
        _bg(prospecting.research_due, 10)
    else:
        flash("Saved.", "ok")
    return redirect(url_for("show_page", show_id=show_id))


@app.post("/shows/<int:show_id>/toggle")
def toggle_show(show_id: int):
    with connect() as db:
        db.execute("UPDATE shows SET active = 1 - active WHERE id = ?", (show_id,))
    return redirect(url_for("show_page", show_id=show_id))


@app.post("/shows/<int:show_id>/delete")
def delete_show(show_id: int):
    with connect() as db:
        db.execute("DELETE FROM leads WHERE show_id = ?", (show_id,))
        db.execute("DELETE FROM runs WHERE show_id = ?", (show_id,))
        db.execute("DELETE FROM exhibitors WHERE show_id = ?", (show_id,))
        db.execute("UPDATE shows SET parent_show_id = NULL WHERE parent_show_id = ?", (show_id,))
        db.execute("DELETE FROM shows WHERE id = ?", (show_id,))
    flash("Show deleted.", "ok")
    return redirect(url_for("dashboard"))


@app.post("/exhibitors/<int:ex_id>/research")
def research_one(ex_id: int):
    with connect() as db:
        ex = db.one("SELECT * FROM exhibitors WHERE id = ?", (ex_id,))
        if not ex:
            abort(404)
        show = db.one("SELECT * FROM shows WHERE id = ?", (ex["show_id"],))
        db.execute("UPDATE exhibitors SET stage = NULL WHERE id = ?", (ex_id,))
    ex["stage"] = None
    try:
        stage = prospecting.process_exhibitor(ex, show, cfg.all_settings())
        msg = {"queued": "Researched: contacts are waiting in Outreach.",
               "skipped": "Skipped: doesn't fit your targeting (see the score notes).",
               "low_score": "Researched, but scored below your minimum.",
               "not_found": "Amplemarket couldn't match this company.",
               "no_contacts": "No suitable contacts with a checked email."}.get(stage, stage)
        flash(f"{ex['name']}: {msg}", "ok" if stage == "queued" else "warn")
    except amplemarket.AmplemarketError as exc:
        flash(f"Amplemarket: {exc}", "bad")
    return _back(url_for("show_page", show_id=ex["show_id"]))


# ---------------------------------------------------------------- new exhibitors
@app.get("/new")
def new_feed():
    days = max(1, min(request.args.get("days", NEW_WINDOW_DAYS, type=int) or NEW_WINDOW_DAYS, 365))
    with connect() as db:
        rows = db.all(
            "SELECT e.*, s.name AS show_name, s.start_date AS show_date FROM exhibitors e "
            "JOIN shows s ON s.id = e.show_id WHERE e.in_first_snapshot = 0 AND e.likely_junk = 0 AND e.first_seen_at >= ? "
            "ORDER BY e.first_seen_at DESC, COALESCE(e.booth_sqft, 0) DESC, e.name", (_since(days),))
        keys = list({r["name_key"] for r in rows})
        other = {}
        if keys:
            marks = ",".join("?" * len(keys))
            for r in db.all(f"SELECT name_key, COUNT(DISTINCT show_id) AS n FROM exhibitors WHERE name_key IN ({marks}) "
                            "GROUP BY name_key", keys):
                other[r["name_key"]] = r["n"] - 1
    for r in rows:
        r["other_shows"] = other.get(r["name_key"], 0)
    return render_template("new.html", rows=rows, days=days)


# ---------------------------------------------------------------- outreach
LEAD_VIEWS = {
    "review": ("To approve", "l.status = 'review'"),
    "scheduled": ("Scheduled", "l.status = 'approved'"),
    "sent": ("In sequence", "l.status = 'sent'"),
    "replied": ("Replied", "l.status = 'replied'"),
    "other": ("Rejected & skipped", "l.status IN ('rejected', 'skipped', 'expired')"),
}


@app.get("/outreach")
def outreach():
    view = request.args.get("view", "review")
    if view not in LEAD_VIEWS:
        view = "review"
    show_filter = request.args.get("show", type=int)
    with connect() as db:
        counts = {k: db.one(f"SELECT COUNT(*) AS n FROM leads l WHERE {cond}")["n"] for k, (_, cond) in LEAD_VIEWS.items()}
        where, params = LEAD_VIEWS[view][1], []
        if show_filter:
            where += " AND l.show_id = ?"
            params.append(show_filter)
        order = "l.replied_at DESC" if view == "replied" else "l.sent_at DESC" if view == "sent" else \
            "s.start_date, l.score DESC, co.name"
        leads = db.all(prospecting.LEAD_QUERY + f" WHERE {where} ORDER BY {order} LIMIT 300", params)
        shows = db.all("SELECT s.id, s.name, COUNT(l.id) AS n FROM shows s JOIN leads l ON l.show_id = s.id "
                       f"WHERE {LEAD_VIEWS[view][1]} GROUP BY s.id, s.name ORDER BY s.name")
    groups: list[dict] = []
    for lead in leads:
        if not groups or groups[-1]["key"] != (lead["company_id"], lead["show_id"]):
            groups.append({"key": (lead["company_id"], lead["show_id"]), "company": lead["company_name"],
                           "show": lead["show_name"], "show_id": lead["show_id"], "show_date": lead["show_date"],
                           "booth": lead["booth"], "booth_sqft": lead["booth_sqft"], "booth_dims": lead["booth_dims"],
                           "domain": lead["domain"], "employees": lead["employees"], "industry": lead["industry"],
                           "score": lead["score"], "score_notes": lead["score_notes"], "leads": []})
        groups[-1]["leads"].append(lead)
    conf = cfg.all_settings()
    return render_template("outreach.html", view=view, views=LEAD_VIEWS, counts=counts, groups=groups, shows=shows,
                           show_filter=show_filter, conf=conf, done_today=prospecting.research_done_today(),
                           am_ok=amplemarket.configured(), ai_ok=ai.configured(),
                           today=date.today().isoformat())


@app.post("/outreach/decide")
def outreach_decide():
    ids = [int(x) for x in request.form.getlist("lead") if x.isdigit()]
    action = request.form.get("action")
    if action == "approve":
        n = prospecting.approve(ids, g.user["email"])
        flash(f"Approved {n} contact{'s' if n != 1 else ''}. They go into Amplemarket when the show's outreach window opens.", "ok")
        _bg(prospecting.send_due)
    elif action == "reject":
        prospecting.reject(ids)
        flash(f"Rejected {len(ids)}.", "ok")
    return _back(url_for("outreach"))


@app.post("/leads/<int:lead_id>/edit")
def lead_edit(lead_id: int):
    with connect() as db:
        db.execute("UPDATE leads SET subject = ?, opener = ? WHERE id = ? AND status IN ('review', 'approved')",
                   (request.form.get("subject", "").strip()[:120], request.form.get("opener", "").strip()[:700], lead_id))
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True}
    flash("Saved.", "ok")
    return _back(url_for("outreach"))


@app.post("/leads/<int:lead_id>/rewrite")
def lead_rewrite(lead_id: int):
    try:
        prospecting.rewrite(lead_id)
        flash("Rewritten.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "bad")
    return _back(url_for("outreach"))


@app.post("/outreach/run")
def outreach_run():
    n = max(1, min(request.form.get("n", 10, type=int) or 10, 50))
    _bg(prospecting.research_due, n)
    flash(f"Researching up to {n} more companies now. Refresh in a minute or two.", "ok")
    return _back(url_for("outreach"))


@app.post("/outreach/send")
def outreach_send():
    result = prospecting.send_due()
    if "skipped" in result:
        flash("Amplemarket isn't connected yet.", "bad")
    else:
        flash(f"Sent {result['sent']} to Amplemarket" + (f", {result['failed']} failed" if result["failed"] else "") + ".",
              "ok" if not result["failed"] else "warn")
    return _back(url_for("outreach"))


# ---------------------------------------------------------------- discover
@app.get("/discover")
def discover():
    with connect() as db:
        suggested = db.all("SELECT * FROM discovered_shows WHERE status = 'suggested' ORDER BY start_date IS NULL, start_date")
        dismissed = db.one("SELECT COUNT(*) AS n FROM discovered_shows WHERE status = 'dismissed'")["n"]
    return render_template("discover.html", suggested=suggested, dismissed=dismissed, ai_ok=ai.configured(),
                           conf=cfg.all_settings(), last=cfg.internal("_last_discovery"))


@app.post("/discover/run")
def discover_run():
    if not ai.configured():
        flash("Add your Anthropic API key first (ANTHROPIC_API_KEY on Render).", "bad")
        return redirect(url_for("discover"))
    if not cfg.lines(cfg.get("discovery_topics")):
        flash("Add the industries you want shows for in Settings → Finding new shows first.", "warn")
        return redirect(url_for("discover"))

    def run():
        try:
            jobs.maybe_discover(force=True)
        except ai.AIError as exc:
            log_activity("discovery", f"Show search failed: {exc}")
    _bg(run)
    flash("Searching the web for shows. This takes a minute or two; refresh to see results.", "ok")
    return redirect(url_for("discover"))


@app.post("/discover/<int:did>/dismiss")
def discover_dismiss(did: int):
    with connect() as db:
        db.execute("UPDATE discovered_shows SET status = 'dismissed' WHERE id = ?", (did,))
    return redirect(url_for("discover"))


# ---------------------------------------------------------------- settings
@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        values = {}
        for key, (default, kind, *_rest) in cfg.FIELDS.items():
            if kind == "bool":
                values[key] = request.form.get(key) == "1"
            elif key in request.form:
                raw = request.form.get(key, "")
                if kind == "int":
                    try:
                        values[key] = int(raw)
                    except ValueError:
                        continue
                else:
                    values[key] = raw.strip()
        cfg.save(values)
        flash("Settings saved.", "ok")
        return redirect(url_for("settings_page"))
    base = os.environ.get("APP_BASE_URL", "").rstrip("/") or request.host_url.rstrip("/")
    status = {
        "Amplemarket": amplemarket.configured(),
        "Claude (writing & show search)": ai.configured(),
        "Email (briefing & alerts)": notify.email_configured(),
        "Slack alerts": bool(notify.SLACK_WEBHOOK_URL),
        "Clay webhook": bool(notify.CLAY_WEBHOOK_URL),
    }
    return render_template("settings.html", conf=cfg.all_settings(), fields=cfg.FIELDS, sections=cfg.SECTIONS,
                           sequences=_sequences(), status=status,
                           webhook_url=f"{base}/webhooks/amplemarket?token={cfg.webhook_token()}")


@app.post("/settings/test-briefing")
def test_briefing():
    to = [t for t in str(cfg.get("briefing_to") or "").split(",") if t.strip()] or [g.user["email"]]
    subject, body = jobs.build_briefing()
    ok = notify.send_email(to, subject, notify.wrap_email(subject, body))
    flash(f"Briefing sent to {', '.join(to)}." if ok else
          "Couldn't send. Check the SMTP settings on Render (see the README).", "ok" if ok else "bad")
    return redirect(url_for("settings_page"))


@app.post("/settings/refresh-sequences")
def refresh_sequences():
    cfg.set_internal("_sequences_cache", None)
    return redirect(url_for("settings_page"))


@app.get("/briefing")
def briefing_preview():
    subject, body = jobs.build_briefing()
    return notify.wrap_email(subject, body)


# ---------------------------------------------------------------- users
@app.route("/users", methods=["GET", "POST"])
@admin_required
def users_page():
    if request.method == "POST":
        try:
            auth.create_user(request.form["email"], request.form["password"], request.form.get("name", ""),
                             request.form.get("is_admin") == "1")
            flash(f"Added {request.form['email']}. Send them the password you set; they can change it under My details.", "ok")
        except (ValueError, KeyError) as exc:
            flash(str(exc), "bad")
        return redirect(url_for("users_page"))
    return render_template("users.html", users=auth.list_users())


@app.post("/users/<int:uid>/delete")
@admin_required
def users_delete(uid: int):
    if uid == g.user["id"]:
        flash("You can't remove yourself.", "bad")
    else:
        auth.delete_user(uid)
        flash("User removed.", "ok")
    return redirect(url_for("users_page"))


@app.post("/users/<int:uid>/password")
@admin_required
def users_password(uid: int):
    pw = request.form.get("password", "")
    if len(pw) < 8:
        flash("Use at least 8 characters.", "bad")
    else:
        auth.set_password(uid, pw)
        flash("Password updated.", "ok")
    return redirect(url_for("users_page"))


@app.route("/me", methods=["GET", "POST"])
def me():
    if request.method == "POST":
        if not auth.authenticate(g.user["email"], request.form.get("current", "")):
            flash("Your current password isn't right.", "bad")
        elif len(request.form.get("new", "")) < 8:
            flash("Use at least 8 characters for the new password.", "bad")
        else:
            auth.set_password(g.user["id"], request.form["new"])
            flash("Password changed.", "ok")
        return redirect(url_for("me"))
    return render_template("me.html")


# ---------------------------------------------------------------- export & webhooks
@app.get("/export.csv")
def export_csv():
    show_id = request.args.get("show", type=int)
    view = request.args.get("view", "all")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["company", "booth", "booth_sqft", "booth_dims", "hall", "website", "description", "profile_url",
                "show", "show_date", "first_seen", "last_seen", "removed", "stage", "score"])
    with connect() as db:
        if show_id:
            show = db.one("SELECT * FROM shows WHERE id = ?", (show_id,))
            if not show:
                abort(404)
            cond = EXH_VIEWS.get(view, EXH_VIEWS["all"])[1]
            rows = db.all(f"SELECT e.*, ? AS show_name, ? AS show_date FROM exhibitors e WHERE e.show_id = ? "
                          f"AND {cond.replace(':since', '?')} ORDER BY e.name",
                          [show["name"], show["start_date"], show_id] + ([_since(NEW_WINDOW_DAYS)] if ":since" in cond else []))
            fname = f"{show['name']}-{view}.csv"
        else:
            rows = db.all("SELECT e.*, s.name AS show_name, s.start_date AS show_date FROM exhibitors e "
                          "JOIN shows s ON s.id = e.show_id WHERE e.in_first_snapshot = 0 AND e.likely_junk = 0 "
                          "AND e.first_seen_at >= ? ORDER BY e.first_seen_at DESC",
                          (_since(request.args.get("days", NEW_WINDOW_DAYS, type=int)),))
            fname = "new-exhibitors.csv"
    for r in rows:
        w.writerow([r["name"], r["booth"] or "", r["booth_sqft"] or "", r["booth_dims"] or "", r["hall"] or "",
                    r["website"] or "", r["description"] or "", r["profile_url"] or "", r["show_name"],
                    r["show_date"] or "", r["first_seen_at"][:10], r["last_seen_at"][:10], (r["removed_at"] or "")[:10],
                    r["stage"] or "", r["score"] if r["score"] is not None else ""])
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in fname)
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@app.post("/webhooks/amplemarket")
def amplemarket_webhook():
    if not secrets.compare_digest(request.args.get("token", ""), cfg.webhook_token()):
        abort(403)
    payload = request.get_json(silent=True) or {}
    if payload.get("is_reply") or payload.get("labels") or payload.get("body"):
        return prospecting.handle_reply(payload)
    return {"ignored": True}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- startup
def create_app(start_scheduler: bool | None = None) -> Flask:
    init_db()
    auth.ensure_admin()
    app.secret_key = auth.secret_key()
    if start_scheduler is None:
        start_scheduler = os.environ.get("RUN_SCHEDULER", "1") == "1"
    if start_scheduler and not app.config.get("SCHEDULER_STARTED"):
        jobs.start()
        app.config["SCHEDULER_STARTED"] = True
    return app
