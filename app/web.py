"""Web interface: dashboard of tracked shows, per-show exhibitor lists, new-exhibitor feed, CSV export."""
from __future__ import annotations

import csv
import io
import logging
import os
import secrets
import threading
from datetime import date, datetime, timedelta, timezone

from flask import Flask, Response, abort, redirect, render_template, request, url_for

from . import tracker
from .db import connect, init_db
from .scrapers import PLATFORM_LABELS, detect_platform

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
NEW_WINDOW_DAYS = 7

app = Flask(__name__)


# ---------------------------------------------------------------- auth
@app.before_request
def require_password():
    if not APP_PASSWORD or request.path == "/healthz" or request.path.startswith("/static/"):
        return None
    auth = request.authorization
    if auth and secrets.compare_digest(auth.password or "", APP_PASSWORD):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Show Tracker"'})


# ---------------------------------------------------------------- helpers
def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _days_until(d: str | None):
    if not d:
        return None
    try:
        return (date.fromisoformat(d[:10]) - date.today()).days
    except ValueError:
        return None


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


def _run_in_background(show_id: int) -> None:
    threading.Thread(target=tracker.check_show, args=(show_id,), daemon=True).start()


# ---------------------------------------------------------------- pages
@app.get("/")
def dashboard():
    since = _since(NEW_WINDOW_DAYS)
    with connect() as db:
        shows = db.all(
            "SELECT s.*, "
            " (SELECT COUNT(*) FROM exhibitors e WHERE e.show_id = s.id AND e.removed_at IS NULL AND e.likely_junk = 0) AS active_count, "
            " (SELECT COUNT(*) FROM exhibitors e WHERE e.show_id = s.id AND e.in_first_snapshot = 0 "
            "    AND e.likely_junk = 0 AND e.first_seen_at >= ?) AS new_count "
            "FROM shows s",
            (since,),
        )
        totals = db.one(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT name_key) AS companies FROM exhibitors "
            "WHERE removed_at IS NULL AND likely_junk = 0"
        )
    for s in shows:
        s["days_until"] = _days_until(s["start_date"])
        s["status"], s["status_class"] = _status(s)
        s["platform_label"] = PLATFORM_LABELS.get(s["platform"], s["platform"])
    # upcoming first, then undated, then past
    shows.sort(key=lambda s: (s["days_until"] is None, (s["days_until"] or 0) < 0, abs(s["days_until"] or 0), s["name"].lower()))
    stats = {
        "shows": sum(1 for s in shows if s["active"]),
        "exhibitors": totals["n"] or 0,
        "companies": totals["companies"] or 0,
        "new": sum(s["new_count"] for s in shows),
        "problems": sum(1 for s in shows if s["status_class"] in ("bad", "warn")),
    }
    return render_template("dashboard.html", shows=shows, stats=stats, window=NEW_WINDOW_DAYS,
                           interval=tracker.CHECK_INTERVAL_HOURS)


@app.post("/shows")
def create_show():
    url = (request.form.get("url") or "").strip()
    if not url:
        return redirect(url_for("dashboard"))
    show_id = tracker.add_show(
        url,
        name=request.form.get("name", ""),
        start_date=request.form.get("start_date", ""),
        location=request.form.get("location", ""),
    )
    _run_in_background(show_id)
    return redirect(url_for("show_page", show_id=show_id))


@app.get("/shows/<int:show_id>")
def show_page(show_id: int):
    view = request.args.get("view", "all")
    q = (request.args.get("q") or "").strip()
    with connect() as db:
        show = db.one("SELECT * FROM shows WHERE id = ?", (show_id,))
        if not show:
            abort(404)
        rows = _exhibitor_rows(db, show_id, view, q)
        counts = db.one(
            "SELECT "
            " SUM(CASE WHEN removed_at IS NULL AND likely_junk = 0 THEN 1 ELSE 0 END) AS all_count, "
            " SUM(CASE WHEN in_first_snapshot = 0 AND likely_junk = 0 AND first_seen_at >= ? THEN 1 ELSE 0 END) AS new_count, "
            " SUM(CASE WHEN removed_at IS NOT NULL THEN 1 ELSE 0 END) AS removed_count, "
            " SUM(CASE WHEN likely_junk = 1 AND removed_at IS NULL THEN 1 ELSE 0 END) AS junk_count "
            "FROM exhibitors WHERE show_id = ?",
            (_since(NEW_WINDOW_DAYS), show_id),
        )
        runs = db.all("SELECT * FROM runs WHERE show_id = ? ORDER BY id DESC LIMIT 12", (show_id,))
        parent = db.one("SELECT id, name FROM shows WHERE id = ?", (show["parent_show_id"],)) if show["parent_show_id"] else None
        child = db.one("SELECT id, name FROM shows WHERE parent_show_id = ?", (show_id,))
    show["status"], show["status_class"] = _status(show)
    show["days_until"] = _days_until(show["start_date"])
    show["platform_label"] = PLATFORM_LABELS.get(show["platform"], show["platform"])
    counts = {k: v or 0 for k, v in counts.items()}
    return render_template("show.html", show=show, rows=rows, view=view, q=q, counts=counts, runs=runs,
                           window=NEW_WINDOW_DAYS, parent=parent, child=child)


