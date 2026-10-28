"""The background loop: every 15 minutes it checks shows, researches exhibitors, sends due leads,
and (once a day / once a week) sends the briefing and looks for new shows."""
from __future__ import annotations

import html
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import ai, amplemarket, notify, prospecting, tracker
from . import settings as cfg
from .db import connect, log_activity, now
from .scrapers import detect_platform

log = logging.getLogger("showtracker")
UK = ZoneInfo("Europe/London")
DISCOVERY_EVERY_DAYS = 7


def tick() -> None:
    for name, job in (("shows", tracker.check_due), ("research", prospecting.research_due),
                      ("send", prospecting.send_due), ("briefing", maybe_send_briefing),
                      ("discovery", maybe_discover)):
        try:
            job()
        except Exception:  # noqa: BLE001 - one failing job mustn't stop the others
            log.exception("job %s failed", name)


def start(every_minutes: float = 15) -> threading.Thread:
    def loop():
        time.sleep(20)  # let the web server come up first
        while True:
            tick()
            time.sleep(every_minutes * 60)

    t = threading.Thread(target=loop, name="jobs", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- daily briefing
def maybe_send_briefing(force: bool = False) -> bool:
    local = datetime.now(UK)
    key = f"_briefing_sent_{local.date().isoformat()}"
    if not force and (local.hour < cfg.get("briefing_hour") or cfg.internal(key)):
        return False
    to = [t for t in str(cfg.get("briefing_to") or "").split(",") if t.strip()]
    if not to or not notify.email_configured():
        return False
    subject, body = build_briefing()
    ok = notify.send_email(to, subject, notify.wrap_email(subject, body))
    if ok:
        cfg.set_internal(key, True)
    return ok


def briefing_data() -> dict:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).replace(microsecond=0).isoformat()
    conf = cfg.all_settings()
    with connect() as db:
        review = db.one("SELECT COUNT(*) AS n, COUNT(DISTINCT company_id) AS companies FROM leads WHERE status = 'review'")
        new_ex = db.all(
            "SELECT e.name, e.booth, e.booth_sqft, s.name AS show_name, s.id AS show_id FROM exhibitors e "
            "JOIN shows s ON s.id = e.show_id WHERE e.in_first_snapshot = 0 AND e.likely_junk = 0 "
            "AND e.first_seen_at >= ? ORDER BY COALESCE(e.booth_sqft, 0) DESC", (since,))
        sent = db.one("SELECT COUNT(*) AS n FROM leads WHERE sent_at >= ?", (since,))["n"]
        replies = db.all(prospecting.LEAD_QUERY + " WHERE l.replied_at >= ? ORDER BY l.replied_at DESC", (since,))
        shows = db.all("SELECT * FROM shows WHERE active = 1")
        found = db.all("SELECT * FROM discovered_shows WHERE status = 'suggested' AND found_at >= ?", (since,))
        broken = db.all("SELECT id, name, last_error FROM shows WHERE active = 1 AND last_error IS NOT NULL")
    opening = []
    for s in shows:
        du = prospecting.days_until(s["start_date"])
        if du is None:
            continue
        opens_in = du - conf["window_start_days"]
        if 0 <= opens_in <= 14:
            opening.append((s, opens_in))
    setup = []
    if not amplemarket.configured():
        setup.append("Amplemarket isn't connected (AMPLEMARKET_API_KEY).")
    if not conf["default_sequence_id"]:
        setup.append("No default Amplemarket sequence chosen in Settings.")
    if not ai.configured():
        setup.append("Claude isn't connected (ANTHROPIC_API_KEY), so emails have no personalised opener.")
    if not conf["pitch"]:
        setup.append("'What you offer' is empty in Settings; openers will be generic.")
    return {"review": review, "new_ex": new_ex, "sent": sent, "replies": replies, "opening": opening,
            "found": found, "broken": broken, "setup": setup}


