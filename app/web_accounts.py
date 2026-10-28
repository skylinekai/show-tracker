"""Companies (accounts ranked by value) and Industries (industry-wide campaigns)."""
from __future__ import annotations

import json
from datetime import date

from flask import abort, flash, g, redirect, render_template, request, url_for

from . import ai, amplemarket, auth, jobs, prospecting
from . import settings as cfg
from . import value as val
from .db import connect, log_activity, now
from .web import _back, _bg, _sequences, _team, _who, app  # noqa: F401


# ---------------------------------------------------------------- template helpers
@app.template_filter("money")
def money_filter(n) -> str:
    return val.money(n) if n else ""


@app.template_filter("moneyrange")
def money_range_filter(v) -> str:
    return val.money_range(v)


@app.template_filter("m2")
def m2_filter(sqft) -> str:
    a = val.sqm(sqft)
    return f"{a:,.0f} m²" if a else ""


@app.template_filter("fromjson")
def fromjson(s):
    try:
        return json.loads(s) if s else None
    except ValueError:
        return None


# ---------------------------------------------------------------- accounts
def all_accounts(conf: dict, industry_id: int | None = None, who: str = "all", q: str = "") -> list[dict]:
    """Every company we know of, rolled up across shows, with estimated stand value."""
    today = date.today().isoformat()
    with connect() as db:
        shows = {s["id"]: s for s in db.all("SELECT id, name, start_date, location, owner_id, industry_id FROM shows")}
        ex = db.all("SELECT name, name_key, show_id, booth_sqft, removed_at FROM exhibitors WHERE likely_junk = 0")
        companies = {c["name_key"]: c for c in db.all("SELECT * FROM companies")}
        web = db.all("SELECT company_id, show_name, start_date, location, stand_sqm FROM company_shows")
        in_industry: dict[int, set] = {}
        ind_owner = {i["id"]: i["owner_id"] for i in db.all("SELECT id, owner_id FROM industries")}
        for r in db.all("SELECT ic.industry_id, co.name_key FROM industry_companies ic JOIN companies co ON co.id = ic.company_id"):
            in_industry.setdefault(r["industry_id"], set()).add(r["name_key"])
        lead_rows = db.all("SELECT co.name_key, l.status, l.owner_id FROM leads l JOIN companies co ON co.id = l.company_id")
    priority = {sid: val.is_priority(s, conf) for sid, s in shows.items()}
    outsourced = {sid: val.is_outsourced(s, conf) for sid, s in shows.items()}
    acc: dict[str, dict] = {}

    def entry(key, name):
        if key not in acc:
            acc[key] = {"key": key, "name": name, "stands": [], "owners": set(), "statuses": set(),
                        "industries": set(), "company": companies.get(key)}
        return acc[key]

    for e in ex:
        if e["removed_at"]:
            continue
        s = shows[e["show_id"]]
        a = entry(e["name_key"], e["name"])
        a["stands"].append({"show": s["name"], "date": s["start_date"], "sqm": val.sqm(e["booth_sqft"]),
                            "priority": priority[e["show_id"]], "outsourced": outsourced[e["show_id"]],
                            "upcoming": not s["start_date"] or s["start_date"] >= today})
        if s["owner_id"]:
            a["owners"].add(s["owner_id"])
        if s["industry_id"]:
            a["industries"].add(s["industry_id"])
    by_id = {c["id"]: c for c in companies.values()}
    for w in web:
        c = by_id.get(w["company_id"])
        if not c:
            continue
        a = entry(c["name_key"], c["name"])
        if any(st["show"].lower() == (w["show_name"] or "").lower() for st in a["stands"]):
            continue
        a["stands"].append({"show": w["show_name"], "date": w["start_date"], "sqm": w["stand_sqm"],
                            "priority": val.is_priority({"location": w["location"], "name": w["show_name"]}, conf),
                            "outsourced": val.is_outsourced({"location": w["location"], "name": w["show_name"]}, conf),
                            "upcoming": not w["start_date"] or w["start_date"] >= today})
    for iid, keys in in_industry.items():
        for k in keys:
            c = companies.get(k)
            if c:
                e = entry(k, c["name"])
                e["industries"].add(iid)
                if ind_owner.get(iid):
                    e["owners"].add(ind_owner[iid])
    for r in lead_rows:
        if r["name_key"] in acc:
            acc[r["name_key"]]["statuses"].add(r["status"])
            if r["owner_id"]:
                acc[r["name_key"]]["owners"].add(r["owner_id"])

    out = []
    ql = q.lower()
    for a in acc.values():
        if industry_id and industry_id not in a["industries"]:
            continue
        if who == "me" and g.user["id"] not in a["owners"]:
            continue
        if who.isdigit() and int(who) not in a["owners"]:
            continue
        if ql and ql not in a["name"].lower():
            continue
        up = [s for s in a["stands"] if s["upcoming"]]
        sized = [s for s in a["stands"] if s["sqm"]]
        best = max(sized, key=lambda s: s["sqm"]) if sized else None
        a["best"] = best
        a["best_value"] = val.stand_value(best["sqm"], conf, best["outsourced"]) if best else None
        a["annual"] = sum(val.mid(val.stand_value(s["sqm"], conf, s["outsourced"])) for s in up if s["sqm"])
        a["annual_gp"] = sum(val.gross_profit(val.mid(val.stand_value(s["sqm"], conf, s["outsourced"])), s["outsourced"], conf)
                             for s in up if s["sqm"])
        a["show_count"] = len({s["show"] for s in up})
        a["priority"] = any(s["priority"] for s in up)
        a["qualifies"] = bool(a["best_value"]) and val.mid(a["best_value"]) >= conf["min_project_value"]
        out.append(a)
    out.sort(key=lambda a: (-(a["annual"] or 0), -(a["best"]["sqm"] if a["best"] else 0), a["name"].lower()))
    return out


