"""Finding, qualifying and contacting the right companies. Two routes in, one pipeline:

  Show route      every exhibitor at a show with prospecting switched on
  Industry route  every company Amplemarket finds for an industry campaign (e.g. Dental), at a show or not

For each company:
  1. Score the whole account on what we know for free: its biggest stand at any show (m² × your rate,
     so a 9 m² booth here still counts if they take 100 m² elsewhere), how many shows they do, whether the
     work is in London/Birmingham (in-house, best margin) and timing. Poor fits are skipped before any
     Amplemarket credits are spent.
  2. Research the best ones up to a daily limit: decision-makers (people search, email reveal + validation)
     and the company profile (revenue, headcount, headcount growth, funding, open marketing/events roles).
  3. Final score; Claude writes a subject and opening line per person.
  4. Leads wait in their owner's approval queue (or skip it when set to send automatically).
  5. Approved leads go into Amplemarket from the owner's own mailbox, timed to the show's outreach window.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from . import ai, amplemarket, notify
from . import settings as cfg
from . import value as val
from .db import connect, log_activity, now
from .scrapers import name_key

log = logging.getLogger("showtracker")

RESEARCH_REUSE_DAYS = 120   # a company researched once is reused for every show/industry within this time
RECONTACT_GAP_DAYS = 90     # never queue the same person twice within this time


# ---------------------------------------------------------------- helpers
def _today() -> date:
    return datetime.now(timezone.utc).date()


def days_until(d: str | None) -> int | None:
    if not d:
        return None
    try:
        return (date.fromisoformat(d[:10]) - _today()).days
    except ValueError:
        return None


def send_date(show: dict | None, conf: dict) -> str:
    """Earliest date to start the sequence: today, or when the show enters the outreach window."""
    du = days_until((show or {}).get("start_date"))
    if du is None:
        return _today().isoformat()
    return (_today() + timedelta(days=max(0, du - conf["window_start_days"]))).isoformat()


def _contains_any(text: str, words: list[str]) -> str | None:
    t = (text or "").lower()
    return next((w for w in words if w.lower() in t), None)


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    if not url:
        return ""
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _since_minutes(m: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=m)).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- scoring
def pre_score(ex: dict, show: dict | None, conf: dict, acct: dict | None = None) -> tuple[int, list[str], str | None]:
    """Account score from free information (out of 75). Returns (score, notes, reason_to_skip)."""
    name = ex["name"]
    hit = _contains_any(name, cfg.lines(conf["exclude_keywords"]))
    if hit:
        return 0, [f"name contains '{hit}'"], f"Name contains '{hit}'"
    du = days_until((show or {}).get("start_date"))
    if show and du is not None and du < conf["window_end_days"]:
        return 0, ["show too close"], "Show is too close to start a stand project"
    acct = acct or val.account(name_key(name), conf)
    notes: list[str] = []
    score = 0

    # stand value (40): biggest stand anywhere, not just this show
    best = acct["biggest"]
    floor = conf["min_project_value"]
    if best and best["value"]:
        best_mid = val.mid(best["value"])
        if best_mid < floor * 0.5:
            return 0, [f"biggest stand {best['sqm']:.0f} m²"], \
                f"Biggest known stand is {best['sqm']:.0f} m² (≈{val.money(best_mid)}), well under {val.money(floor)}"
        pts = 40 if best_mid >= floor else round(40 * best_mid / floor)
        where = "" if show and best.get("show_id") == show.get("id") else f" at {best['show']}"
        notes.append(f"biggest stand {best['sqm']:.0f} m²{where} ≈ {val.money_range(best['value'])} (+{pts})")
        score += pts
    else:
        score += 20
        notes.append("stand size unknown (+20)")

    # breadth (10): companies doing several shows a year are worth more over time
    n = acct["show_count"]
    pts = 10 if n >= 3 else 5 if n == 2 else 0
    if pts:
        notes.append(f"{n} upcoming shows (+{pts})")
        score += pts

    # location (15): London / Birmingham work is in-house and most profitable
    if show:
        here = val.is_priority(show, conf)
        pts = 15 if here else 5
        notes.append(("in-house location" if here else "outside priority locations") + f" (+{pts})")
    else:  # industry route: their upcoming shows, else where they're based
        hq = ex.get("location") or ""
        if acct["any_priority"] or val.is_priority({"location": hq}, conf):
            pts = 15
            notes.append(f"London/Birmingham {'shows' if acct['any_priority'] else 'based'} (+15)")
        elif any(w in hq.lower() for w in ("united kingdom", "england", "scotland", "wales", ", uk")):
            pts = 10
            notes.append("UK based (+10)")
        else:
            pts = 5
    score += pts

    # timing (10)
    if not show:
        score += 10  # no show deadline to work around
    elif du is None:
        score += 7
    elif du <= conf["window_start_days"]:
        score += 10
        notes.append("inside outreach window (+10)")
    else:
        score += 7
        notes.append(f"show in {du} days, will send when the window opens (+7)")
    return score, notes, None


def company_points(company: dict, conf: dict) -> tuple[int, list[str], str | None]:
    """Company strength (out of 25): size, revenue, growth, funding, hiring."""
    ind = company.get("industry") or ""
    hit = _contains_any(ind, cfg.lines(conf["exclude_industries"]))
    if hit:
        return 0, [f"industry {ind}"], f"Industry excluded ({ind})"
    notes, pts = [], 0
    emp = company.get("employees")
    if emp is not None and emp < conf["min_employees"]:
        return 0, [f"{emp} employees"], f"Only {emp} employees"
    p = 6 if emp is None else 12 if emp >= 200 else 8 if emp >= 50 else 5
    pts += p
    notes.append(f"{emp:,} employees (+{p})" if emp is not None else "size unknown (+6)")
    rev = company.get("revenue_usd")
    p = 2 if not rev else 6 if rev >= 100_000_000 else 4 if rev >= 10_000_000 else 2
    pts += p
    if rev:
        notes.append(f"revenue ${rev / 1e6:,.0f}m (+{p})")
    if company.get("growing") == "growing":
        pts += 3
        notes.append("headcount growing (+3)")
    if company.get("marketing_roles"):
        pts += 2
        notes.append("hiring in marketing/events (+2)")
    fd = company.get("funding_date")
    if fd and days_until(fd) is not None and days_until(fd) > -548:
        pts += 2
        notes.append(f"raised funding {fd[:7]} (+2)")
    return min(pts, 25), notes, None


# ---------------------------------------------------------------- company research
def _get_company(name: str, domain: str = "") -> dict:
    key = name_key(name)
    with connect() as db:
        company = db.one("SELECT * FROM companies WHERE name_key = ?", (key,))
        if not company:
            cid = db.insert("INSERT INTO companies (name, name_key, domain, status) VALUES (?, ?, ?, 'pending')",
                            (name, key, domain or None))
            company = db.one("SELECT * FROM companies WHERE id = ?", (cid,))
    return company


def save_profile(company_id: int, comp: dict) -> None:
    """Store what Amplemarket knows about a company."""
    if not comp:
        return
    loc = comp.get("location")
    with connect() as db:
        db.execute(
            "UPDATE companies SET domain = COALESCE(?, domain), linkedin_url = COALESCE(?, linkedin_url), "
            "industry = COALESCE(?, industry), size = COALESCE(?, size), employees = COALESCE(?, employees), "
            "revenue = COALESCE(?, revenue), revenue_usd = COALESCE(?, revenue_usd), location = COALESCE(?, location), "
            "total_funding = COALESCE(?, total_funding), funding_stage = COALESCE(?, funding_stage), "
            "funding_date = COALESCE(?, funding_date), founded_year = COALESCE(?, founded_year), "
            "overview = COALESCE(?, overview), am_company_id = COALESCE(?, am_company_id) WHERE id = ?",
            (_domain(comp.get("website") or "") or None, comp.get("linkedin_url"), comp.get("industry"),
             comp.get("size"), comp.get("estimated_number_of_employees"), comp.get("estimated_revenue"),
             comp.get("revenue") if isinstance(comp.get("revenue"), int) else None,
             loc if isinstance(loc, str) else None,
             str(comp["total_funding"]) if comp.get("total_funding") else None,
             comp.get("latest_funding_stage"), (comp.get("latest_funding_date") or "")[:10] or None,
             comp.get("founded_year"), (comp.get("overview") or "")[:1200] or None, comp.get("id"), company_id))


def _enrich(company: dict) -> None:
    """Full profile, growth and hiring. Each part is optional; failures just leave gaps."""
    domain = company.get("domain")
    if not domain:
        return
    try:
        save_profile(company["id"], amplemarket.find_company(domain=domain))
    except amplemarket.AmplemarketError as exc:
        log.info("company profile failed for %s: %s", domain, exc)
    growing = amplemarket.headcount_growth(domain)
    try:
        roles, marketing = amplemarket.job_openings(domain)
    except amplemarket.AmplemarketError:
        roles, marketing = None, []
    with connect() as db:
        db.execute("UPDATE companies SET growing = COALESCE(?, growing), open_roles = COALESCE(?, open_roles), "
                   "marketing_roles = ? WHERE id = ?",
                   (growing, roles, json.dumps(marketing) if marketing else None, company["id"]))


def _rank_people(people: list[dict], titles: list[str]) -> list[dict]:
    def rank(p):
        t = (p.get("title") or "").lower()
        idx = next((i for i, want in enumerate(titles) if want.lower() in t), len(titles))
        return (idx, 0 if p.get("email") else 1)
    return sorted(people, key=rank)


def _matches_company(person: dict, key: str) -> bool:
    comp = name_key((person.get("company") or {}).get("name") or "")
    return bool(comp) and (comp == key or comp.startswith(key + " ") or key.startswith(comp + " "))


def _same_company(person: dict, key: str, domain: str) -> bool:
    """Search results occasionally include people from other firms; keep only this company's."""
    site = _domain((person.get("company") or {}).get("website") or "")
    if domain and site:
        return site == domain or site.endswith("." + domain) or domain.endswith("." + site)
    return _matches_company(person, key)