def _exhibitor_rows(db, show_id: int, view: str, q: str) -> list[dict]:
    where = ["show_id = ?"]
    params: list = [show_id]
    if view == "new":
        where += ["in_first_snapshot = 0", "likely_junk = 0", "first_seen_at >= ?"]
        params.append(_since(NEW_WINDOW_DAYS))
    elif view == "removed":
        where.append("removed_at IS NOT NULL")
    elif view == "junk":
        where += ["likely_junk = 1", "removed_at IS NULL"]
    else:
        where += ["removed_at IS NULL", "likely_junk = 0"]
    if q:
        where.append("(LOWER(name) LIKE ? OR LOWER(COALESCE(booth, '')) LIKE ?)")
        params += [f"%{q.lower()}%"] * 2
    order = "first_seen_at DESC, name" if view == "new" else "name"
    return db.all(f"SELECT * FROM exhibitors WHERE {' AND '.join(where)} ORDER BY {order}", params)


@app.post("/shows/<int:show_id>/check")
def check_now(show_id: int):
    _run_in_background(show_id)
    return redirect(request.referrer or url_for("show_page", show_id=show_id))


@app.post("/shows/<int:show_id>/edit")
def edit_show(show_id: int):
    url = (request.form.get("url") or "").strip()
    with connect() as db:
        db.execute(
            "UPDATE shows SET name = ?, start_date = ?, location = ?, url = ?, platform = ? WHERE id = ?",
            (request.form.get("name", "").strip() or "Untitled show", request.form.get("start_date") or None,
             request.form.get("location", "").strip() or None, url, detect_platform(url), show_id),
        )
    return redirect(url_for("show_page", show_id=show_id))


@app.post("/shows/<int:show_id>/toggle")
def toggle_show(show_id: int):
    with connect() as db:
        db.execute("UPDATE shows SET active = 1 - active WHERE id = ?", (show_id,))
    return redirect(url_for("show_page", show_id=show_id))


@app.post("/shows/<int:show_id>/delete")
def delete_show(show_id: int):
    with connect() as db:
        db.execute("DELETE FROM runs WHERE show_id = ?", (show_id,))
        db.execute("DELETE FROM exhibitors WHERE show_id = ?", (show_id,))
        db.execute("UPDATE shows SET parent_show_id = NULL WHERE parent_show_id = ?", (show_id,))
        db.execute("DELETE FROM shows WHERE id = ?", (show_id,))
    return redirect(url_for("dashboard"))


@app.get("/new")
def new_feed():
    days = max(1, min(int(request.args.get("days", NEW_WINDOW_DAYS) or NEW_WINDOW_DAYS), 365))
    with connect() as db:
        rows = db.all(
            "SELECT e.*, s.name AS show_name, s.start_date AS show_date FROM exhibitors e "
            "JOIN shows s ON s.id = e.show_id "
            "WHERE e.in_first_snapshot = 0 AND e.likely_junk = 0 AND e.first_seen_at >= ? "
            "ORDER BY e.first_seen_at DESC, s.name, e.name",
            (_since(days),),
        )
        # companies seen at other tracked shows: useful context for outreach
        keys = list({r["name_key"] for r in rows})
        other = {}
        if keys:
            marks = ",".join("?" * len(keys))
            for r in db.all(
                f"SELECT name_key, COUNT(DISTINCT show_id) AS n FROM exhibitors WHERE name_key IN ({marks}) GROUP BY name_key",
                keys,
            ):
                other[r["name_key"]] = r["n"] - 1
    for r in rows:
        r["other_shows"] = other.get(r["name_key"], 0)
    return render_template("new.html", rows=rows, days=days)


@app.get("/export.csv")
def export_csv():
    show_id = request.args.get("show", type=int)
    view = request.args.get("view", "all")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["company", "booth", "website", "description", "profile_url", "show", "show_date",
                "first_seen", "last_seen", "removed", "sent_to_clay"])
    with connect() as db:
        if show_id:
            show = db.one("SELECT * FROM shows WHERE id = ?", (show_id,))
            if not show:
                abort(404)
            rows = _exhibitor_rows(db, show_id, view, "")
            for r in rows:
                r["show_name"], r["show_date"] = show["name"], show["start_date"]
            fname = f"{show['name']}-{view}.csv"
        else:
            rows = db.all(
                "SELECT e.*, s.name AS show_name, s.start_date AS show_date FROM exhibitors e "
                "JOIN shows s ON s.id = e.show_id WHERE e.in_first_snapshot = 0 AND e.likely_junk = 0 "
                "AND e.first_seen_at >= ? ORDER BY e.first_seen_at DESC",
                (_since(int(request.args.get("days", NEW_WINDOW_DAYS))),),
            )
            fname = "new-exhibitors.csv"
    for r in rows:
        w.writerow([r["name"], r["booth"] or "", r["website"] or "", r["description"] or "", r["profile_url"] or "",
                    r["show_name"], r["show_date"] or "", r["first_seen_at"][:10], r["last_seen_at"][:10],
                    (r["removed_at"] or "")[:10], (r["pushed_at"] or "")[:10]])
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in fname)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- startup
def create_app(start_scheduler: bool | None = None) -> Flask:
    init_db()
    if start_scheduler is None:
        start_scheduler = os.environ.get("RUN_SCHEDULER", "1") == "1"
    if start_scheduler and not app.config.get("SCHEDULER_STARTED"):
        tracker.start_scheduler()
        app.config["SCHEDULER_STARTED"] = True
    return app
