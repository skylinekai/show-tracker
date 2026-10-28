"""The background loop: every 15 minutes it checks shows, researches exhibitors, sends due leads,
and (once a day / once a week) sends the briefing and looks for new shows."""
from __future__ import annotations

import html
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import ai, amplemarket, notify, opportunities, prospecting, tracker
from . import settings as cfg
from .db import connect, log_activity, now
from .scrapers import detect_platform

log = logging.getLogger("showtracker")
UK = ZoneInfo("Europe/London")
DISCOVERY_EVERY_DAYS = 7


def tick() -> None:
    for name, job in (("shows", tracker.check_due), ("industries", refresh_industries),
                      ("research", prospecting.research_due), ("web research", prospecting.web_research_due),
                      ("signals", lambda: opportunities.maybe_scan_signals(every_days=1)),
                      ("opportunities", prospecting.research_opportunities_due),
                      ("send", prospecting.send_due),
                      ("briefing", maybe_send_briefing),
                      ("discovery", maybe_discover)):
        try:
            job()
        except Exception:  # noqa: BLE001 - one failing job mustn't stop the others
            log.exception("job %s failed", name)


def refresh_industries(every_days: int = 7) -> int:
    """Look for more companies in each active industry campaign about once a week."""
    if not amplemarket.configured():
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=every_days)).replace(microsecond=0).isoformat()
    with connect() as db:
        due = db.all("SELECT id FROM industries WHERE prospecting = 1 AND (last_search_at IS NULL OR last_search_at < ?)",
                     (cutoff,))
    added = 0
    for row in due:
        try:
            added += prospecting.refresh_industry(row["id"])
        except amplemarket.AmplemarketError as exc:
            log.warning("industry search failed: %s", exc)
    return added


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
def maybe_send_briefing(force: bool = False) -> int:
    """Each salesperson gets their own briefing; managers' include a team summary.
    Extra addresses in Settings get the team version. Returns how many were sent."""
    local = datetime.now(UK)
    if not force and local.strftime("%H:%M") < _briefing_time():
        return 0
    if not notify.email_configured() or not cfg.get("email_alerts"):
        return 0
    day = local.date().isoformat()
    sent = 0
    with connect() as db:
        users = db.all("SELECT id, email, name, is_admin, mailbox, sequence_id, briefing FROM users")
    for u in users:
        key = f"_briefing_sent_{day}_{u['id']}"
        if cfg.internal(key) and not force:
            continue
        if not u["briefing"]:
            continue
        subject, body = build_briefing(u)
        if notify.send_email([u["email"]], subject, notify.wrap_email(subject, body)):
            cfg.set_internal(key, True)
            sent += 1
    extra = [t.strip() for t in str(cfg.get("briefing_to") or "").split(",") if t.strip()]
    extra = [t for t in extra if t.lower() not in {u["email"] for u in users}]
    key = f"_briefing_sent_{day}_team"
    if extra and (force or not cfg.internal(key)):
        subject, body = build_briefing(None)
        if notify.send_email(extra, subject, notify.wrap_email(subject, body)):
            cfg.set_internal(key, True)
            sent += 1
    return sent


def _briefing_time() -> str:
    raw = str(cfg.get("briefing_time") or "07:30").strip().replace(".", ":")
    try:
        h, m = (raw.split(":") + ["0"])[:2]
        return f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        return "07:30"