@app.get("/companies")
def companies_page():
    conf = cfg.all_settings()
    q = (request.args.get("q") or "").strip()
    who = _who("all" if g.user["is_admin"] else "me")
    industry_id = request.args.get("industry", type=int)
    only = request.args.get("only", "qualified")
    rows = all_accounts(conf, industry_id, who, q)
    if only == "qualified":
        rows = [r for r in rows if r["qualifies"]]
    elif only == "priority":
        rows = [r for r in rows if r["priority"] and r["qualifies"]]
    elif only == "multi":
        rows = [r for r in rows if r["show_count"] >= 2]
    total = len(rows)
    with connect() as db:
        industries = db.all("SELECT id, name FROM industries ORDER BY name")
    team = {u["id"]: auth.display_name(u) for u in _team()}
    return render_template("companies.html", rows=rows[:300], total=total, q=q, who=who, only=only,
                           industry_id=industry_id, industries=industries, team=team, conf=conf)


@app.get("/companies/<key>")
def company_page(key: str):
    conf = cfg.all_settings()
    acct = val.account(key, conf)
    with connect() as db:
        name = db.one("SELECT name FROM exhibitors WHERE name_key = ? LIMIT 1", (key,))
        company = acct["company"]
        if not name and not company:
            abort(404)
        contacts = db.all("SELECT * FROM contacts WHERE company_id = ? ORDER BY name", (company["id"],)) if company else []
        leads = db.all(prospecting.LEAD_QUERY + " WHERE co.name_key = ? ORDER BY l.created_at DESC", (key,)) if company else []
        industries = db.all("SELECT i.id, i.name FROM industries i JOIN industry_companies ic ON ic.industry_id = i.id "
                            "WHERE ic.company_id = ?", (company["id"],)) if company else []
    if not g.user["is_admin"]:
        owners = _account_owners(key)
        if owners and g.user["id"] not in owners:
            names = ", ".join(auth.display_name(auth.get_user(o)) for o in owners)
            flash(f"{(company or {}).get('name') or name['name']} is being worked by {names}.", "warn")
            return redirect(url_for("companies_page"))
    research = json.loads(company["web_research"]) if company and company.get("web_research") else None
    roles = json.loads(company["marketing_roles"]) if company and company.get("marketing_roles") else []
    return render_template("company.html", key=key, name=(company or {}).get("name") or name["name"], acct=acct,
                           company=company, contacts=contacts, leads=leads, research=research, roles=roles,
                           industries=industries, conf=conf, ai_ok=ai.configured(), am_ok=amplemarket.configured())