def research_company(name: str, website: str, conf: dict) -> tuple[dict, list[dict]]:
    """Find (or reuse) the company, its profile and its best contacts with usable emails."""
    company = _get_company(name, _domain(website))
    key = company["name_key"]
    fresh = company["researched_at"] and (
        datetime.now(timezone.utc) - datetime.fromisoformat(company["researched_at"])).days < RESEARCH_REUSE_DAYS
    if fresh:
        with connect() as db:
            contacts = db.all("SELECT * FROM contacts WHERE company_id = ? AND email IS NOT NULL", (company["id"],))
        return company, [c for c in contacts if _email_ok(c["email_status"], conf)]

    titles = cfg.lines(conf["target_titles"])
    seniorities = cfg.lines(conf["target_seniorities"])
    domain = company["domain"] or _domain(website)
    people = amplemarket.search_people(company_domain=domain, company_name="" if domain else name,
                                       titles=titles, seniorities=seniorities, page_size=25)
    people = [p for p in people if _same_company(p, key, domain)]
    if not people:  # broaden: anyone senior in marketing/events
        people = amplemarket.search_people(company_domain=domain, company_name="" if domain else name,
                                           seniorities=seniorities, page_size=25)
        people = [p for p in people if _same_company(p, key, domain)]
        people = [p for p in people if _contains_any(p.get("title") or "",
                                                     ["market", "event", "brand", "exhibit", "trade show", "communication"])]
    if people:
        save_profile(company["id"], people[0].get("company") or {})
        with connect() as db:
            company = db.one("SELECT * FROM companies WHERE id = ?", (company["id"],))
        _enrich(company)

    chosen = _rank_people(people, titles)[: conf["contacts_per_company"]]
    for p in chosen:
        if not p.get("email"):
            try:
                p["email"] = amplemarket.reveal_email(p).get("email") or None
            except amplemarket.AmplemarketError as exc:
                log.info("email reveal failed for %s: %s", p.get("name"), exc)
    statuses = amplemarket.validate_emails([p["email"] for p in chosen if p.get("email")])

    ts = now()
    with connect() as db:
        db.execute("UPDATE companies SET status = ?, researched_at = ? WHERE id = ?",
                   ("found" if people else "not_found", ts, company["id"]))
        saved = []
        for p in chosen:
            email = (p.get("email") or "").lower() or None
            link = p.get("linkedin_url") or f"am:{p.get('id')}"
            existing = db.one("SELECT id FROM contacts WHERE company_id = ? AND linkedin_url = ?", (company["id"], link))
            values = (p.get("id"), p.get("name"), p.get("first_name"), p.get("last_name"), p.get("title"),
                      email, statuses.get(email) if email else None,
                      p.get("location") if isinstance(p.get("location"), str) else None)
            if existing:
                db.execute("UPDATE contacts SET am_person_id = ?, name = ?, first_name = ?, last_name = ?, title = ?, "
                           "email = ?, email_status = ?, location = ? WHERE id = ?", values + (existing["id"],))
                cid = existing["id"]
            else:
                cid = db.insert("INSERT INTO contacts (am_person_id, name, first_name, last_name, title, email, "
                                "email_status, location, company_id, linkedin_url, created_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values + (company["id"], link, ts))
            saved.append(db.one("SELECT * FROM contacts WHERE id = ?", (cid,)))
        company = db.one("SELECT * FROM companies WHERE id = ?", (company["id"],))
    _count_research()
    return company, [c for c in saved if c["email"] and _email_ok(c["email_status"], conf)]


def _email_ok(status: str | None, conf: dict) -> bool:
    return status == "deliverable" or (conf["accept_risky_emails"] and status == "risky")


def _research_key() -> str:
    return f"_research_count_{_today().isoformat()}"


def research_done_today() -> int:
    return int(cfg.internal(_research_key(), 0) or 0)


def _count_research() -> None:
    cfg.set_internal(_research_key(), research_done_today() + 1)


# ---------------------------------------------------------------- one company through the pipeline
def _set_stage(ex_id: int | None, stage: str, score: int | None = None, notes: str | None = None, company_id=None,
               ic_id: int | None = None) -> None:
    with connect() as db:
        if ex_id:
            db.execute("UPDATE exhibitors SET stage = ?, score = COALESCE(?, score), score_notes = COALESCE(?, score_notes), "
                       "company_id = COALESCE(?, company_id) WHERE id = ?", (stage, score, notes, company_id, ex_id))
        if ic_id:
            db.execute("UPDATE industry_companies SET stage = ?, score = COALESCE(?, score), "
                       "score_notes = COALESCE(?, score_notes) WHERE id = ?", (stage, score, notes, ic_id))


def teammate_on(key: str, owner_id) -> dict | None:
    """Is another salesperson already working this company? (open or recent outreach)"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECONTACT_GAP_DAYS)).isoformat()
    with connect() as db:
        return db.one(
            "SELECT l.owner_id, u.name, u.email, s.name AS show_name, i.name AS industry_name FROM leads l "
            "JOIN companies co ON co.id = l.company_id LEFT JOIN users u ON u.id = l.owner_id "
            "LEFT JOIN shows s ON s.id = l.show_id LEFT JOIN industries i ON i.id = l.industry_id "
            "WHERE co.name_key = ? AND l.owner_id IS NOT NULL AND (? IS NULL OR l.owner_id <> ?) "
            "AND (l.status IN ('review', 'approved') OR (l.status IN ('sent', 'replied') AND l.sent_at >= ?)) "
            "ORDER BY l.created_at DESC LIMIT 1", (key, owner_id, owner_id, cutoff))


def _qualify(name: str, website: str, show: dict | None, conf: dict, ex_id=None, ic_id=None, hq: str = "",
             owner_id=None, force: bool = False):
    """Shared by both routes. Returns (stage, company, contacts, score, notes)."""
    mate = teammate_on(name_key(name), owner_id)
    if mate:
        via = mate["show_name"] or mate["industry_name"] or "their outreach"
        note = f"With {mate['name'] or mate['email']} ({via})"
        _set_stage(ex_id, "teammate", None, note, ic_id=ic_id)
        return "teammate", None, [], 0, note
    acct = val.account(name_key(name), conf)
    score, notes, skip = pre_score({"name": name, "location": hq}, show, conf, acct)
    if skip:
        _set_stage(ex_id, "skipped", score, skip, ic_id=ic_id)
        return "skipped", None, [], score, skip
    try:
        company, contacts = research_company(name, website, conf)
    except amplemarket.AmplemarketError as exc:
        _set_stage(ex_id, "error", score, str(exc)[:300], ic_id=ic_id)
        raise
    pts, more, skip = company_points(company, conf)
    score, notes = score + pts, notes + more
    note_text = "; ".join(notes)
    if skip:
        _set_stage(ex_id, "skipped", score, skip, company["id"], ic_id)
        return "skipped", company, [], score, skip
    if company["status"] == "not_found":
        _set_stage(ex_id, "not_found", score, "Amplemarket couldn't match this company", company["id"], ic_id)
        return "not_found", company, [], score, note_text
    if not force and score < (conf["min_score_industry"] if ic_id else conf["min_score"]):
        _set_stage(ex_id, "low_score", score, note_text, company["id"], ic_id)
        return "low_score", company, [], score, note_text
    found = contacts
    contacts = _not_recently_contacted(contacts)
    if not contacts:
        if found:
            _set_stage(ex_id, "in_outreach", score, "Already in outreach (another show or campaign)", company["id"], ic_id)
            return "in_outreach", company, [], score, note_text
        _set_stage(ex_id, "no_contacts", score, "No decision-makers with a checked email", company["id"], ic_id)
        return "no_contacts", company, [], score, note_text
    return "ok", company, contacts, score, note_text


def _create_leads(*, show: dict | None, ex: dict | None, industry: dict | None, company: dict, contacts: list[dict],
                  score: int, note_text: str, conf: dict, owner_id, auto_send: bool, sequence: str | None) -> int:
    acct = val.account(company["name_key"], conf)
    copy, copy_error = {}, None
    if ai.configured():
        try:
            copy = ai.write_openers(conf, show, ex or {"name": company["name"]}, company, contacts,
                                    stands=acct["stands"], industry=(industry or {}).get("name"))
        except ai.AIError as exc:
            copy_error = str(exc)
    with connect() as db:
        owner = db.one("SELECT id, sequence_id FROM users WHERE id = ?", (owner_id,)) if owner_id else None
    sequence = sequence or (owner or {}).get("sequence_id") or conf["default_sequence_id"] or None
    status = "approved" if auto_send else "review"
    ts = now()
    made = 0
    with connect() as db:
        for c in contacts:
            if show and db.one("SELECT id FROM leads WHERE show_id = ? AND contact_id = ?", (show["id"], c["id"])):
                continue
            if not show and db.one("SELECT id FROM leads WHERE contact_id = ? AND industry_id = ?",
                                   (c["id"], (industry or {}).get("id"))):
                continue
            line = copy.get(c["id"], {})
            db.insert(
                "INSERT INTO leads (exhibitor_id, show_id, industry_id, company_id, contact_id, owner_id, status, score, "
                "score_notes, subject, opener, sequence_id, send_after, created_at, approved_at, approved_by, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ((ex or {}).get("id"), (show or {}).get("id"), (industry or {}).get("id"), company["id"], c["id"],
                 owner_id, status, score, note_text, line.get("subject"), line.get("opener"), sequence,
                 send_date(show, conf), ts, ts if status == "approved" else None,
                 "auto" if status == "approved" else None, copy_error))
            made += 1
    return made


def process_exhibitor(ex: dict, show: dict, conf: dict) -> str:
    """Show route: one exhibitor through the pipeline. Returns the stage it ended in."""
    stage, company, contacts, score, note_text = _qualify(ex["name"], ex.get("website") or "", show, conf, ex_id=ex["id"],
                                                          owner_id=show.get("owner_id"))
    if stage != "ok":
        return stage
    industry = None
    if show.get("industry_id"):
        with connect() as db:
            industry = db.one("SELECT * FROM industries WHERE id = ?", (show["industry_id"],))
    # New exhibitors found by the daily check go straight into the sequence (once the show's first batch
    # has been reviewed); the first batch waits for approval so you can check the quality.
    auto = bool(show.get("auto_send")) or (bool(show.get("auto_send_new")) and not ex.get("in_first_snapshot")
                                           and _first_batch_reviewed(show["id"]))
    _create_leads(show=show, ex=ex, industry=industry, company=company, contacts=contacts, score=score,
                  note_text=note_text, conf=conf, owner_id=show.get("owner_id"), auto_send=auto,
                  sequence=show.get("sequence_id"))
    _set_stage(ex["id"], "queued", score, note_text, company["id"])
    return "queued"


def _first_batch_reviewed(show_id: int) -> bool:
    with connect() as db:
        return bool(db.one("SELECT id FROM leads WHERE show_id = ? AND status IN ('approved', 'sent', 'replied') "
                           "AND approved_by IS NOT NULL AND approved_by <> 'auto' LIMIT 1", (show_id,)))


def process_industry_company(ic: dict, industry: dict, conf: dict) -> str:
    """Industry route. If the company is exhibiting at an upcoming show we track, the outreach mentions it."""
    with connect() as db:
        company = db.one("SELECT * FROM companies WHERE id = ?", (ic["company_id"],))
        next_stand = db.one(
            "SELECT e.*, s.id AS sid FROM exhibitors e JOIN shows s ON s.id = e.show_id WHERE e.name_key = ? "
            "AND e.removed_at IS NULL AND (s.start_date IS NULL OR s.start_date >= ?) ORDER BY s.start_date LIMIT 1",
            (company["name_key"], _today().isoformat()))
        show = db.one("SELECT * FROM shows WHERE id = ?", (next_stand["sid"],)) if next_stand else None
    if show and days_until(show.get("start_date")) is not None and days_until(show["start_date"]) < conf["window_end_days"]:
        show, next_stand = None, None  # too close to pitch that show; pitch the account instead
    stage, company, contacts, score, note_text = _qualify(company["name"], company["domain"] or "", show, conf,
                                                          ic_id=ic["id"], hq=company.get("location") or "",
                                                          owner_id=industry.get("owner_id"))
    if stage != "ok":
        return stage
    _create_leads(show=show, ex=next_stand, industry=industry, company=company, contacts=contacts, score=score,
                  note_text=note_text, conf=conf, owner_id=industry.get("owner_id"),
                  auto_send=bool(industry.get("auto_send")), sequence=industry.get("sequence_id"))
    _set_stage(next_stand["id"] if next_stand else None, "queued", score, note_text, company["id"], ic["id"])
    return "queued"


def _not_recently_contacted(contacts: list[dict]) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECONTACT_GAP_DAYS)).isoformat()
    with connect() as db:
        recent = {r["email"] for r in db.all(
            "SELECT c.email FROM leads l JOIN contacts c ON c.id = l.contact_id "
            "WHERE l.status IN ('review', 'approved', 'sent', 'replied') "
            "AND COALESCE(l.sent_at, l.approved_at, l.created_at) >= ?", (cutoff,))}
    return [c for c in contacts if c["email"] not in recent]


# ---------------------------------------------------------------- batch jobs
def candidates(limit: int, show_id: int | None = None) -> list[tuple[dict, dict]]:
    """Show-route exhibitors still to research, best first: London/Birmingham, then soonest, then biggest."""
    conf = cfg.all_settings()
    with connect() as db:
        q = ("SELECT e.*, s.start_date AS _show_date FROM exhibitors e JOIN shows s ON s.id = e.show_id "
             "WHERE s.prospecting = 1 AND s.active = 1 AND e.likely_junk = 0 AND e.removed_at IS NULL "
             "AND e.stage IS NULL")
        params: list = []
        if show_id:
            q += " AND s.id = ?"
            params.append(show_id)
        rows = db.all(q, params)
        shows = {s["id"]: s for s in db.all("SELECT * FROM shows WHERE prospecting = 1")}
    priority = {sid: val.is_priority(s, conf) for sid, s in shows.items()}
    rows.sort(key=lambda e: (not priority.get(e["show_id"]),
                             days_until(e["_show_date"]) if days_until(e["_show_date"]) is not None else 9999,
                             -(e["booth_sqft"] or 0)))
    return [(e, shows[e["show_id"]]) for e in rows[: limit * 3]]


def industry_candidates(limit: int) -> list[tuple[dict, dict]]:
    with connect() as db:
        rows = db.all(
            "SELECT ic.*, i.id AS iid, co.employees FROM industry_companies ic JOIN industries i ON i.id = ic.industry_id "
            "JOIN companies co ON co.id = ic.company_id WHERE i.prospecting = 1 AND ic.stage IS NULL "
            "ORDER BY COALESCE(co.employees, 0) DESC LIMIT ?", (limit * 3,))
        inds = {i["id"]: i for i in db.all("SELECT * FROM industries WHERE prospecting = 1")}
    return [(r, inds[r["iid"]]) for r in rows]


def research_due(max_companies: int | None = None) -> dict:
    """Daily-budgeted research across shows and industry campaigns with prospecting on."""
    conf = cfg.all_settings()
    if not amplemarket.configured():
        return {"skipped": "no Amplemarket key"}
    limit = max_companies if max_companies is not None else conf["daily_research_limit"]
    budget = limit - (0 if max_companies is not None else research_done_today())
    counts: dict[str, int] = {}
    if budget <= 0:
        return {"budget_used": True}
    shows_q = candidates(budget)
    inds_q = industry_candidates(budget)
    # industries get up to 40% of the day when both routes have work waiting
    queue: list = []
    while shows_q or inds_q:
        for _ in range(3):
            if shows_q:
                queue.append(("show", shows_q.pop(0)))
        for _ in range(2):
            if inds_q:
                queue.append(("industry", inds_q.pop(0)))
    start = research_done_today()
    for kind, (row, parent) in queue:
        if research_done_today() - start >= budget:
            break
        try:
            stage = process_exhibitor(row, parent, conf) if kind == "show" else process_industry_company(row, parent, conf)
        except amplemarket.AmplemarketError as exc:
            log.warning("Amplemarket error, stopping research for now: %s", exc)
            counts["error"] = counts.get("error", 0) + 1
            break
        counts[stage] = counts.get(stage, 0) + 1
    if counts.get("queued"):
        with connect() as db:
            per_owner = db.all("SELECT owner_id, COUNT(DISTINCT company_id) AS n FROM leads WHERE status = 'review' "
                               "AND created_at >= ? GROUP BY owner_id", (_since_minutes(30),))
        for row in per_owner:
            log_activity("leads_ready", f"{row['n']} compan{'ies' if row['n'] != 1 else 'y'} researched and ready to approve",
                         None, row["owner_id"])
    return counts


def refresh_industry(industry_id: int, max_new: int = 100) -> int:
    """Pull more companies for an industry campaign from Amplemarket's company search."""
    with connect() as db:
        ind = db.one("SELECT * FROM industries WHERE id = ?", (industry_id,))
    if not ind:
        return 0
    conf = cfg.all_settings()
    floor = ind["min_employees"] or conf["min_employees"]
    bands = [("11-50 employees", 50), ("51-200 employees", 200), ("201-500 employees", 500),
             ("501-1000 employees", 1000), ("1001-5000 employees", 5000), ("5001-10000 employees", 10000),
             ("10001+ employees", 10 ** 9)]
    sizes = [label for label, top in bands if top >= floor]
    added, page = 0, (ind["search_page"] or 0) + 1
    total = None
    while added < max_new:
        results, total = amplemarket.search_companies(
            industries=cfg.lines(ind["am_industries"]), keywords=cfg.lines(ind["keywords"]),
            locations=cfg.lines(ind["locations"]), sizes=sizes, page=page, page_size=50)
        if not results:
            page = 0  # start over next time; new companies appear in the database
            break
        for comp in results:
            name = (comp.get("name") or "").strip()
            if not name or _contains_any(name, cfg.lines(conf["exclude_keywords"])):
                continue
            company = _get_company(name, _domain(comp.get("website") or ""))
            save_profile(company["id"], comp)
            with connect() as db:
                if not db.one("SELECT id FROM industry_companies WHERE industry_id = ? AND company_id = ?",
                              (industry_id, company["id"])):
                    db.insert("INSERT INTO industry_companies (industry_id, company_id, found_at) VALUES (?, ?, ?)",
                              (industry_id, company["id"], now()))
                    added += 1
        page += 1
    with connect() as db:
        db.execute("UPDATE industries SET last_search_at = ?, last_search_count = ?, search_page = ? WHERE id = ?",
                   (now(), total, page - 1 if page else 0, industry_id))
    if added:
        log_activity("industry", f"Found {added} more {ind['name']} companies", f"industry:{industry_id}", ind["owner_id"])
    return added


def web_research_due(limit: int | None = None) -> int:
    """Deep web research (other shows, growth, news) on the best accounts that haven't had it."""
    if not ai.configured():
        return 0
    conf = cfg.all_settings()
    limit = limit if limit is not None else conf["daily_web_research_limit"]
    key = f"_web_research_{_today().isoformat()}"
    done = int(cfg.internal(key, 0) or 0)
    if done >= limit:
        return 0
    with connect() as db:
        rows = db.all(
            "SELECT co.* FROM companies co WHERE co.web_researched_at IS NULL AND co.status = 'found' AND EXISTS "
            "(SELECT 1 FROM leads l WHERE l.company_id = co.id AND l.status IN ('review', 'approved', 'sent', 'replied')) "
            "ORDER BY COALESCE(co.employees, 0) DESC LIMIT ?", (limit - done,))
    n = 0
    for co in rows:
        try:
            research_company_web(co["id"])
            n += 1
        except ai.AIError as exc:
            log.warning("web research failed for %s: %s", co["name"], exc)
            break
    cfg.set_internal(key, done + n)
    return n


def research_company_web(company_id: int) -> dict:
    with connect() as db:
        co = db.one("SELECT * FROM companies WHERE id = ?", (company_id,))
        known = [r["name"] for r in db.all("SELECT DISTINCT s.name FROM exhibitors e JOIN shows s ON s.id = e.show_id "
                                           "WHERE e.name_key = ?", (co["name_key"],))]
    found = ai.research_company(co["name"], co["domain"], co.get("industry"), known, _today().isoformat())
    with connect() as db:
        db.execute("DELETE FROM company_shows WHERE company_id = ?", (company_id,))
        for s in found.get("other_shows", []):
            try:
                area = float(s["stand_sqm"]) if s.get("stand_sqm") not in (None, "", "null") else None
            except (TypeError, ValueError):
                area = None
            db.insert("INSERT INTO company_shows (company_id, show_name, start_date, location, stand_sqm, source_url, "
                      "found_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (company_id, str(s.get("name"))[:200], (s.get("date") or "")[:10] or None, s.get("location"),
                       area, s.get("source"), now()))
        db.execute("UPDATE companies SET web_research = ?, web_researched_at = ? WHERE id = ?",
                   (json.dumps(found), now(), company_id))
    return found


