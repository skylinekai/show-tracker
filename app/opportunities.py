"""Opportunity signals: companies worth calling before anyone else does.

  first_time   exhibiting with no record at any other show we track (and not at the previous edition)
  upsized      a bigger stand than at their previous show or last year's edition, or grown since last check
  funding      raised money recently, in one of your industries (often precedes a step up in events)
  hiring       hiring events / trade show / field marketing people
  growing      headcount growing fast
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from . import amplemarket
from . import settings as cfg
from . import value as val
from .db import connect, log_activity, now
from .scrapers import name_key

log = logging.getLogger("showtracker")

KINDS = {
    "first_time": ("First-time exhibitors", "No record of exhibiting before"),
    "upsized": ("Bigger stands", "Taking more space than before"),
    "funding": ("Recently funded", "Raised money in the last 6 months"),
    "hiring": ("Hiring event staff", "Recruiting events / trade show people"),
    "growing": ("Growing fast", "Headcount up 20%+ in 6 months"),
}
UPSIZE_MIN_PCT = 25
UPSIZE_MIN_SQM = 9
EVENT_TITLES = ["events manager", "event manager", "head of events", "trade show", "tradeshow", "exhibitions manager",
                "exhibition manager", "field marketing", "event marketing"]


def series_key(name: str) -> str:
    """'NAB Show 2027' and 'NAB Show 2026' are the same series."""
    n = re.sub(r"\b(19|20)\d{2}\b|\b\d{2}\b", " ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", "", n)


def _upsert(db, kind: str, *, key: str, company: str, show_id=None, exhibitor_id=None, industry_id=None,
            owner_id=None, detail: str = "", area=None, prev=None, conf: dict) -> bool:
    if db.one("SELECT id FROM opportunities WHERE kind = ? AND name_key = ? AND COALESCE(show_id, 0) = ?",
              (kind, key, show_id or 0)):
        return False
    out = False
    if show_id:
        sh = db.one("SELECT name, location FROM shows WHERE id = ?", (show_id,))
        out = val.is_outsourced(sh, conf) if sh else False
    v = val.stand_value(area, conf, out)
    db.insert("INSERT INTO opportunities (kind, name_key, company_name, show_id, exhibitor_id, industry_id, owner_id, "
              "detail, sqm, prev_sqm, value_low, value_high, found_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (kind, key, company, show_id, exhibitor_id, industry_id, owner_id, detail[:400], area, prev,
               v[0] if v else None, v[1] if v else None, now()))
    return True


def prune_first_timers() -> int:
    """A 'first-timer' who turns up at another show later (or in web research) wasn't a first-timer."""
    with connect() as db:
        stale = db.all(
            "SELECT o.id FROM opportunities o WHERE o.kind = 'first_time' AND o.status = 'new' AND ("
            " EXISTS (SELECT 1 FROM exhibitors e WHERE e.name_key = o.name_key AND e.show_id <> o.show_id "
            "         AND e.likely_junk = 0) OR "
            " EXISTS (SELECT 1 FROM company_shows cs JOIN companies co ON co.id = cs.company_id "
            "         WHERE co.name_key = o.name_key))")
        for r in stale:
            db.execute("DELETE FROM opportunities WHERE id = ?", (r["id"],))
    return len(stale)