def _account_owners(key: str) -> set:
    with connect() as db:
        rows = db.all("SELECT s.owner_id FROM exhibitors e JOIN shows s ON s.id = e.show_id WHERE e.name_key = ? "
                      "AND s.owner_id IS NOT NULL", (key,))
        rows += db.all("SELECT l.owner_id FROM leads l JOIN companies co ON co.id = l.company_id WHERE co.name_key = ? "
                       "AND l.owner_id IS NOT NULL", (key,))
        rows += db.all("SELECT i.owner_id FROM industry_companies ic JOIN industries i ON i.id = ic.industry_id "
                       "JOIN companies co ON co.id = ic.company_id WHERE co.name_key = ? AND i.owner_id IS NOT NULL", (key,))
    return {r["owner_id"] for r in rows}


@app.post("/companies/<key>/web-research")
def company_web_research(key: str):
    with connect() as db:
        company = db.one("SELECT * FROM companies WHERE name_key = ?", (key,))
        if not company:
            name = db.one("SELECT name FROM exhibitors WHERE name_key = ? LIMIT 1", (key,))
            if not name:
                abort(404)
            cid = db.insert("INSERT INTO companies (name, name_key, status) VALUES (?, ?, 'pending')", (name["name"], key))
            company = {"id": cid, "name": name["name"]}
    try:
        prospecting.research_company_web(company["id"])
        flash("Research updated.", "ok")
    except ai.AIError as exc:
        flash(f"Couldn't research right now: {exc}", "bad")
    return redirect(url_for("company_page", key=key))


# ---------------------------------------------------------------- industries
@app.route("/industries", methods=["GET", "POST"])
def industries_page():
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()
        if not name:
            flash("Give the industry a name.", "bad")
            return redirect(url_for("industries_page"))
        clash = industry_clash(name, f.get("keywords", ""))
        if clash:
            who = clash["owner_name"] or clash["owner_email"]
            if clash["owner_id"] and clash["owner_id"] != g.user["id"]:
                flash(f"{who} is already running a {clash['name']} campaign, which overlaps with this one. "
                      "Ask them or a manager before starting another.", "bad")
                return redirect(url_for("industries_page"))
            if not clash["owner_id"]:
                with connect() as db:
                    db.execute("UPDATE industries SET owner_id = ? WHERE id = ?", (g.user["id"], clash["id"]))
                flash(f"There was already a {clash['name']} campaign up for grabs, so it's now yours.", "ok")
            return redirect(url_for("industry_page", iid=clash["id"]))
        with connect() as db:
            iid = db.insert("INSERT INTO industries (name, keywords, am_industries, locations, min_employees, owner_id, "
                            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (name, f.get("keywords", "").strip() or name.lower(), f.get("am_industries", "").strip(),
                             f.get("locations", "").strip() or "United Kingdom", f.get("min_employees", type=int),
                             g.user["id"], now()))
        log_activity("industry", f"{auth.display_name(g.user)} started an industry campaign: {name}",
                     f"industry:{iid}", g.user["id"])
        return redirect(url_for("industry_page", iid=iid))
    with connect() as db:
        rows = db.all(
            "SELECT i.*, u.name AS owner_name, u.email AS owner_email, "
            " (SELECT COUNT(*) FROM industry_companies ic WHERE ic.industry_id = i.id) AS found, "
            " (SELECT COUNT(*) FROM shows s WHERE s.industry_id = i.id) AS shows, "
            " (SELECT COUNT(*) FROM leads l WHERE l.industry_id = i.id AND l.status IN ('sent', 'replied')) AS contacted, "
            " (SELECT COUNT(*) FROM leads l WHERE l.industry_id = i.id AND l.status = 'replied') AS replied "
            "FROM industries i LEFT JOIN users u ON u.id = i.owner_id ORDER BY i.name")
    if not g.user["is_admin"]:
        rows = [r for r in rows if r["owner_id"] in (None, g.user["id"])]
    return render_template("industries.html", rows=rows)


def _stem(word: str) -> str:
    w = word.lower().strip()
    for suffix in ("istries", "istry", "ists", "ist", "ical", "ics", "ic", "al", "s"):
        if len(w) > 5 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