# ---------------------------------------------------------------- sending
def lead_data(lead: dict) -> dict:
    """The fields sent to Amplemarket; use them in your sequence as {{opening_line}}, {{show_name}} etc."""
    d = lambda v: "" if v is None else str(v)  # noqa: E731
    show_date = ""
    if lead.get("show_date"):
        try:
            show_date = date.fromisoformat(lead["show_date"][:10]).strftime("%-d %B %Y")
        except ValueError:
            show_date = lead["show_date"]
    area = val.sqm(lead.get("booth_sqft"))
    return {
        "first_name": d(lead.get("first_name")),
        "last_name": d(lead.get("last_name")),
        "company_name": d(lead.get("company_name")),
        "title": d(lead.get("title")),
        "industry": d(lead.get("industry_name") or lead.get("industry")),
        "show_name": d(lead.get("show_name")),
        "show_date": show_date,
        "show_location": d(lead.get("show_location")),
        "booth_number": d(lead.get("booth")),
        "stand_size": f"{area:,.0f} m²" if area else "",
        "subject_line": d(lead.get("subject")),
        "opening_line": d(lead.get("opener")),
        "sender_name": d(lead.get("owner_name")),
        "sender_first_name": d((lead.get("owner_name") or "").split(" ")[0]),
        "sender_title": d(lead.get("owner_title")),
        "sender_phone": d(lead.get("owner_phone")),
    }