def build_briefing() -> tuple[str, str]:
    d = briefing_data()
    e = html.escape
    link = notify._link
    parts = []
    n_review = d["review"]["n"] or 0
    if n_review:
        parts.append(f"<h3 style='font-weight:500'>Waiting for your approval</h3><p><b>{n_review}</b> contacts at "
                     f"<b>{d['review']['companies']}</b> companies are researched and written up.</p>"
                     + link("/outreach", "Review and approve"))
    if d["replies"]:
        items = "".join(f"<li><b>{e(r['contact_name'] or r['email'])}</b>, {e(r['company_name'])} ({e(r['show_name'])})"
                        f"{' – ' + e(r['reply_label'].replace('_', ' ')) if r['reply_label'] else ''}</li>" for r in d["replies"])
        parts.append(f"<h3 style='font-weight:500'>Replies</h3><ul>{items}</ul>")
    if d["new_ex"]:
        by_show: dict[str, list] = {}
        for x in d["new_ex"]:
            by_show.setdefault(x["show_name"], []).append(x)
        items = "".join(
            f"<li><b>{e(show)}</b>: " + ", ".join(
                e(x["name"]) + (f" ({x['booth_sqft']:,} sq ft)" if x["booth_sqft"] else "") for x in xs[:8])
            + (f" and {len(xs) - 8} more" if len(xs) > 8 else "") + "</li>" for show, xs in by_show.items())
        parts.append(f"<h3 style='font-weight:500'>New exhibitors ({len(d['new_ex'])})</h3><ul>{items}</ul>")
    if d["sent"]:
        parts.append(f"<p><b>{d['sent']}</b> contacts went into Amplemarket sequences.</p>")
    if d["opening"]:
        items = "".join(f"<li><b>{e(s['name'])}</b>: outreach window opens "
                        f"{'today' if days == 0 else f'in {days} days'}</li>" for s, days in d["opening"])
        parts.append(f"<h3 style='font-weight:500'>Coming into your window</h3><ul>{items}</ul>")
    if d["found"]:
        items = "".join(f"<li><b>{e(f['name'])}</b>{' · ' + e(f['location']) if f['location'] else ''}"
                        f"{' · ' + _nice(f['start_date']) if f['start_date'] else ''}</li>" for f in d["found"])
        parts.append(f"<h3 style='font-weight:500'>New shows found</h3><ul>{items}</ul>" + link("/discover", "Review shows"))
    if d["broken"] or d["setup"]:
        items = "".join(f"<li>{e(s['name'])}: {e(s['last_error'])}</li>" for s in d["broken"])
        items += "".join(f"<li>{e(x)}</li>" for x in d["setup"])
        parts.append(f"<h3 style='font-weight:500'>Needs a look</h3><ul>{items}</ul>")
    if not parts:
        parts.append("<p>A quiet day: no new exhibitors, replies or approvals.</p>")
    headline = []
    if n_review:
        headline.append(f"{n_review} to approve")
    if d["replies"]:
        headline.append(f"{len(d['replies'])} repl{'ies' if len(d['replies']) != 1 else 'y'}")
    if d["new_ex"]:
        headline.append(f"{len(d['new_ex'])} new exhibitors")
    subject = "Daily briefing" + (": " + ", ".join(headline) if headline else "")
    return subject, "".join(parts)


def _nice(d: str) -> str:
    try:
        return date.fromisoformat(d[:10]).strftime("%-d %b %Y")
    except ValueError:
        return html.escape(d)


# ---------------------------------------------------------------- show discovery
def maybe_discover(force: bool = False) -> int:
    conf = cfg.all_settings()
    if not ai.configured() or (not force and not conf["discovery_enabled"]):
        return 0
    last = cfg.internal("_last_discovery")
    if not force and last and (date.today() - date.fromisoformat(last)).days < DISCOVERY_EVERY_DAYS:
        return 0
    cfg.set_internal("_last_discovery", date.today().isoformat())
    with connect() as db:
        known = [r["name"] for r in db.all("SELECT name FROM shows")] + \
                [r["name"] for r in db.all("SELECT name FROM discovered_shows")]
    shows = ai.discover_shows(cfg.lines(conf["discovery_topics"]), cfg.lines(conf["discovery_regions"]),
                              known, date.today().isoformat())
    added = 0
    with connect() as db:
        for s in shows:
            name = str(s["name"]).strip()[:200]
            if db.one("SELECT id FROM discovered_shows WHERE LOWER(name) = ?", (name.lower(),)) or \
                    db.one("SELECT id FROM shows WHERE LOWER(name) = ?", (name.lower(),)):
                continue
            url = (s.get("exhibitor_list_url") or "").strip() or None
            start = str(s.get("start_date") or "")[:10] or None
            db.insert("INSERT INTO discovered_shows (name, url, website, platform, start_date, location, industry, "
                      "reason, found_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (name, url, s.get("website"), detect_platform(url) if url else None, start,
                       s.get("location"), s.get("industry"), s.get("why"), now()))
            added += 1
    if added:
        log_activity("discovery", f"Found {added} new show{'s' if added != 1 else ''} to consider")
    return added