def industry_clash(name: str, keywords: str) -> dict | None:
    """An existing campaign covering the same ground (same name, or overlapping keywords)."""
    import re as _re

    def words(text):
        return {_stem(w) for w in _re.findall(r"[a-zA-Z]{3,}", text or "")}
    mine_name, mine_kw = words(name), words(name + " " + keywords)
    with connect() as db:
        rows = db.all("SELECT i.*, u.name AS owner_name, u.email AS owner_email FROM industries i "
                      "LEFT JOIN users u ON u.id = i.owner_id")
    for r in rows:
        theirs_name, theirs_kw = words(r["name"]), words(r["name"] + " " + (r["keywords"] or ""))
        if mine_name & theirs_name:
            return r
        overlap = mine_kw & theirs_kw
        if overlap and len(overlap) / max(1, min(len(mine_kw), len(theirs_kw))) >= 0.5:
            return r
    return None


def _industry_or_403(iid: int, manage: bool = True) -> dict:
    with connect() as db:
        ind = db.one("SELECT * FROM industries WHERE id = ?", (iid,))
    if not ind:
        abort(404)
    if manage and not (g.user["is_admin"] or ind["owner_id"] in (None, g.user["id"])):
        abort(403, "This campaign belongs to someone else.")
    return ind


@app.get("/industries/<int:iid>")
def industry_page(iid: int):
    conf = cfg.all_settings()
    ind = _industry_or_403(iid, manage=False)
    if not (g.user["is_admin"] or ind["owner_id"] in (None, g.user["id"])):
        flash(f"The {ind['name']} campaign belongs to a teammate.", "warn")
        return redirect(url_for("industries_page"))
    with connect() as db:
        shows = db.all("SELECT s.*, (SELECT COUNT(*) FROM exhibitors e WHERE e.show_id = s.id AND e.removed_at IS NULL "
                       "AND e.likely_junk = 0) AS exhibitors FROM shows s WHERE s.industry_id = ? ORDER BY s.start_date",
                       (iid,))
        other_shows = db.all("SELECT id, name FROM shows WHERE industry_id IS NULL OR industry_id <> ? ORDER BY name", (iid,))
        suggested = db.all("SELECT * FROM discovered_shows WHERE industry_id = ? AND status = 'suggested' "
                           "ORDER BY start_date", (iid,))
        stages = {r["stage"] or "waiting": r["n"] for r in db.all(
            "SELECT stage, COUNT(*) AS n FROM industry_companies WHERE industry_id = ? GROUP BY stage", (iid,))}
        lead_counts = {r["status"]: r["n"] for r in db.all(
            "SELECT status, COUNT(*) AS n FROM leads WHERE industry_id = ? GROUP BY status", (iid,))}
        owner = db.one("SELECT id, name, email FROM users WHERE id = ?", (ind["owner_id"],)) if ind["owner_id"] else None
    for s in shows:
        s["priority"] = val.is_priority(s, conf)
    accounts = all_accounts(conf, iid)
    return render_template("industry.html", ind=ind, shows=shows, other_shows=other_shows, suggested=suggested,
                           stages=stages, lead_counts=lead_counts, accounts=accounts[:150], total=len(accounts),
                           owner=owner, sequences=_sequences() if ind["prospecting"] else [], team=_team(),
                           can_manage=g.user["is_admin"] or ind["owner_id"] in (None, g.user["id"]),
                           am_ok=amplemarket.configured(), ai_ok=ai.configured(), conf=conf)


@app.post("/industries/<int:iid>/edit")
def industry_edit(iid: int):
    ind = _industry_or_403(iid)
    f = request.form
    on = bool(ind["prospecting"]) if f.get("details") == "1" else f.get("prospecting") == "1"
    owner_id = ind["owner_id"]
    if g.user["is_admin"] and "owner_id" in f:
        owner_id = int(f["owner_id"]) if f["owner_id"].isdigit() else None
    elif on and owner_id is None:
        owner_id = g.user["id"]
    with connect() as db:
        db.execute("UPDATE industries SET name = ?, keywords = ?, am_industries = ?, locations = ?, min_employees = ?, "
                   "prospecting = ?, auto_send = ?, sequence_id = ?, owner_id = ? WHERE id = ?",
                   (f.get("name", ind["name"]).strip() or ind["name"], f.get("keywords", ind["keywords"] or ""),
                    f.get("am_industries", ind["am_industries"] or ""), f.get("locations", ind["locations"] or ""),
                    f.get("min_employees", type=int), 1 if on else 0, 1 if f.get("auto_send") == "1" else 0,
                    f.get("sequence_id") or None, owner_id, iid))
        db.execute("UPDATE leads SET owner_id = ? WHERE industry_id = ? AND show_id IS NULL AND status IN ('review', 'approved')",
                   (owner_id, iid))
    if on and not ind["prospecting"]:
        flash("Campaign is on. Companies are being found now, then researched within your daily limit.", "ok")
        _bg(_kickoff_industry, iid)
    else:
        flash("Saved.", "ok")
    return redirect(url_for("industry_page", iid=iid))


