"""Exhibitor -> qualified company -> decision-makers with checked emails -> personalised opener -> Amplemarket.

Flow for each show with prospecting switched on:
  1. Pre-score every exhibitor on what we already know (booth size, timing, name) and skip poor fits
     before spending any Amplemarket credits.
  2. Research the best ones (biggest booths at the soonest shows first) up to a daily limit:
     Amplemarket people search -> pick the right people -> reveal + validate emails.
  3. Final score with company size and industry; Claude writes a subject and opening line per person.
  4. Leads wait in the approval queue (or skip it for shows set to send automatically).
  5. Approved leads go into the Amplemarket sequence once the show is inside your outreach window.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from . import ai, amplemarket, notify
from . import settings as cfg
from .db import connect, log_activity, now
from .scrapers import name_key

log = logging.getLogger("showtracker")

RESEARCH_REUSE_DAYS = 120   # a company researched for one show is reused for others within this time
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


def send_date(show: dict, conf: dict) -> str:
    """Earliest date to start the sequence: today, or when the show enters the outreach window."""
    du = days_until(show.get("start_date"))
    if du is None:
        return _today().isoformat()
    start = _today() + timedelta(days=max(0, du - conf["window_start_days"]))
    return start.isoformat()


def _contains_any(text: str, words: list[str]) -> str | None:
    t = (text or "").lower()
    return next((w for w in words if w.lower() in t), None)


# ---------------------------------------------------------------- scoring
def pre_score(ex: dict, show: dict, conf: dict) -> tuple[int, list[str], str | None]:
    """Score from what the show listing tells us. Returns (score, notes, reason_to_skip)."""
    notes: list[str] = []
    hit = _contains_any(ex["name"], cfg.lines(conf["exclude_keywords"]))
    if hit:
        return 0, [f"name contains '{hit}'"], f"Name contains '{hit}'"
    du = days_until(show.get("start_date"))
    if du is not None and du < conf["window_end_days"]:
        return 0, ["show too close"], "Show is too close to start a stand project"
    score = 0
    sqft = ex.get("booth_sqft")
    lo, ideal = conf["min_booth_sqft"], max(conf["ideal_booth_sqft"], conf["min_booth_sqft"] + 1)
    if sqft:
        if sqft < lo:
            return 0, [f"booth {sqft} sq ft"], f"Booth only {sqft} sq ft"
        pts = 40 if sqft >= ideal else round(20 + 20 * (sqft - lo) / (ideal - lo))
        score += pts
        notes.append(f"booth {sqft:,} sq ft (+{pts})")
    else:
        score += 20
        notes.append("booth size unknown (+20)")
    if du is None or du <= conf["window_start_days"]:
        score += 20
        notes.append("inside outreach window (+20)")
    else:
        score += 15
        notes.append(f"show in {du} days, will send when window opens (+15)")
    if ex.get("booth"):
        score += 10
        notes.append("has a booth number (+10)")
    return score, notes, None


def company_points(company: dict, conf: dict) -> tuple[int, list[str], str | None]:
    ind = company.get("industry") or ""
    hit = _contains_any(ind, cfg.lines(conf["exclude_industries"]))
    if hit:
        return 0, [f"industry {ind}"], f"Industry excluded ({ind})"
    emp = company.get("employees")
    if emp is None:
        return 15, ["company size unknown (+15)"], None
    if emp < conf["min_employees"]:
        return 0, [f"{emp} employees"], f"Only {emp} employees"
    pts = 30 if emp >= 200 else 22 if emp >= 50 else 15
    return pts, [f"{emp:,} employees (+{pts})"], None


# ---------------------------------------------------------------- research
def _rank_people(people: list[dict], titles: list[str]) -> list[dict]:
    def rank(p):
        t = (p.get("title") or "").lower()
        idx = next((i for i, want in enumerate(titles) if want.lower() in t), len(titles))
        return (idx, 0 if p.get("email") else 1)
    return sorted(people, key=rank)


def _matches_company(person: dict, ex_key: str) -> bool:
    comp = name_key((person.get("company") or {}).get("name") or "")
    return bool(comp) and (comp == ex_key or comp.startswith(ex_key + " ") or ex_key.startswith(comp + " "))


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    if not url:
        return ""
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def research_company(ex: dict, conf: dict) -> tuple[dict, list[dict]]:
    """Find (or reuse) the company and its best contacts with usable emails."""
    key = name_key(ex["name"])
    with connect() as db:
        company = db.one("SELECT * FROM companies WHERE name_key = ?", (key,))
        if not company:
            cid = db.insert("INSERT INTO companies (name, name_key, status) VALUES (?, ?, 'pending')", (ex["name"], key))
            company = db.one("SELECT * FROM companies WHERE id = ?", (cid,))
    fresh = company["researched_at"] and (
        datetime.now(timezone.utc) - datetime.fromisoformat(company["researched_at"])).days < RESEARCH_REUSE_DAYS
    if fresh:
        with connect() as db:
            contacts = db.all("SELECT * FROM contacts WHERE company_id = ? AND email IS NOT NULL", (company["id"],))
        return company, [c for c in contacts if _email_ok(c["email_status"], conf)]

    titles = cfg.lines(conf["target_titles"])
    domain = _domain(ex.get("website") or "")
    people = amplemarket.search_people(company_domain=domain, company_name="" if domain else ex["name"],
                                       titles=titles, seniorities=cfg.lines(conf["target_seniorities"]), page_size=25)
    if not domain:
        people = [p for p in people if _matches_company(p, key)]
    if not people:  # broaden: anyone senior in marketing/events at the company
        people = amplemarket.search_people(company_domain=domain, company_name="" if domain else ex["name"],
                                           seniorities=cfg.lines(conf["target_seniorities"]), page_size=25)
        people = [p for p in people if domain or _matches_company(p, key)]
        people = [p for p in people if _contains_any(p.get("title") or "", ["market", "event", "brand", "exhibit", "trade show", "communication"])]

    comp = (people[0].get("company") or {}) if people else {}
    updates = {
        "domain": _domain(comp.get("website") or "") or domain or None,
        "linkedin_url": comp.get("linkedin_url"),
        "industry": comp.get("industry"),
        "size": comp.get("size"),
        "employees": comp.get("estimated_number_of_employees"),
        "revenue": comp.get("estimated_revenue") or (str(comp["revenue"]) if comp.get("revenue") else None),
        "location": comp.get("location") if isinstance(comp.get("location"), str) else None,
    }
    chosen = _rank_people(people, titles)[: conf["contacts_per_company"]]
    for p in chosen:
        if not p.get("email"):
            try:
                found = amplemarket.reveal_email(p)
                p["email"] = found.get("email") or None
            except amplemarket.AmplemarketError as exc:
                log.info("email reveal failed for %s: %s", p.get("name"), exc)
    statuses = amplemarket.validate_emails([p["email"] for p in chosen if p.get("email")])

    ts = now()
    with connect() as db:
        db.execute(
            "UPDATE companies SET domain = COALESCE(?, domain), linkedin_url = COALESCE(?, linkedin_url), "
            "industry = COALESCE(?, industry), size = COALESCE(?, size), employees = COALESCE(?, employees), "
            "revenue = COALESCE(?, revenue), location = COALESCE(?, location), status = ?, researched_at = ? WHERE id = ?",
            (updates["domain"], updates["linkedin_url"], updates["industry"], updates["size"],
             updates["employees"], updates["revenue"], updates["location"],
             "found" if people else "not_found", ts, company["id"]),
        )
        saved = []
        for p in chosen:
            email = (p.get("email") or "").lower() or None
            status = statuses.get(email) if email else None
            link = p.get("linkedin_url") or f"am:{p.get('id')}"
            existing = db.one("SELECT id FROM contacts WHERE company_id = ? AND linkedin_url = ?", (company["id"], link))
            values = (p.get("id"), p.get("name"), p.get("first_name"), p.get("last_name"), p.get("title"),
                      email, status, p.get("location") if isinstance(p.get("location"), str) else None)
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


# ---------------------------------------------------------------- per exhibitor
def _set_stage(ex_id: int, stage: str, score: int | None = None, notes: str | None = None, company_id=None) -> None:
    with connect() as db:
        db.execute("UPDATE exhibitors SET stage = ?, score = COALESCE(?, score), score_notes = COALESCE(?, score_notes), "
                   "company_id = COALESCE(?, company_id) WHERE id = ?", (stage, score, notes, company_id, ex_id))


def process_exhibitor(ex: dict, show: dict, conf: dict) -> str:
    """Runs one exhibitor through the pipeline. Returns the stage it ended in."""
    score, notes, skip = pre_score(ex, show, conf)
    if skip:
        _set_stage(ex["id"], "skipped", score, skip)
        return "skipped"
    try:
        company, contacts = research_company(ex, conf)
    except amplemarket.AmplemarketError as exc:
        _set_stage(ex["id"], "error", score, str(exc)[:300])
        raise
    pts, more, skip = company_points(company, conf)
    score, notes = score + pts, notes + more
    note_text = "; ".join(notes)
    if skip:
        _set_stage(ex["id"], "skipped", score, skip, company["id"])
        return "skipped"
    if company["status"] == "not_found":
        _set_stage(ex["id"], "not_found", score, "Amplemarket couldn't match this company", company["id"])
        return "not_found"
    if score < conf["min_score"]:
        _set_stage(ex["id"], "low_score", score, note_text, company["id"])
        return "low_score"
    contacts = _not_recently_contacted(contacts)
    if not contacts:
        _set_stage(ex["id"], "no_contacts", score, "No decision-makers with a checked email (or contacted recently)", company["id"])
        return "no_contacts"

    copy, copy_error = {}, None
    if ai.configured():
        try:
            copy = ai.write_openers(conf, show, ex, company, contacts)
        except ai.AIError as exc:
            copy_error = str(exc)
    status = "approved" if show.get("auto_send") else "review"
    ts = now()
    with connect() as db:
        for c in contacts:
            if db.one("SELECT id FROM leads WHERE show_id = ? AND contact_id = ?", (show["id"], c["id"])):
                continue
            line = copy.get(c["id"], {})
            db.insert(
                "INSERT INTO leads (exhibitor_id, show_id, company_id, contact_id, status, score, score_notes, subject, "
                "opener, sequence_id, send_after, created_at, approved_at, approved_by, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ex["id"], show["id"], company["id"], c["id"], status, score, note_text, line.get("subject"),
                 line.get("opener"), show.get("sequence_id") or conf["default_sequence_id"] or None,
                 send_date(show, conf), ts, ts if status == "approved" else None,
                 "auto" if status == "approved" else None, copy_error),
            )
    _set_stage(ex["id"], "queued", score, note_text, company["id"])
    return "queued"


def _not_recently_contacted(contacts: list[dict]) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECONTACT_GAP_DAYS)).isoformat()
    with connect() as db:
        recent = {r["email"] for r in db.all(
            "SELECT c.email FROM leads l JOIN contacts c ON c.id = l.contact_id "
            "WHERE l.status IN ('approved', 'sent', 'replied') AND COALESCE(l.sent_at, l.approved_at, l.created_at) >= ?",
            (cutoff,))}
    return [c for c in contacts if c["email"] not in recent]


# ---------------------------------------------------------------- batch jobs
def candidates(limit: int, show_id: int | None = None) -> list[tuple[dict, dict]]:
    """Exhibitors still to research, best first: soonest show inside the window, then biggest booth."""
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
    rows.sort(key=lambda e: (days_until(e["_show_date"]) if days_until(e["_show_date"]) is not None else 9999,
                             -(e["booth_sqft"] or 0)))
    return [(e, shows[e["show_id"]]) for e in rows[: limit * 3]]


def research_due(max_companies: int | None = None) -> dict:
    """Daily-budgeted research across all shows with prospecting on."""
    conf = cfg.all_settings()
    if not amplemarket.configured():
        return {"skipped": "no Amplemarket key"}
    budget = (max_companies if max_companies is not None else conf["daily_research_limit"]) - research_done_today()
    counts: dict[str, int] = {}
    if budget <= 0:
        return {"budget_used": True}
    for ex, show in candidates(budget):
        if research_done_today() >= conf["daily_research_limit"] and max_companies is None:
            break
        try:
            stage = process_exhibitor(ex, show, conf)
        except amplemarket.AmplemarketError as exc:
            log.warning("Amplemarket error, stopping research for now: %s", exc)
            counts["error"] = counts.get("error", 0) + 1
            break
        counts[stage] = counts.get(stage, 0) + 1
    if counts.get("queued"):
        log_activity("leads_ready", f"{counts['queued']} compan{'ies' if counts['queued'] != 1 else 'y'} researched and ready for outreach")
    return counts


def lead_data(lead: dict) -> dict:
    """The fields sent to Amplemarket; use them in your sequence as {{opening_line}}, {{show_name}} etc."""
    d = lambda v: "" if v is None else str(v)  # noqa: E731
    show_date = ""
    if lead.get("show_date"):
        try:
            show_date = date.fromisoformat(lead["show_date"][:10]).strftime("%-d %B %Y")
        except ValueError:
            show_date = lead["show_date"]
    return {
        "first_name": d(lead.get("first_name")),
        "last_name": d(lead.get("last_name")),
        "company_name": d(lead.get("company_name")),
        "title": d(lead.get("title")),
        "show_name": d(lead.get("show_name")),
        "show_date": show_date,
        "show_location": d(lead.get("show_location")),
        "booth_number": d(lead.get("booth")),
        "booth_size": f"{lead['booth_sqft']:,} sq ft" if lead.get("booth_sqft") else "",
        "subject_line": d(lead.get("subject")),
        "opening_line": d(lead.get("opener")),
    }


LEAD_QUERY = (
    "SELECT l.*, c.email, c.first_name, c.last_name, c.name AS contact_name, c.title, c.linkedin_url, "
    "co.name AS company_name, co.domain, co.employees, co.industry, e.name AS exhibitor_name, e.booth, "
    "e.booth_sqft, e.booth_dims, s.name AS show_name, s.start_date AS show_date, s.location AS show_location "
    "FROM leads l JOIN contacts c ON c.id = l.contact_id JOIN companies co ON co.id = l.company_id "
    "JOIN exhibitors e ON e.id = l.exhibitor_id JOIN shows s ON s.id = l.show_id"
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
    by_seq: dict[str, list[dict]] = {}
    for lead in due:
        du = days_until(lead["show_date"])
        if du is not None and du < conf["window_end_days"]:
            _update_lead(lead["id"], status="expired", error="Show got too close before this was sent")
            continue
        seq = lead["sequence_id"] or conf["default_sequence_id"]
        if not seq:
            _update_lead(lead["id"], error="No Amplemarket sequence chosen; pick one in Settings or on the show")
            continue
        by_seq.setdefault(seq, []).append(lead)
    for seq, leads in by_seq.items():
        for i in range(0, len(leads), 250):
            chunk = leads[i:i + 250]
            payload = [{"email": l["email"],
                        **({"linkedin_url": l["linkedin_url"]} if (l.get("linkedin_url") or "").startswith("http") else {}),
                        "data": lead_data(l)} for l in chunk]
            try:
                resp = amplemarket.add_leads(seq, payload)
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
    if sent:
        log_activity("sent", f"{sent} contact{'s' if sent != 1 else ''} added to Amplemarket sequences")
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
            row = db.one("SELECT l.id, s.start_date FROM leads l JOIN shows s ON s.id = l.show_id "
                         "WHERE l.id = ? AND l.status IN ('review', 'rejected')", (lid,))
            if not row:
                continue
            db.execute("UPDATE leads SET status = 'approved', approved_at = ?, approved_by = ?, send_after = ?, "
                       "error = NULL WHERE id = ?", (ts, user, send_date({"start_date": row["start_date"]}, conf), lid))
            n += 1
    if n:
        log_activity("approved", f"{user} approved {n} contact{'s' if n != 1 else ''} for outreach")
    return n


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
        show = db.one("SELECT * FROM shows WHERE id = ?", (lead["show_id"],))
        ex = db.one("SELECT * FROM exhibitors WHERE id = ?", (lead["exhibitor_id"],))
        company = db.one("SELECT * FROM companies WHERE id = ?", (lead["company_id"],))
        contact = db.one("SELECT * FROM contacts WHERE id = ?", (lead["contact_id"],))
    copy = ai.write_openers(conf, show, ex, company, [contact]).get(contact["id"])
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
                 + (f" ({label.replace('_', ' ')})" if label else ""), f"lead:{lead['id']}")
    if label not in ("ooo", "hard_no", "not_interested"):
        notify.hot_reply(lead, label, snippet)
    return {"matched": True, "lead_id": lead["id"]}