def briefing_data(user: dict | None = None) -> dict:
    """user=None: the whole team. Otherwise that salesperson's shows and outreach."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).replace(microsecond=0).isoformat()
    conf = cfg.all_settings()
    mine_l, mine_s, p = "", "", []
    if user:
        mine_l, mine_s, p = " AND l.owner_id = ?", " AND s.owner_id = ?", [user["id"]]
    with connect() as db:
        review = db.one("SELECT COUNT(*) AS n, COUNT(DISTINCT company_id) AS companies FROM leads l "
                        "WHERE status = 'review'" + mine_l, p)
        new_ex = db.all(
            "SELECT e.name, e.booth, e.booth_sqft, s.name AS show_name, s.id AS show_id FROM exhibitors e "
            "JOIN shows s ON s.id = e.show_id WHERE e.in_first_snapshot = 0 AND e.likely_junk = 0 "
            "AND e.first_seen_at >= ?" + mine_s + " ORDER BY COALESCE(e.booth_sqft, 0) DESC", [since] + p)
        sent = db.one("SELECT COUNT(*) AS n FROM leads l WHERE sent_at >= ?" + mine_l, [since] + p)["n"]
        replies = db.all(prospecting.LEAD_QUERY + " WHERE l.replied_at >= ?" + mine_l + " ORDER BY l.replied_at DESC",
                         [since] + p)
        shows = db.all("SELECT * FROM shows s WHERE active = 1" + mine_s, p)
        found = db.all("SELECT * FROM discovered_shows WHERE status = 'suggested' AND found_at >= ?", (since,))
        unassigned = db.one("SELECT COUNT(*) AS n FROM shows WHERE active = 1 AND owner_id IS NULL")["n"]
        broken = db.all("SELECT id, name, last_error FROM shows s WHERE active = 1 AND last_error IS NOT NULL" + mine_s, p)
        team = []
        if user is None or user.get("is_admin"):
            team = db.all(
                "SELECT u.id, COALESCE(u.name, u.email) AS who, "
                " SUM(CASE WHEN l.status = 'review' THEN 1 ELSE 0 END) AS review, "
                " SUM(CASE WHEN l.sent_at >= ? THEN 1 ELSE 0 END) AS sent, "
                " SUM(CASE WHEN l.replied_at >= ? THEN 1 ELSE 0 END) AS replied "
                "FROM users u LEFT JOIN leads l ON l.owner_id = u.id GROUP BY u.id, u.name, u.email ORDER BY who",
                (since, since))
    opening = []
    for s in shows:
        du = prospecting.days_until(s["start_date"])
        if du is None:
            continue
        opens_in = du - conf["window_start_days"]
        if 0 <= opens_in <= 14:
            opening.append((s, opens_in))
    setup = []
    if user is None or user.get("is_admin"):
        if not amplemarket.configured():
            setup.append("Amplemarket isn't connected (AMPLEMARKET_API_KEY).")
        if not conf["default_sequence_id"]:
            setup.append("No default Amplemarket sequence chosen in Settings.")
        if not ai.configured():
            setup.append("Claude isn't connected (ANTHROPIC_API_KEY), so emails have no personalised opener.")
        if not conf["pitch"]:
            setup.append("'What you offer' is empty in Settings; openers will be generic.")
    if user and not user.get("mailbox"):
        setup.append("Pick your Amplemarket mailbox under My details so emails go out from you.")
    return {"review": review, "new_ex": new_ex, "sent": sent, "replies": replies, "opening": opening,
            "found": found, "broken": broken, "setup": setup, "team": team, "unassigned": unassigned}


def build_briefing(user: dict | None = None) -> tuple[str, str]:
    d = briefing_data(user)
    e = html.escape
    link = notify._link
    parts = []
    first = ((user or {}).get("name") or "").split(" ")[0]
    if first:
        parts.append(f"<p>Morning {e(first)}, here's your day.</p>")
    n_review = d["review"]["n"] or 0
    if n_review:
        parts.append(f"<h3 style='font-weight:500'>Waiting for your approval</h3><p><b>{n_review}</b> contacts at "
                     f"<b>{d['review']['companies']}</b> companies are researched and written up.</p>"
                     + link("/outreach", "Review and approve"))
    if d["replies"]:
        items = "".join(f"<li><b>{e(r['contact_name'] or r['email'])}</b>, {e(r['company_name'])} "
                        f"({e(r['show_name'] or r['industry_name'] or 'outreach')})"
                        f"{' – ' + e(r['reply_label'].replace('_', ' ')) if r['reply_label'] else ''}"
                        f"{' · ' + e(r['owner_name']) if user is None and r['owner_name'] else ''}</li>" for r in d["replies"])
        parts.append(f"<h3 style='font-weight:500'>Replies</h3><ul>{items}</ul>")
    if d["new_ex"]:
        by_show: dict[str, list] = {}
        for x in d["new_ex"]:
            by_show.setdefault(x["show_name"], []).append(x)
        items = "".join(
            f"<li><b>{e(show)}</b>: " + ", ".join(
                e(x["name"]) + (f" ({x['booth_sqft']:,} sq ft)" if x["booth_sqft"] else "") for x in xs[:8])
            + (f" and {len(xs) - 8} more" if len(xs) > 8 else "") + "</li>" for show, xs in by_show.items())
        title = "New exhibitors at your shows" if user else "New exhibitors"
        parts.append(f"<h3 style='font-weight:500'>{title} ({len(d['new_ex'])})</h3><ul>{items}</ul>")
    if d["sent"]:
        parts.append(f"<p><b>{d['sent']}</b> contacts went into Amplemarket sequences.</p>")
    if d["opening"]:
        items = "".join(f"<li><b>{e(s['name'])}</b>: outreach window opens "
                        f"{'today' if days == 0 else f'in {days} days'}</li>" for s, days in d["opening"])
        parts.append(f"<h3 style='font-weight:500'>Coming into your window</h3><ul>{items}</ul>")
    if d["found"] or d["unassigned"]:
        items = "".join(f"<li><b>{e(f['name'])}</b>{' · ' + e(f['location']) if f['location'] else ''}"
                        f"{' · ' + _nice(f['start_date']) if f['start_date'] else ''}</li>" for f in d["found"])
        extra = (f"<p>{d['unassigned']} tracked show{'s are' if d['unassigned'] != 1 else ' is'} not assigned to anyone yet.</p>"
                 if d["unassigned"] else "")
        parts.append(f"<h3 style='font-weight:500'>Shows up for grabs</h3>{'<ul>' + items + '</ul>' if items else ''}{extra}"
                     + link("/discover", "Find shows"))
    if len(d["team"]) > 1:
        rows = "".join(f"<tr><td style='padding:4px 12px 4px 0'>{e(t['who'])}</td><td align='right'>{t['review'] or 0}</td>"
                       f"<td align='right'>{t['sent'] or 0}</td><td align='right'>{t['replied'] or 0}</td></tr>" for t in d["team"])
        parts.append("<h3 style='font-weight:500'>Team (last 24 hours)</h3><table style='font-size:14px;border-collapse:collapse'>"
                     "<tr style='color:#6a6987'><td></td><td style='padding:0 10px'>To approve</td><td style='padding:0 10px'>Sent</td>"
                     f"<td style='padding:0 10px'>Replies</td></tr>{rows}</table>")
    if d["broken"] or d["setup"]:
        items = "".join(f"<li>{e(s['name'])}: {e(s['last_error'])}</li>" for s in d["broken"])
        items += "".join(f"<li>{e(x)}</li>" for x in d["setup"])
        parts.append(f"<h3 style='font-weight:500'>Needs a look</h3><ul>{items}</ul>")
    if len(parts) <= (1 if first else 0):
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
    """Weekly: look for new shows across your discovery topics and every industry campaign."""
    conf = cfg.all_settings()
    if not ai.configured() or (not force and not conf["discovery_enabled"]):
        return 0
    last = cfg.internal("_last_discovery")
    if not force and last and (date.today() - date.fromisoformat(last)).days < DISCOVERY_EVERY_DAYS:
        return 0
    cfg.set_internal("_last_discovery", date.today().isoformat())
    added = 0
    topics = cfg.lines(conf["discovery_topics"])
    if topics:
        added += discover_for(topics, cfg.lines(conf["discovery_regions"]))
    with connect() as db:
        industries = db.all("SELECT * FROM industries WHERE prospecting = 1")
    for ind in industries:
        added += discover_for([ind["name"]] + cfg.lines(ind["keywords"])[:4], cfg.lines(conf["discovery_regions"]),
                              ind["id"])
    return added


def discover_for(topics: list[str], regions: list[str], industry_id: int | None = None) -> int:
    with connect() as db:
        known = [r["name"] for r in db.all("SELECT name FROM shows")] + \
                [r["name"] for r in db.all("SELECT name FROM discovered_shows")]
    shows = ai.discover_shows(topics, regions, known, date.today().isoformat())
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
                      "reason, found_at, industry_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (name, url, s.get("website"), detect_platform(url) if url else None, start,
                       s.get("location"), s.get("industry"), s.get("why"), now(), industry_id))
            added += 1
    if added:
        log_activity("discovery", f"Found {added} new show{'s' if added != 1 else ''} to consider")
    return added