def _kickoff_industry(iid: int) -> None:
    try:
        prospecting.refresh_industry(iid)
        prospecting.research_due(10)
    except amplemarket.AmplemarketError as exc:
        log_activity("industry", f"Industry search failed: {exc}")


@app.post("/industries/<int:iid>/search")
def industry_search(iid: int):
    _industry_or_403(iid)
    if not amplemarket.configured():
        flash("Connect Amplemarket first (AMPLEMARKET_API_KEY).", "bad")
    else:
        _bg(prospecting.refresh_industry, iid)
        flash("Searching Amplemarket for more companies. Refresh in a minute.", "ok")
    return redirect(url_for("industry_page", iid=iid))


@app.post("/industries/<int:iid>/find-shows")
def industry_find_shows(iid: int):
    ind = _industry_or_403(iid)
    if not ai.configured():
        flash("Add your Anthropic API key first (ANTHROPIC_API_KEY).", "bad")
        return redirect(url_for("industry_page", iid=iid))
    regions = cfg.lines(cfg.get("discovery_regions"))
    _bg(jobs.discover_for, [ind["name"]] + cfg.lines(ind["keywords"])[:4], regions, iid)
    flash(f"Searching the web for {ind['name']} shows. This takes a minute or two; refresh to see them.", "ok")
    return redirect(url_for("industry_page", iid=iid))


@app.post("/industries/<int:iid>/shows")
def industry_tag_show(iid: int):
    _industry_or_403(iid)
    sid = request.form.get("show_id", type=int)
    remove = request.form.get("remove") == "1"
    with connect() as db:
        db.execute("UPDATE shows SET industry_id = ? WHERE id = ?", (None if remove else iid, sid))
    return _back(url_for("industry_page", iid=iid))


@app.post("/industries/<int:iid>/delete")
def industry_delete(iid: int):
    _industry_or_403(iid)
    with connect() as db:
        db.execute("UPDATE shows SET industry_id = NULL WHERE industry_id = ?", (iid,))
        db.execute("DELETE FROM industry_companies WHERE industry_id = ?", (iid,))
        db.execute("UPDATE leads SET industry_id = NULL WHERE industry_id = ?", (iid,))
        db.execute("DELETE FROM industries WHERE id = ?", (iid,))
    flash("Industry campaign deleted.", "ok")
    return redirect(url_for("industries_page"))


@app.post("/shows/<int:show_id>/industry")
def show_industry(show_id: int):
    iid = request.form.get("industry_id", type=int)
    with connect() as db:
        db.execute("UPDATE shows SET industry_id = ? WHERE id = ?", (iid, show_id))
    return redirect(url_for("show_page", show_id=show_id))


# ---------------------------------------------------------------- opportunities
from . import opportunities as opps  # noqa: E402


