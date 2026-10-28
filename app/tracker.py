"""Checks each show's exhibitor list, records what's new or gone, and hands new exhibitors on."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from . import notify
from .db import connect, log_activity, now
from .scrapers import (
    ScrapeError,
    detect_platform,
    looks_like_junk,
    mys_next_edition_url,
    name_key,
    scrape,
)

log = logging.getLogger("showtracker")

CHECK_INTERVAL_HOURS = float(os.environ.get("CHECK_INTERVAL_HOURS", "24"))
# If a check suddenly returns far fewer exhibitors than last time, it's almost always the
# site misbehaving, not exhibitors pulling out. Below this share we don't mark anyone removed.
REMOVAL_SANITY_SHARE = 0.6

_running: set[int] = set()
_running_lock = threading.Lock()


def is_running(show_id: int) -> bool:
    return show_id in _running


def add_show(url: str, name: str = "", start_date: str = "", location: str = "", parent_show_id=None) -> int:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    platform = detect_platform(url)
    if not name:
        name = re.sub(r"\.mapyourshow\.com$", "", re.sub(r"^www\.", "", url.split("/")[2]))
    with connect() as db:
        existing = db.one("SELECT id FROM shows WHERE url = ?", (url,))
        if existing:
            return existing["id"]
        return db.insert(
            "INSERT INTO shows (name, url, platform, start_date, location, created_at, parent_show_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name.strip(), url, platform, start_date or None, location.strip() or None, now(), parent_show_id),
        )


def check_show(show_id: int, scraper=scrape) -> dict:
    """Scrape one show and store the diff. Returns a summary dict."""
    with _running_lock:
        if show_id in _running:
            return {"skipped": True}
        _running.add(show_id)
    try:
        return _check_show(show_id, scraper)
    finally:
        _running.discard(show_id)


def _check_show(show_id: int, scraper) -> dict:
    with connect() as db:
        show = db.one("SELECT * FROM shows WHERE id = ?", (show_id,))
        if not show:
            raise ValueError(f"No show {show_id}")
        run_id = db.insert("INSERT INTO runs (show_id, started_at) VALUES (?, ?)", (show_id, now()))

    try:
        rows = scraper(show["url"], show["platform"])
    except ScrapeError as exc:
        _finish_failed(show_id, run_id, str(exc))
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - keep the scheduler alive, record what happened
        log.exception("check failed for show %s", show_id)
        _finish_failed(show_id, run_id, f"Unexpected error: {exc.__class__.__name__}: {exc}")
        return {"error": str(exc)}

    ts = now()
    first_check = show["first_checked_at"] is None
    added: list[dict] = []
    removed = 0
    warning = None

    with connect() as db:
        current = {
            r["name_key"]: r
            for r in db.all("SELECT id, name_key, removed_at FROM exhibitors WHERE show_id = ?", (show_id,))
        }
        active_before = sum(1 for r in current.values() if r["removed_at"] is None)
        seen = set()
        for r in rows:
            key = name_key(r["name"])
            if not key or key in seen:
                continue
            seen.add(key)
            sqft, dims, hall = r.get("booth_sqft"), r.get("booth_dims") or "", r.get("hall") or ""
            if key in current:
                db.execute(
                    "UPDATE exhibitors SET name = ?, booth = ?, "
                    "description = CASE WHEN ? <> '' THEN ? ELSE description END, "
                    "website = CASE WHEN ? <> '' THEN ? ELSE website END, "
                    "profile_url = CASE WHEN ? <> '' THEN ? ELSE profile_url END, "
                    "booth_sqft = COALESCE(?, booth_sqft), "
                    "booth_dims = CASE WHEN ? <> '' THEN ? ELSE booth_dims END, "
                    "hall = CASE WHEN ? <> '' THEN ? ELSE hall END, "
                    "last_seen_at = ?, removed_at = NULL WHERE id = ?",
                    (r["name"], r["booth"], r["description"], r["description"], r["website"], r["website"],
                     r["profile_url"], r["profile_url"], sqft, dims, dims, hall, hall, ts, current[key]["id"]),
                )
            else:
                junk = 1 if looks_like_junk(r["name"]) else 0
                new_id = db.insert(
                    "INSERT INTO exhibitors (show_id, name, name_key, booth, description, website, profile_url, "
                    "booth_sqft, booth_dims, hall, likely_junk, first_seen_at, last_seen_at, in_first_snapshot) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (show_id, r["name"], key, r["booth"], r["description"], r["website"], r["profile_url"],
                     sqft, dims or None, hall or None, junk, ts, ts, 1 if first_check else 0),
                )
                if not first_check and not junk:
                    added.append({**r, "id": new_id})

        missing = [r for k, r in current.items() if k not in seen and r["removed_at"] is None]
        if missing:
            if active_before and len(seen) < active_before * REMOVAL_SANITY_SHARE:
                warning = (f"Only {len(seen)} exhibitors came back (was {active_before}); "
                           "not marking anyone as removed in case the site was having a bad moment.")
            else:
                for r in missing:
                    db.execute("UPDATE exhibitors SET removed_at = ? WHERE id = ?", (ts, r["id"]))
                removed = len(missing)

        db.execute(
            "UPDATE shows SET last_checked_at = ?, last_count = ?, last_error = ?, "
            "first_checked_at = COALESCE(first_checked_at, ?) WHERE id = ?",
            (ts, len(seen), warning, ts, show_id),
        )
        db.execute(
            "UPDATE runs SET finished_at = ?, found = ?, added = ?, removed = ?, error = ? WHERE id = ?",
            (ts, len(seen), len(added), removed, warning, run_id),
        )

    if added:
        _push_new(show, added)
        log_activity("new_exhibitors", f"{len(added)} new exhibitor{'s' if len(added) != 1 else ''} at {show['name']}",
                     f"show:{show_id}")
        _big_booth_alerts(show, added)
    if first_check:
        log_activity("show_added", f"Started tracking {show['name']}: {len(seen)} exhibitors", f"show:{show_id}")
    if show["platform"] == "mapyourshow" and not show["next_edition_url"]:
        _probe_next_edition(show, scraper)

    return {"found": len(seen), "added": len(added), "removed": removed, "first_check": first_check, "warning": warning}


def _big_booth_alerts(show: dict, added: list[dict]) -> None:
    from . import settings as cfg

    threshold = cfg.get("big_booth_alert_sqft")
    big = [a for a in added if a.get("booth_sqft") and a["booth_sqft"] >= threshold]
    if big:
        notify.big_booths(show, big)
        with connect() as db:
            for a in big:
                db.execute("UPDATE exhibitors SET alerted_at = ? WHERE id = ?", (now(), a["id"]))


def _finish_failed(show_id: int, run_id: int, msg: str) -> None:
    ts = now()
    with connect() as db:
        db.execute("UPDATE runs SET finished_at = ?, error = ? WHERE id = ?", (ts, msg, run_id))
        db.execute("UPDATE shows SET last_checked_at = ?, last_error = ? WHERE id = ?", (ts, msg, show_id))


def _push_new(show: dict, added: list[dict]) -> None:
    pushed_ids = notify.new_exhibitors(show, added)
    if pushed_ids:
        ts = now()
        with connect() as db:
            for eid in pushed_ids:
                db.execute("UPDATE exhibitors SET pushed_at = ? WHERE id = ?", (ts, eid))


def _bump_year(name: str) -> str:
    def bump(m):
        y = m.group(0)
        return str(int(y) + 1).zfill(len(y))
    out = re.sub(r"\b(20\d{2}|\d{2})\b(?!.*\b(20\d{2}|\d{2})\b)", bump, name)
    return out if out != name else f"{name} (next edition)"


def _probe_next_edition(show: dict, scraper) -> None:
    """Map Your Show hosts follow <show><yy>; when next year's directory goes live, start tracking it."""
    url = mys_next_edition_url(show["url"])
    if not url:
        return
    try:
        rows = scraper(url, "mapyourshow")
    except Exception:  # noqa: BLE001 - not live yet is the normal case
        return
    if not rows:
        return
    new_id = add_show(url, name=_bump_year(show["name"]), location=show.get("location") or "",
                      parent_show_id=show["id"])
    with connect() as db:
        db.execute("UPDATE shows SET next_edition_url = ? WHERE id = ?", (url, show["id"]))
    notify.next_edition_found(show, url, len(rows))
    log.info("Next edition of %s is live: %s (show %s)", show["name"], url, new_id)


# ---------------------------------------------------------------- scheduling
def due_shows() -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CHECK_INTERVAL_HOURS)).replace(microsecond=0).isoformat()
    with connect() as db:
        return db.all(
            "SELECT * FROM shows WHERE active = 1 AND (last_checked_at IS NULL OR last_checked_at < ?) "
            "ORDER BY last_checked_at IS NOT NULL, last_checked_at",
            (cutoff,),
        )


def check_due(pause_seconds: float = 3.0) -> int:
    shows = due_shows()
    for s in shows:
        log.info("Checking %s", s["name"])
        check_show(s["id"])
        time.sleep(pause_seconds)  # be polite to show sites
    return len(shows)


def start_scheduler(every_minutes: float = 15) -> threading.Thread:
    def loop():
        while True:
            try:
                check_due()
            except Exception:  # noqa: BLE001
                log.exception("scheduled check failed")
            time.sleep(every_minutes * 60)

    t = threading.Thread(target=loop, name="show-scheduler", daemon=True)
    t.start()
    return t