LEAD_QUERY = (
    "SELECT l.*, c.email, c.first_name, c.last_name, c.name AS contact_name, c.title, c.linkedin_url, "
    "co.name AS company_name, co.name_key, co.domain, co.employees, co.industry, co.revenue_usd, co.growing, "
    "e.name AS exhibitor_name, e.booth, e.booth_sqft, e.booth_dims, "
    "s.name AS show_name, s.start_date AS show_date, s.location AS show_location, i.name AS industry_name, "
    "u.name AS owner_name, u.email AS owner_email, u.job_title AS owner_title, u.phone AS owner_phone, "
    "u.mailbox AS owner_mailbox, u.sequence_id AS owner_sequence "
    "FROM leads l JOIN contacts c ON c.id = l.contact_id JOIN companies co ON co.id = l.company_id "
    "LEFT JOIN exhibitors e ON e.id = l.exhibitor_id LEFT JOIN shows s ON s.id = l.show_id "
    "LEFT JOIN industries i ON i.id = l.industry_id LEFT JOIN users u ON u.id = l.owner_id"
)


def send_due() -> dict:
    """Push approved leads whose send date has arrived into their Amplemarket sequence."""
    if not amplemarket.configured():
        return {"skipped": "no Amplemarket key"}
    conf = cfg.all_settings()
    today = _today().isoformat()
    with connect() as db:
        due = db.all(LEAD_QUERY + " WHERE l.status = 'approved' AND (l.send_after IS NULL OR l.send_after <= ?)", (today,))
    sent = failed = 0
    by_seq: dict[tuple, list[dict]] = {}
    for lead in due:
        du = days_until(lead["show_date"])
        if du is not None and du < conf["window_end_days"]:
            _update_lead(lead["id"], status="expired", error="Show got too close before this was sent")
            continue
        seq = lead["sequence_id"] or lead["owner_sequence"] or conf["default_sequence_id"]
        if not seq:
            _update_lead(lead["id"], error="No Amplemarket sequence chosen; pick one in Settings, My details or on the show")
            continue
        by_seq.setdefault((seq, lead["owner_mailbox"] or None), []).append(lead)
    sent_by_owner: dict = {}
    for (seq, mailbox), leads in by_seq.items():
        for i in range(0, len(leads), 250):
            chunk = leads[i:i + 250]
            payload = [{"email": l["email"],
                        **({"linkedin_url": l["linkedin_url"]} if (l.get("linkedin_url") or "").startswith("http") else {}),
                        "data": lead_data(l)} for l in chunk]
            try:
                resp = amplemarket.add_leads(seq, payload, mailbox)
            except amplemarket.AmplemarketError as exc:
                for l in chunk:
                    _update_lead(l["id"], error=str(exc)[:400])
                failed += len(chunk)
                continue
            skipped = _skipped_emails(resp)
            ts = now()
            for l in chunk:
                if l["email"].lower() in skipped:
                    _update_lead(l["id"], status="skipped", error=f"Amplemarket skipped this lead: {skipped[l['email'].lower()]}")
                    failed += 1
                else:
                    _update_lead(l["id"], status="sent", sent_at=ts, sequence_id=seq, error=None)
                    sent += 1
                    sent_by_owner[l["owner_id"]] = sent_by_owner.get(l["owner_id"], 0) + 1
    for owner_id, n in sent_by_owner.items():
        log_activity("sent", f"{n} contact{'s' if n != 1 else ''} added to Amplemarket sequences", None, owner_id)
    return {"sent": sent, "failed": failed}