@app.get("/opportunities")
def opportunities_page():
    kind = request.args.get("kind", "all")
    status = request.args.get("status", "new")
    ready_only = request.args.get("ready") == "1"
    conf = cfg.all_settings()
    where, params = ["o.status = ?"], [status if status in ("new", "actioned", "dismissed") else "new"]
    if not g.user["is_admin"] or request.args.get("who") != "all":
        where.append("(o.owner_id IS NULL OR o.owner_id = ?)")
        params.append(g.user["id"])
    if kind in opps.KINDS:
        where.append("o.kind = ?")
        params.append(kind)
    if ready_only:
        where.append("o.research_stage = 'ready'")
    with connect() as db:
        rows = db.all(
            "SELECT o.*, s.name AS show_name, s.start_date AS show_date, s.location AS show_location, "
            "i.name AS industry_name, u.name AS owner_name, u.email AS owner_email, co.id AS company_id, co.employees, "
            "co.growing, co.industry AS company_industry FROM opportunities o LEFT JOIN shows s ON s.id = o.show_id "
            "LEFT JOIN industries i ON i.id = o.industry_id LEFT JOIN users u ON u.id = o.owner_id "
            "LEFT JOIN companies co ON co.name_key = o.name_key "
            f"WHERE {' AND '.join(where)} AND (s.start_date IS NULL OR s.start_date >= ?) "
            "ORDER BY o.found_at DESC LIMIT 600", params + [date.today().isoformat()])
        mine = "(o.owner_id IS NULL OR o.owner_id = ? OR ? = 1)"
        scope = (g.user["id"], 1 if g.user["is_admin"] and request.args.get("who") == "all" else 0, date.today().isoformat())
        counts = {k: 0 for k in opps.KINDS}
        for r in db.all("SELECT o.kind, COUNT(*) AS n FROM opportunities o LEFT JOIN shows s ON s.id = o.show_id "
                        f"WHERE o.status = 'new' AND {mine} AND (s.start_date IS NULL OR s.start_date >= ?) GROUP BY o.kind",
                        scope):
            counts[r["kind"]] = r["n"]
        progress = db.one(
            "SELECT SUM(CASE WHEN o.research_stage = 'ready' THEN 1 ELSE 0 END) AS ready, "
            "SUM(CASE WHEN o.research_stage IS NULL THEN 1 ELSE 0 END) AS queued FROM opportunities o "
            f"LEFT JOIN shows s ON s.id = o.show_id WHERE o.status = 'new' AND {mine} "
            "AND (s.start_date IS NULL OR s.start_date >= ?)", scope)
        ids = [r["company_id"] for r in rows if r["company_id"] and r["research_stage"] == "ready"]
        people: dict = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for c in db.all(f"SELECT company_id, name, title FROM contacts WHERE company_id IN ({marks}) "
                            "AND email IS NOT NULL ORDER BY id", ids):
                people.setdefault(c["company_id"], []).append(c)
    for r in rows:
        r["priority"] = val.is_priority({"location": r["show_location"], "name": r["show_name"]}, conf) if r["show_id"] else False
        r["people"] = people.get(r["company_id"], [])[: conf["contacts_per_company"]]
    stage_rank = {"ready": 0, None: 1, "in_outreach": 2, "no_contacts": 3, "not_found": 4, "not_fit": 5, "teammate": 6}
    rows.sort(key=lambda r: (stage_rank.get(r["research_stage"], 2), -(r["score"] or 0), -(r["value_high"] or 0),
                             opps.KIND_RANK.get(r["kind"], 9)))
    return render_template("opportunities.html", rows=rows, kinds=opps.KINDS, kind=kind, status=status, counts=counts,
                           ready_only=ready_only, progress=progress, am_ok=amplemarket.configured(),
                           daily_limit=conf["daily_opportunity_limit"], last_scan=cfg.internal("_last_signal_scan"))


def _opp_or_403(oid: int) -> dict:
    with connect() as db:
        o = db.one("SELECT * FROM opportunities WHERE id = ?", (oid,))
    if not o:
        abort(404)
    if not g.user["is_admin"] and o["owner_id"] not in (None, g.user["id"]):
        abort(403, "A teammate has this opportunity.")
    return o


@app.get("/opportunities/<int:oid>")
def opportunity_page(oid: int):
    """Start outreach lands here: the approval queue if contacts are waiting, otherwise the company."""
    o = _opp_or_403(oid)
    with connect() as db:
        ready = db.one("SELECT COUNT(*) AS n FROM leads l JOIN companies co ON co.id = l.company_id "
                       "WHERE co.name_key = ? AND l.owner_id = ? AND l.status = 'review'", (o["name_key"], g.user["id"]))["n"]
    if ready:
        return redirect(url_for("outreach", q=o["company_name"]))
    return redirect(url_for("company_page", key=o["name_key"], opp=oid))


