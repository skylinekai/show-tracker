"""Client value: stand size (m²) × your build rate, per show and across every show a company exhibits at.

A company with a 9 m² booth at one show may take 100 m² at another, so qualification looks at the
whole account, not one booth.
"""
from __future__ import annotations

import re
from datetime import date

from . import settings as cfg
from .db import connect

SQFT_PER_SQM = 10.7639


def sqm(sqft) -> float | None:
    return round(sqft / SQFT_PER_SQM, 1) if sqft else None


def stand_value(area_sqm: float | None, conf: dict, outsourced: bool = False) -> tuple[int, int] | None:
    """(low, high) estimated stand budget in £. Outsourced work (e.g. North America) uses its own rate."""
    if not area_sqm:
        return None
    if outsourced:
        v = int(area_sqm * conf["rate_outsourced"])
        return v, v
    return int(area_sqm * conf["rate_low"]), int(area_sqm * conf["rate_high"])


def is_outsourced(show: dict, conf: dict | None = None) -> bool:
    conf = conf or cfg.all_settings()
    text = f"{show.get('location') or ''} {show.get('name') or ''}"
    return any(re.search(r"\b" + re.escape(p) + r"\b", text, re.I) for p in cfg.lines(conf["outsourced_regions"]))


def mid(v: tuple[int, int] | None) -> int:
    return (v[0] + v[1]) // 2 if v else 0


def money(n) -> str:
    if n is None:
        return ""
    n = int(n)
    if n >= 1_000_000:
        return f"£{n / 1_000_000:.1f}m".replace(".0m", "m")
    if n >= 10_000:
        return f"£{round(n / 1000):,}k"
    return f"£{n:,}"


def money_range(v: tuple[int, int] | None) -> str:
    if not v:
        return ""
    return money(v[0]) if money(v[0]) == money(v[1]) else f"{money(v[0])}–{money(v[1])}"


def is_priority(show: dict, conf: dict | None = None) -> bool:
    """London / Birmingham (or wherever you've listed) = in-house work, best margin."""
    conf = conf or cfg.all_settings()
    text = f"{show.get('location') or ''} {show.get('name') or ''}".lower()
    return any(re.search(r"\b" + re.escape(p.lower()) + r"\b", text) for p in cfg.lines(conf["priority_locations"]))


def gross_profit(value: int, outsourced: bool, conf: dict) -> int:
    pct = conf["gp_elsewhere_pct"] if outsourced else conf["gp_inhouse_pct"]
    return int(value * pct / 100)


def account(name_key: str, conf: dict | None = None, upcoming_only: bool = False) -> dict:
    """Every stand this company has, from tracked shows and from web research."""
    conf = conf or cfg.all_settings()
    today = date.today().isoformat()
    with connect() as db:
        tracked = db.all(
            "SELECT e.id AS exhibitor_id, e.booth, e.booth_sqft, e.booth_dims, e.hall, e.removed_at, s.id AS show_id, "
            "s.name AS show_name, s.start_date, s.location, s.owner_id FROM exhibitors e JOIN shows s ON s.id = e.show_id "
            "WHERE e.name_key = ? AND e.likely_junk = 0 ORDER BY s.start_date", (name_key,))
        company = db.one("SELECT * FROM companies WHERE name_key = ?", (name_key,))
        web = db.all("SELECT * FROM company_shows WHERE company_id = ? ORDER BY start_date",
                     (company["id"],)) if company else []
    stands = []
    seen = set()
    for t in tracked:
        a = sqm(t["booth_sqft"])
        stands.append({"source": "tracked", "show_id": t["show_id"], "show": t["show_name"], "date": t["start_date"],
                       "location": t["location"], "booth": t["booth"], "sqm": a, "dims": t["booth_dims"],
                       "removed": bool(t["removed_at"]), "exhibitor_id": t["exhibitor_id"]})
        seen.add(re.sub(r"\W+", "", t["show_name"].lower()))
    for w in web:
        if re.sub(r"\W+", "", (w["show_name"] or "").lower()) in seen:
            continue
        stands.append({"source": "web", "show_id": None, "show": w["show_name"], "date": w["start_date"],
                       "location": w["location"], "booth": None, "sqm": w["stand_sqm"], "dims": None,
                       "removed": False, "url": w["source_url"]})
    for s in stands:
        place = {"location": s["location"], "name": s["show"]}
        s["priority"] = is_priority(place, conf)
        s["outsourced"] = is_outsourced(place, conf)
        s["value"] = stand_value(s["sqm"], conf, s["outsourced"])
        s["gp"] = gross_profit(mid(s["value"]), s["outsourced"], conf) if s["value"] else None
        s["upcoming"] = not s["date"] or s["date"][:10] >= today
    live = [s for s in stands if not s["removed"] and (s["upcoming"] or not upcoming_only)]
    upcoming = [s for s in live if s["upcoming"]]
    sized = [s for s in live if s["sqm"]]
    biggest = max(sized, key=lambda s: s["sqm"]) if sized else None
    return {
        "stands": stands,
        "show_count": len({s["show"] for s in upcoming}),
        "biggest": biggest,
        "best_value": biggest["value"] if biggest else None,
        "annual_value": sum(mid(s["value"]) for s in upcoming if s["value"]),
        "annual_gp": sum(s["gp"] or 0 for s in upcoming),
        "any_priority": any(s["priority"] for s in upcoming),
        "company": company,
    }