def _skipped_emails(resp) -> dict[str, str]:
    """Amplemarket lists leads its safety checks skipped; pick out their emails and reasons."""
    out: dict[str, str] = {}

    def walk(node, under_skip=False):
        if isinstance(node, dict):
            if under_skip and node.get("email"):
                out[str(node["email"]).lower()] = str(node.get("reason") or node.get("message") or "duplicate or excluded")
            for k, v in node.items():
                walk(v, under_skip or "skip" in str(k).lower())
        elif isinstance(node, list):
            for v in node:
                walk(v, under_skip)
    walk(resp)
    return out


def _update_lead(lead_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as db:
        db.execute(f"UPDATE leads SET {cols} WHERE id = ?", list(fields.values()) + [lead_id])


# ---------------------------------------------------------------- approvals
def approve(lead_ids: list[int], user: str) -> int:
    conf = cfg.all_settings()
    ts = now()
    n = 0
    with connect() as db:
        for lid in lead_ids:
            row = db.one("SELECT l.id, s.start_date FROM leads l LEFT JOIN shows s ON s.id = l.show_id "
                         "WHERE l.id = ? AND l.status IN ('review', 'rejected')", (lid,))
            if not row:
                continue
            db.execute("UPDATE leads SET status = 'approved', approved_at = ?, approved_by = ?, send_after = ?, "
                       "error = NULL WHERE id = ?", (ts, user, send_date({"start_date": row["start_date"]}, conf), lid))
            n += 1
    if n:
        log_activity("approved", f"{user} approved {n} contact{'s' if n != 1 else ''} for outreach")
    return n


def owned_by(lead_ids: list[int], user: dict) -> list[int]:
    """Only the lead's owner (or a manager) can approve, edit or reject it."""
    if user.get("is_admin"):
        return lead_ids
    if not lead_ids:
        return []
    with connect() as db:
        marks = ",".join("?" * len(lead_ids))
        return [r["id"] for r in db.all(f"SELECT id FROM leads WHERE id IN ({marks}) AND owner_id = ?",
                                        list(lead_ids) + [user["id"]])]


def reject(lead_ids: list[int]) -> int:
    with connect() as db:
        for lid in lead_ids:
            db.execute("UPDATE leads SET status = 'rejected' WHERE id = ? AND status IN ('review', 'approved')", (lid,))
    return len(lead_ids)


def rewrite(lead_id: int) -> bool:
    """Ask Claude for a fresh subject and opener for one lead."""
    conf = cfg.all_settings()
    with connect() as db:
        lead = db.one(LEAD_QUERY + " WHERE l.id = ?", (lead_id,))
        if not lead:
            return False
        show = db.one("SELECT * FROM shows WHERE id = ?", (lead["show_id"],)) if lead["show_id"] else None
        ex = db.one("SELECT * FROM exhibitors WHERE id = ?", (lead["exhibitor_id"],)) if lead["exhibitor_id"] else None
        company = db.one("SELECT * FROM companies WHERE id = ?", (lead["company_id"],))
        contact = db.one("SELECT * FROM contacts WHERE id = ?", (lead["contact_id"],))
    acct = val.account(company["name_key"], conf)
    copy = ai.write_openers(conf, show, ex or {"name": company["name"]}, company, [contact],
                            stands=acct["stands"], industry=lead.get("industry_name")).get(contact["id"])
    if copy:
        _update_lead(lead_id, subject=copy["subject"], opener=copy["opener"], error=None)
    return bool(copy)


# ---------------------------------------------------------------- replies
def handle_reply(payload: dict) -> dict:
    dyn = payload.get("dynamic_fields") or {}
    email = (dyn.get("email") or payload.get("from") or "").lower().strip()
    if "<" in email:
        email = email.split("<")[-1].rstrip(">")
    labels = payload.get("labels") or []
    label = str(labels[0]).lower().replace(" ", "_") if labels else ""
    body = payload.get("body") or (payload.get("linkedin") or {}).get("description") or ""
    snippet = " ".join(str(body).split())[:600]
    with connect() as db:
        lead = db.one(LEAD_QUERY + " WHERE LOWER(c.email) = ? AND l.status IN ('sent', 'replied') "
                      "ORDER BY l.sent_at DESC", (email,)) if email else None
    if not lead:
        log_activity("reply", f"Reply from {email or 'unknown sender'} (not matched to a lead)")
        return {"matched": False}
    _update_lead(lead["id"], status="replied", reply_label=label or None, reply_snippet=snippet, replied_at=now())
    log_activity("reply", f"{lead['contact_name'] or email} at {lead['company_name']} replied"
                 + (f" ({label.replace('_', ' ')})" if label else ""), f"lead:{lead['id']}", lead["owner_id"])
    if label not in ("ooo", "hard_no", "not_interested"):
        notify.hot_reply(lead, label, snippet)
    return {"matched": True, "lead_id": lead["id"]}


# ---------------------------------------------------------------- opportunities
def process_opportunity(opp_id: int, user_id: int) -> str:
    """Research an opportunity for one salesperson: contacts + openers, waiting in their approval queue.
    Their pick, so the minimum score doesn't apply (the skip rules still do)."""
    conf = cfg.all_settings()
    with connect() as db:
        opp = db.one("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
        if not opp:
            return "missing"
        show = db.one("SELECT * FROM shows WHERE id = ?", (opp["show_id"],)) if opp["show_id"] else None
        ex = db.one("SELECT * FROM exhibitors WHERE id = ?", (opp["exhibitor_id"],)) if opp["exhibitor_id"] else None
        industry = db.one("SELECT * FROM industries WHERE id = ?", (opp["industry_id"],)) if opp["industry_id"] else None
        company = db.one("SELECT * FROM companies WHERE name_key = ?", (opp["name_key"],))
    if show and days_until(show.get("start_date")) is not None and days_until(show["start_date"]) < conf["window_end_days"]:
        show, ex = None, None
    stage, company, contacts, score, note_text = _qualify(
        opp["company_name"], (ex or {}).get("website") or (company or {}).get("domain") or "", show, conf,
        ex_id=(ex or {}).get("id"), hq=(company or {}).get("location") or "", owner_id=user_id, force=True)
    if stage == "ok":
        _create_leads(show=show, ex=ex, industry=industry, company=company, contacts=contacts, score=score,
                      note_text=note_text, conf=conf, owner_id=user_id, auto_send=False,
                      sequence=(show or {}).get("sequence_id") or (industry or {}).get("sequence_id"))
        if ex:
            _set_stage(ex["id"], "queued", score, note_text, company["id"])
    with connect() as db:
        db.execute("UPDATE opportunities SET status = ?, acted_at = ?, owner_id = ?, research_stage = ? WHERE id = ?",
                   ("actioned" if stage == "ok" else "new", now(), user_id, "started" if stage == "ok" else stage, opp_id))
    return "queued" if stage == "ok" else stage


def research_opportunity(opp_id: int) -> str:
    """Background: score the company and find its contacts so the Opportunities list is ready to act on.
    Nothing is queued for sending until someone presses Start outreach."""
    conf = cfg.all_settings()
    with connect() as db:
        opp = db.one("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
        show = db.one("SELECT * FROM shows WHERE id = ?", (opp["show_id"],)) if opp and opp["show_id"] else None
        company = db.one("SELECT * FROM companies WHERE name_key = ?", (opp["name_key"],)) if opp else None
    if not opp:
        return "missing"

    def save(stage, score=None, notes="", found=None):
        with connect() as db:
            db.execute("UPDATE opportunities SET research_stage = ?, score = ?, score_notes = ?, contacts_found = ?, "
                       "researched_at = ? WHERE id = ?", (stage, score, notes[:600], found, now(), opp_id))
        return stage

    mate = teammate_on(opp["name_key"], opp["owner_id"])
    if mate:
        return save("teammate", None, f"With {mate['name'] or mate['email']} ({mate['show_name'] or mate['industry_name'] or 'outreach'})")
    with connect() as db:
        active = db.one("SELECT COUNT(*) AS n FROM leads l JOIN companies co ON co.id = l.company_id WHERE co.name_key = ? "
                        "AND l.status IN ('review', 'approved', 'sent', 'replied')", (opp["name_key"],))["n"]
    if active:
        return save("in_outreach", None, "Already in outreach from another show or campaign")
    if show and days_until(show.get("start_date")) is not None and days_until(show["start_date"]) < conf["window_end_days"]:
        show = None
    acct = val.account(opp["name_key"], conf)
    score, notes, skip = pre_score({"name": opp["company_name"], "location": (company or {}).get("location") or ""},
                                   show, conf, acct)
    if skip:
        return save("not_fit", score, skip)
    website = (company or {}).get("domain") or ""
    company, contacts = research_company(opp["company_name"], website, conf)
    pts, more, skip = company_points(company, conf)
    score, notes = score + pts, notes + more
    if skip:
        return save("not_fit", score, skip)
    if company["status"] == "not_found":
        return save("not_found", score, "Amplemarket couldn't match this company")
    contacts = _not_recently_contacted(contacts)
    return save("ready" if contacts else "no_contacts", score, "; ".join(notes), len(contacts))


def research_opportunities_due(limit: int | None = None) -> int:
    """Works through new opportunities, best first, within the daily limit. Runs every 15 minutes."""
    if not amplemarket.configured():
        return 0
    conf = cfg.all_settings()
    limit = limit if limit is not None else conf["daily_opportunity_limit"]
    key = f"_opp_research_{_today().isoformat()}"
    done = int(cfg.internal(key, 0) or 0)
    if done >= limit:
        return 0
    with connect() as db:
        rows = db.all("SELECT o.id, o.kind, o.value_high FROM opportunities o LEFT JOIN shows s ON s.id = o.show_id "
                      "WHERE o.status = 'new' AND o.research_stage IS NULL "
                      "AND (s.start_date IS NULL OR s.start_date >= ?)", (_today().isoformat(),))
    from .opportunities import KIND_RANK

    rows.sort(key=lambda r: (-(r["value_high"] or 0), KIND_RANK.get(r["kind"], 9)))
    n = 0
    for r in rows[: limit - done]:
        try:
            research_opportunity(r["id"])
            n += 1
        except amplemarket.AmplemarketError as exc:
            log.warning("opportunity research stopped: %s", exc)
            break
    cfg.set_internal(key, done + n)
    if n:
        log_activity("opportunity", f"Researched {n} opportunit{'ies' if n != 1 else 'y'}; contacts are ready on the list")
    return n