@app.post("/opportunities/<int:oid>/<action>")
def opportunity_action(oid: int, action: str):
    o = _opp_or_403(oid)
    if action == "dismiss":
        with connect() as db:
            db.execute("UPDATE opportunities SET status = 'dismissed', acted_at = ? WHERE id = ?", (now(), oid))
        flash(f"Dismissed {o['company_name']}.", "ok")
    elif action == "claim":
        with connect() as db:
            db.execute("UPDATE opportunities SET owner_id = ? WHERE id = ?", (g.user["id"], oid))
        flash(f"{o['company_name']} is yours.", "ok")
    elif action == "start":
        if not amplemarket.configured():
            flash("Connect Amplemarket first (AMPLEMARKET_API_KEY).", "bad")
        else:
            try:
                stage = prospecting.process_opportunity(oid, g.user["id"])
            except amplemarket.AmplemarketError as exc:
                flash(f"Amplemarket: {exc}", "bad")
                return _back(url_for("opportunities_page"))
            if stage == "queued":
                flash(f"{o['company_name']}: contacts found and openers written. They're waiting in Outreach.", "ok")
                return redirect(url_for("outreach"))
            flash(f"{o['company_name']}: {stage.replace('_', ' ')}. See the company page for why.", "warn")
    return _back(url_for("opportunities_page"))


@app.post("/opportunities/rescan")
def opportunities_rescan():
    def run():
        opps.rescan_all()
        try:
            opps.scan_signals()
        except amplemarket.AmplemarketError:
            pass
        prospecting.research_opportunities_due()
    _bg(run)
    flash("Looking for new opportunities and researching them now. Refresh in a minute.", "ok")
    return redirect(url_for("opportunities_page"))


# ---------------------------------------------------------------- industry: hand-picked companies
@app.post("/industries/<int:iid>/add-company")
def industry_add_company(iid: int):
    ind = _industry_or_403(iid)
    name = request.form.get("name", "").strip()
    website = request.form.get("website", "").strip()
    if not name and not website:
        flash("Give a company name or website.", "bad")
        return redirect(url_for("industry_page", iid=iid))
    company = prospecting._get_company(name or prospecting._domain(website), prospecting._domain(website))
    with connect() as db:
        ic = db.one("SELECT * FROM industry_companies WHERE industry_id = ? AND company_id = ?", (iid, company["id"]))
        if not ic:
            db.insert("INSERT INTO industry_companies (industry_id, company_id, found_at) VALUES (?, ?, ?)",
                      (iid, company["id"], now()))
    if request.form.get("now") == "1":
        return industry_target(iid, company["id"])
    flash(f"Added {company['name']} to {ind['name']}.", "ok")
    return redirect(url_for("industry_page", iid=iid))


@app.post("/industries/<int:iid>/target/<int:company_id>")
def industry_target(iid: int, company_id: int):
    """Hand-picked: research this company now, regardless of the daily queue."""
    ind = _industry_or_403(iid)
    if ind["owner_id"] is None:
        with connect() as db:
            db.execute("UPDATE industries SET owner_id = ? WHERE id = ?", (g.user["id"], iid))
        ind["owner_id"] = g.user["id"]
    with connect() as db:
        ic = db.one("SELECT * FROM industry_companies WHERE industry_id = ? AND company_id = ?", (iid, company_id))
        if not ic:
            db.insert("INSERT INTO industry_companies (industry_id, company_id, found_at) VALUES (?, ?, ?)",
                      (iid, company_id, now()))
            ic = db.one("SELECT * FROM industry_companies WHERE industry_id = ? AND company_id = ?", (iid, company_id))
        db.execute("UPDATE industry_companies SET stage = NULL WHERE id = ?", (ic["id"],))
        name = db.one("SELECT name FROM companies WHERE id = ?", (company_id,))["name"]
    if not amplemarket.configured():
        flash("Connect Amplemarket first (AMPLEMARKET_API_KEY).", "bad")
        return redirect(url_for("industry_page", iid=iid))
    try:
        stage = prospecting.process_industry_company(ic, ind, cfg.all_settings())
    except amplemarket.AmplemarketError as exc:
        flash(f"Amplemarket: {exc}", "bad")
        return redirect(url_for("industry_page", iid=iid))
    flash(f"{name}: " + ("contacts found and openers written. They're in Outreach." if stage == "queued"
                         else stage.replace("_", " ") + "."), "ok" if stage == "queued" else "warn")
    return _back(url_for("industry_page", iid=iid))