def scan_show(show_id: int) -> int:
    """First-timers and upsizers at one show. Runs after every check of that show."""
    prune_first_timers()
    conf = cfg.all_settings()
    today = date.today().isoformat()
    with connect() as db:
        show = db.one("SELECT * FROM shows WHERE id = ?", (show_id,))
        if not show or (show["start_date"] and show["start_date"] < today):
            return 0
        others = db.all("SELECT id, name, start_date FROM shows WHERE id <> ?", (show_id,))
        series = series_key(show["name"])
        prev_editions = [o for o in others if series_key(o["name"]) == series and (o["start_date"] or "") < (show["start_date"] or "9")]
        exhibitors = db.all("SELECT * FROM exhibitors WHERE show_id = ? AND removed_at IS NULL AND likely_junk = 0", (show_id,))
        elsewhere: dict[str, list] = {}
        for r in db.all("SELECT e.name_key, e.booth_sqft, s.id AS show_id, s.name, s.start_date FROM exhibitors e "
                        "JOIN shows s ON s.id = e.show_id WHERE e.show_id <> ? AND e.likely_junk = 0", (show_id,)):
            elsewhere.setdefault(r["name_key"], []).append(r)
        web = {r["name_key"] for r in db.all("SELECT DISTINCT co.name_key FROM company_shows cs "
                                             "JOIN companies co ON co.id = cs.company_id")}
        grown = {r["exhibitor_id"]: r for r in db.all(
            "SELECT h.exhibitor_id, MIN(h.old_sqft) AS old_sqft, MAX(h.new_sqft) AS new_sqft FROM stand_history h "
            "JOIN exhibitors e ON e.id = h.exhibitor_id WHERE e.show_id = ? GROUP BY h.exhibitor_id", (show_id,))}
        prev_ids = {p["id"] for p in prev_editions}
        found = 0
        outsourced = val.is_outsourced(show, conf)
        for e in exhibitors:
            k = e["name_key"]
            area = val.sqm(e["booth_sqft"])
            history = elsewhere.get(k, [])
            if area and val.mid(val.stand_value(area, conf, outsourced)) < conf["min_project_value"] * 0.8:
                continue  # too small to be worth a call
            # first-timers: nowhere else, and (if we have it) not at last year's edition either
            if not history and k not in web:
                detail = ("Not at the previous edition or any other show we track" if prev_ids
                          else "No record at any other show we track")
                found += _upsert(db, "first_time", key=k, company=e["name"], show_id=show_id, exhibitor_id=e["id"],
                                 owner_id=show["owner_id"], industry_id=show["industry_id"], detail=detail, area=area,
                                 conf=conf)
            # upsizers: compare with last year's edition first, else their most recent earlier stand
            if area:
                earlier = [h for h in history if h["booth_sqft"] and (h["start_date"] or "") < (show["start_date"] or "9")]
                last_year = [h for h in earlier if h["show_id"] in prev_ids]
                ref = (last_year or sorted(earlier, key=lambda h: h["start_date"] or ""))[-1:] or []
                if ref:
                    before = val.sqm(ref[0]["booth_sqft"])
                    if area - before >= UPSIZE_MIN_SQM and area >= before * (1 + UPSIZE_MIN_PCT / 100):
                        found += _upsert(db, "upsized", key=k, company=e["name"], show_id=show_id, exhibitor_id=e["id"],
                                         owner_id=show["owner_id"], industry_id=show["industry_id"],
                                         detail=f"{before:.0f} m² at {ref[0]['name']} → {area:.0f} m² here", area=area,
                                         prev=before, conf=conf)
                g = grown.get(e["id"])
                if g and g["old_sqft"] and g["new_sqft"] > g["old_sqft"]:
                    before = val.sqm(g["old_sqft"])
                    if area - before >= UPSIZE_MIN_SQM:
                        found += _upsert(db, "upsized", key=k, company=e["name"], show_id=show_id, exhibitor_id=e["id"],
                                         owner_id=show["owner_id"], industry_id=show["industry_id"],
                                         detail=f"Stand grown from {before:.0f} m² to {area:.0f} m² since we started tracking",
                                         area=area, prev=before, conf=conf)
    if found:
        log_activity("opportunity", f"{found} new opportunit{'ies' if found != 1 else 'y'} at {show['name']}",
                     f"show:{show_id}", show["owner_id"])
    return found


def scan_signals(max_per_kind: int = 50) -> int:
    """Funding, hiring and growth signals for each industry campaign (or your discovery topics). Weekly."""
    if not amplemarket.configured():
        return 0
    conf = cfg.all_settings()
    with connect() as db:
        campaigns = db.all("SELECT * FROM industries WHERE prospecting = 1")
    if not campaigns and cfg.lines(conf["discovery_topics"]):
        campaigns = [{"id": None, "owner_id": None, "keywords": conf["discovery_topics"], "am_industries": "",
                      "locations": "United Kingdom", "name": "your topics"}]
    found = 0
    for c in campaigns:
        base = {"industries": cfg.lines(c["am_industries"]), "keywords": cfg.lines(c["keywords"]),
                "locations": cfg.lines(c["locations"]) or ["United Kingdom"],
                "sizes": ["51-200 employees", "201-500 employees", "501-1000 employees", "1001-5000 employees",
                          "5001-10000 employees", "10001+ employees"]}
        searches = [
            ("funding", {"last_funding": {"range_months": 6}}, "Raised {stage} funding {date}"),
            ("hiring", {"job_openings": {"titles": EVENT_TITLES}}, "Hiring for events / trade shows"),
            ("growing", {"growth": {"growth_rates": [{"min": 20, "max": 10000}], "time_frame_in_months": 6}},
             "Headcount up 20%+ in 6 months"),
        ]
        for kind, extra, template in searches:
            try:
                results, _ = amplemarket.search_companies(**base, **extra, page_size=min(50, max_per_kind))
            except amplemarket.AmplemarketError as exc:
                log.warning("%s signal search failed: %s", kind, exc)
                continue
            remember = []
            with connect() as db:
                for comp in results:
                    name = (comp.get("name") or "").strip()
                    if not name:
                        continue
                    detail = template.format(stage=(comp.get("latest_funding_stage") or "").replace("_", " "),
                                             date=(comp.get("latest_funding_date") or "")[:7]).replace("  ", " ")
                    if c["name"] != "your topics":
                        detail += f" · {c['name']}"
                    if _upsert(db, kind, key=name_key(name), company=name, industry_id=c["id"],
                               owner_id=c["owner_id"], detail=detail, conf=conf):
                        found += 1
                        remember.append(comp)
            for comp in remember:  # outside the transaction above
                _remember_company(comp)
    if found:
        log_activity("opportunity", f"{found} companies showing buying signals (funding, hiring, growth)")
    return found


def _remember_company(comp: dict) -> None:
    from .prospecting import _domain, _get_company, save_profile

    company = _get_company(comp["name"], _domain(comp.get("website") or ""))
    save_profile(company["id"], comp)


def maybe_scan_signals(every_days: int = 7) -> int:
    last = cfg.internal("_last_signal_scan")
    if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)) < timedelta(days=every_days):
        return 0
    cfg.set_internal("_last_signal_scan", now())
    return scan_signals()


def rescan_all() -> int:
    with connect() as db:
        ids = [r["id"] for r in db.all("SELECT id FROM shows WHERE active = 1")]
    return sum(scan_show(i) for i in ids)


KIND_RANK = {"upsized": 0, "first_time": 1, "funding": 2, "hiring": 3, "growing": 4}
