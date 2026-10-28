"""Login, outreach pipeline (Amplemarket and Claude mocked), webhooks, briefing, discovery and every page."""
import re
from datetime import date, timedelta

import pytest


def _soon(days):
    return (date.today() + timedelta(days=days)).isoformat()


def _rows(names, **extra):
    return [dict(name=n, booth=str(100 + i), description="", website="", profile_url="",
                 booth_sqft=extra.get("sqft", {}).get(n), booth_dims="", hall="") for i, n in enumerate(names)]


@pytest.fixture
def show(monkeypatch):
    """A show 120 days out with 4 exhibitors, prospecting on."""
    from app import notify, tracker
    from app.db import connect

    monkeypatch.setattr(notify, "new_exhibitors", lambda *a: [])
    sid = tracker.add_show("https://x.org/2027/exhibitor_list.cfm", name="MedTech Expo 2027",
                           start_date=_soon(120), location="Birmingham")
    sizes = {"Acme Medical": 800, "Tiny Co": 100, "Display Stands Ltd": 600, "Bigcorp Health": 2400}
    tracker.check_show(sid, lambda url, p=None: _rows(list(sizes), sqft={"sqft": sizes}["sqft"]))
    with connect() as db:
        db.execute("UPDATE shows SET prospecting = 1 WHERE id = ?", (sid,))
    return sid


@pytest.fixture
def fake_apis(monkeypatch):
    from app import ai, amplemarket

    monkeypatch.setenv("AMPLEMARKET_API_KEY", "am-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = {"search": [], "added": [], "validate": []}

    def search_people(company_domain="", company_name="", titles=(), seniorities=(), page_size=25):
        calls["search"].append(company_name or company_domain)
        slug = re.sub(r"\W+", "", (company_name or company_domain.split(".")[0]).lower())
        return [
            {"id": "p1", "name": "Sam Lee", "first_name": "Sam", "last_name": "Lee", "title": "Events Manager",
             "linkedin_url": f"https://linkedin.com/in/sam-{slug}",
             "company": {"name": company_name, "website": f"https://www.{slug}.com", "industry": "Medical Devices",
                         "estimated_number_of_employees": 450, "size": "201-500 employees"}},
            {"id": "p2", "name": "Ana Ruiz", "first_name": "Ana", "last_name": "Ruiz", "title": "Head of Marketing",
             "linkedin_url": f"https://linkedin.com/in/ana-{slug}", "email": f"ana@{slug}.com",
             "company": {"name": company_name, "website": f"https://www.{slug}.com"}},
            {"id": "p3", "name": "Wrong Co Person", "title": "Head of Marketing",
             "linkedin_url": "https://linkedin.com/in/x", "company": {"name": "Somebody Else Entirely"}},
        ]

    def reveal_email(person):
        slug = person["linkedin_url"].rsplit("-", 1)[-1]
        return {"email": f"{person['first_name'].lower()}@{slug}.com"}

    def validate(emails, max_wait=180):
        calls["validate"].append(emails)
        return {e.lower(): ("risky" if e.startswith("ana@bigcorp") else "deliverable") for e in emails}

    def add_leads(seq, leads, mailbox=None):
        calls["added"].append((seq, leads))
        return {"leads_added": len(leads), "skipped_leads": [{"email": l["email"], "reason": "already in sequence"}
                                                             for l in leads if l["email"].startswith("ana@acme")]}

    def write_openers(conf, show, ex, company, contacts, stands=None, industry=None):
        where = show["name"] if show else industry
        calls.setdefault("openers", []).append({"show": show and show["name"], "industry": industry,
                                                "stands": [s["show"] for s in stands or []]})
        return {c["id"]: {"subject": f"{where} stand", "opener": f"Saw {ex['name']} is at {where}."}
                for c in contacts}

    def find_company(domain="", linkedin_url=""):
        big = domain.startswith("bigcorp")
        return {"name": domain, "estimated_number_of_employees": 450 if big else 120, "revenue": 250_000_000 if big else None,
                "latest_funding_date": "2026-05-01" if big else None, "total_funding": 40_000_000 if big else None}

    monkeypatch.setattr(amplemarket, "search_people", search_people)
    monkeypatch.setattr(amplemarket, "reveal_email", reveal_email)
    monkeypatch.setattr(amplemarket, "validate_emails", validate)
    monkeypatch.setattr(amplemarket, "add_leads", add_leads)
    monkeypatch.setattr(amplemarket, "list_sequences", lambda: [{"id": "seq-1", "name": "Show outreach", "status": "active"}])
    monkeypatch.setattr(ai, "write_openers", write_openers)
    monkeypatch.setattr(amplemarket, "find_company", find_company)
    monkeypatch.setattr(amplemarket, "headcount_growth", lambda d: "growing" if d.startswith("bigcorp") else "steady")
    monkeypatch.setattr(amplemarket, "job_openings", lambda d: (12, ["Events Manager"]) if d.startswith("bigcorp") else (2, []))
    return calls


# ---------------------------------------------------------------- scoring & pipeline
def test_pre_score_rules():
    from app import prospecting
    from app import settings as cfg

    conf = cfg.all_settings()
    s, _, skip = prospecting.pre_score({"name": "ABC Exhibition Stands"}, {"start_date": _soon(120)}, conf)
    assert skip and "exhibition stand" in skip.lower()
    s, _, skip = prospecting.pre_score({"name": "Late Co"}, {"start_date": _soon(10)}, conf)
    assert skip and "too close" in skip
    # unknown stand size, London (in-house), inside the window
    s, notes, skip = prospecting.pre_score({"name": "Good"}, {"start_date": _soon(120), "location": "ExCeL London"}, conf)
    assert not skip and s == 20 + 15 + 10
    # abroad and not yet in the window
    s, _, _ = prospecting.pre_score({"name": "Early"}, {"start_date": _soon(400), "location": "Dubai"}, conf)
    assert s == 20 + 5 + 7


def test_value_model():
    from app import settings as cfg
    from app import value

    conf = cfg.all_settings()
    assert value.sqm(1076.39) == 100.0
    assert value.stand_value(36, conf) == (28800, 36000)
    assert value.money_range((28800, 36000)) == "£29k–£36k"
    assert value.is_priority({"location": "NEC, Birmingham"}, conf)
    assert value.is_priority({"name": "Dentistry Show London 2027"}, conf)
    assert not value.is_priority({"location": "Dubai World Trade Centre"}, conf)
    assert value.gross_profit(100000, False, conf) == 60000 and value.gross_profit(100000, True, conf) == 40000
    assert value.is_outsourced({"location": "Las Vegas"}, conf) and value.stand_value(100, conf, True) == (150000, 150000)


def test_small_booth_here_big_stand_elsewhere(show, fake_apis):
    """A 9 m² booth at one show still qualifies if the company takes 100 m² somewhere else."""
    from app import notify, prospecting, tracker, value
    from app import settings as cfg
    from app.db import connect

    other = tracker.add_show("https://y.org/2027/exhibitor_list.cfm", name="Healthcare Expo London 2027",
                             start_date=_soon(200), location="ExCeL London")
    tracker.check_show(other, lambda url, p=None: _rows(["Tiny Co"], sqft={"Tiny Co": 1100}))
    acct = value.account("tiny", cfg.all_settings())
    assert acct["show_count"] == 2 and acct["biggest"]["show"] == "Healthcare Expo London 2027"
    assert round(acct["biggest"]["sqm"]) == 102 and acct["any_priority"]
    with connect() as db:
        tiny = db.one("SELECT * FROM exhibitors WHERE name = 'Tiny Co' AND show_id = ?", (show,))
        s = db.one("SELECT * FROM shows WHERE id = ?", (show,))
    score, notes, skip = prospecting.pre_score(tiny, s, cfg.all_settings())
    assert not skip and score == 40 + 5 + 15 + 10
    assert any("Healthcare Expo London 2027" in n for n in notes)


def test_full_pipeline(show, fake_apis):
    from app import prospecting
    from app import settings as cfg
    from app.db import connect

    cfg.save({"default_sequence_id": "seq-1"})
    result = prospecting.research_due(max_companies=10)
    assert result.get("queued") == 2 and result.get("skipped") == 2
    # biggest stand researched first; the tiny booth and the competitor never cost a search
    assert fake_apis["search"] == ["Bigcorp Health", "Acme Medical"]

    with connect() as db:
        leads = db.all(prospecting.LEAD_QUERY + " ORDER BY co.name, c.name")
        ex = {r["name"]: r for r in db.all("SELECT name, stage, score_notes FROM exhibitors")}
        big = db.one("SELECT * FROM companies WHERE name = 'Bigcorp Health'")
    assert {k: v["stage"] for k, v in ex.items()} == {"Acme Medical": "queued", "Bigcorp Health": "queued",
                                                       "Tiny Co": "skipped", "Display Stands Ltd": "skipped"}
    assert "9 m²" in ex["Tiny Co"]["score_notes"]
    # company profile, growth and hiring were stored
    assert big["employees"] == 450 and big["revenue_usd"] == 250_000_000 and big["growing"] == "growing"
    assert "Events Manager" in big["marketing_roles"] and big["funding_date"] == "2026-05-01"
    # Bigcorp's Ana is 'risky' so left out by default; the wrong-company person is filtered out
    assert [(l["company_name"], l["contact_name"]) for l in leads] == [
        ("Acme Medical", "Ana Ruiz"), ("Acme Medical", "Sam Lee"), ("Bigcorp Health", "Sam Lee")]
    assert all(l["status"] == "review" and l["opener"].startswith("Saw ") for l in leads)
    assert leads[2]["score"] == 40 + 15 + 10 + 25   # stand, Birmingham, timing, strong company
    assert leads[0]["score"] == 40 + 15 + 10 + 10

    n = prospecting.approve([l["id"] for l in leads], "kai@example.com")
    assert n == 3
    out = prospecting.send_due()
    assert out == {"sent": 2, "failed": 1}
    seq, payload = fake_apis["added"][0]
    assert seq == "seq-1"
    data = {p["email"]: p["data"] for p in payload}
    assert data["sam@bigcorphealth.com"]["show_name"] == "MedTech Expo 2027"
    assert data["sam@bigcorphealth.com"]["stand_size"] == "223 m²"
    assert data["sam@bigcorphealth.com"]["opening_line"] == "Saw Bigcorp Health is at MedTech Expo 2027."
    with connect() as db:
        statuses = {r["email"]: (r["status"], r["error"]) for r in db.all(prospecting.LEAD_QUERY)}
    assert statuses["ana@acmemedical.com"][0] == "skipped" and "already in sequence" in statuses["ana@acmemedical.com"][1]

    # research isn't repeated, and the same people aren't queued again for another show
    assert prospecting.research_due(max_companies=10) == {}


def test_send_date_waits_for_window():
    from app import prospecting
    from app import settings as cfg

    conf = cfg.all_settings()
    assert prospecting.send_date({"start_date": _soon(100)}, conf) == date.today().isoformat()
    assert prospecting.send_date({"start_date": _soon(300)}, conf) == _soon(30)


def test_auto_send_and_window(show, fake_apis):
    from app import prospecting
    from app import settings as cfg
    from app.db import connect

    cfg.save({"default_sequence_id": "seq-1"})
    with connect() as db:
        db.execute("UPDATE shows SET auto_send = 1, start_date = ? WHERE id = ?", (_soon(330), show))
    prospecting.research_due(max_companies=10)
    with connect() as db:
        leads = db.all("SELECT status, send_after FROM leads")
    assert leads and all(l["status"] == "approved" and l["send_after"] == _soon(60) for l in leads)
    assert prospecting.send_due() == {"sent": 0, "failed": 0}  # waits for the window


def test_reply_webhook_and_alert(show, fake_apis, monkeypatch):
    from app import notify, prospecting, web
    from app import settings as cfg
    from app.db import connect

    alerts = []
    monkeypatch.setattr(notify, "hot_reply", lambda lead, label, snip: alerts.append((lead["email"], label, snip)))
    cfg.save({"default_sequence_id": "seq-1"})
    prospecting.research_due(max_companies=10)
    with connect() as db:
        ids = [r["id"] for r in db.all("SELECT id FROM leads")]
    prospecting.approve(ids, "kai")
    prospecting.send_due()

    client = web.create_app(start_scheduler=False).test_client()
    token = cfg.webhook_token()
    assert client.post("/webhooks/amplemarket?token=wrong", json={}).status_code == 403
    r = client.post(f"/webhooks/amplemarket?token={token}", json={
        "is_reply": True, "from": "Sam Lee <sam@bigcorphealth.com>", "labels": ["Interested"],
        "body": "Hi, yes we need a new stand for MedTech, can you call me?", "dynamic_fields": {}})
    assert r.get_json()["matched"] is True
    assert alerts == [("sam@bigcorphealth.com", "interested", "Hi, yes we need a new stand for MedTech, can you call me?")]
    with connect() as db:
        lead = db.one(prospecting.LEAD_QUERY + " WHERE c.email = 'sam@bigcorphealth.com'")
    assert lead["status"] == "replied" and lead["reply_label"] == "interested"


def test_big_booth_alert(monkeypatch):
    from app import notify, tracker

    seen = []
    monkeypatch.setattr(notify, "new_exhibitors", lambda *a: [])
    monkeypatch.setattr(notify, "big_booths", lambda show, ex: seen.append([e["name"] for e in ex]))
    sid = tracker.add_show("https://x.org/a/exhibitor_list.cfm", name="Show A")
    lists = iter([_rows(["One"]), _rows(["One", "Huge Co", "Small Co"], sqft={"Huge Co": 3000, "Small Co": 100})])
    tracker.check_show(sid, lambda url, p=None: next(lists))
    tracker.check_show(sid, lambda url, p=None: next(lists))
    assert seen == [["Huge Co"]]


def test_mys_booth_join():
    from app.scrapers import join_mys_booths, parse_mys_booths

    data = {"COLUMNS": ["OBJECTTYPE", "EXHID", "BOOTHDISPLAY", "FEATUREPROPERTIES"],
            "DATA": [["booth", 1001, "C123", '{"properties": {"area": 400, "boothWidth": 240, "boothHeight": 240}}'],
                     ["booth", 1001, "C125", {"properties": {"boothWidth": 120, "boothHeight": 120}}],
                     ["label", None, "", {}]]}
    booths = parse_mys_booths(data, "C")
    rows = [{"_exhid": "1001", "name": "A"}, {"_exhid": "9", "name": "B"}]
    join_mys_booths(rows, booths, {"C": "Central Hall"})
    assert rows[0]["booth_sqft"] == 500 and rows[0]["booth_dims"] == "20×20 ft (+1)" and rows[0]["hall"] == "Central Hall"
    assert "booth_sqft" not in rows[1]


def test_briefing_and_discovery(show, fake_apis, monkeypatch):
    from app import ai, jobs
    from app import settings as cfg
    from app.db import connect

    monkeypatch.setattr(ai, "discover_shows", lambda topics, regions, already, today: [
        {"name": "Arab Health 2027", "start_date": "2027-01-25", "location": "Dubai", "industry": "Healthcare",
         "website": "https://arabhealth.example", "exhibitor_list_url": "https://arabhealth27.mapyourshow.com/8_0/",
         "why": "Huge medtech floor"},
        {"name": "MedTech Expo 2027"}])  # already tracked under that name: skipped
    cfg.save({"discovery_topics": "medtech"})
    assert jobs.maybe_discover(force=True) == 1
    assert jobs.maybe_discover() == 0  # weekly
    with connect() as db:
        d = db.one("SELECT * FROM discovered_shows")
    assert d["platform"] == "mapyourshow" and d["location"] == "Dubai"

    from app import prospecting
    prospecting.research_due(max_companies=10)
    subject, body = jobs.build_briefing()
    assert "to approve" in subject and "Arab Health 2027" in body


# ---------------------------------------------------------------- web
def _login(client, email="kai@skylinewhitespace.com", password="correct horse"):
    return client.post("/login", data={"email": email, "password": password})


def _csrf(client):
    page = client.get("/me").get_data(as_text=True)
    return re.search(r'name="_csrf" value="([^"]+)"', page).group(1)


def test_login_flow(monkeypatch):
    from app import auth, web

    monkeypatch.setenv("ADMIN_EMAIL", "kai@skylinewhitespace.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse")
    client = web.create_app(start_scheduler=False).test_client()
    assert client.get("/").status_code == 302 and "/login" in client.get("/").headers["Location"]
    page = client.get("/login").get_data(as_text=True)
    assert "logo.png" in page and "Sign in" in page
    assert "match" in _login(client, password="nope").get_data(as_text=True)
    r = _login(client)
    assert r.status_code == 302
    assert "Show Tracker" in client.get("/").get_data(as_text=True)
    # POST without csrf is refused
    assert client.post("/shows", data={"url": "https://x.org/exh_list.cfm"}).status_code == 400
    token = _csrf(client)
    assert client.post("/users", data={"_csrf": token, "email": "sam@skylinewhitespace.com", "password": "abcdefgh1",
                                       "name": "Sam Lee", "job_title": "Account Manager", "phone": "+44 1473 000000"}).status_code == 302
    # missing job title is refused
    client.post("/users", data={"_csrf": token, "email": "x@skylinewhitespace.com", "password": "abcdefgh1", "name": "X"})
    page = client.get("/users").get_data(as_text=True)
    assert "Sam Lee" in page and "Account Manager" in page and "Never signed in" in page and "x@skyline" not in page
    sam = [u for u in auth.list_users() if u["email"].startswith("sam")][0]
    client.post(f"/users/{sam['id']}/edit", data={"_csrf": token, "name": "Sam Lee", "job_title": "Sales Lead",
                                                  "phone": "1", "email": "sam@skylinewhitespace.com"})
    assert auth.get_user(sam["id"])["job_title"] == "Sales Lead"
    client.post(f"/users/{sam['id']}/password", data={"_csrf": token, "password": "short"})
    client.post("/me", data={"_csrf": token, "form": "details", "name": "Kai", "job_title": "Sales & Marketing Director",
                             "phone": "+44"})
    assert "Sales &amp; Marketing Director" in client.get("/me").get_data(as_text=True)
    client.post("/me", data={"_csrf": token, "form": "password", "current": "correct horse", "new": "newpass99",
                             "again": "newpass99"})
    assert auth.authenticate("kai@skylinewhitespace.com", "newpass99")
    assert len(auth.list_users()) == 2
    client.post("/logout", data={"_csrf": token})
    assert client.get("/").status_code == 302


def test_legacy_password_becomes_admin(monkeypatch):
    from app import auth, web

    monkeypatch.setenv("APP_PASSWORD", "oldpass")
    client = web.create_app(start_scheduler=False).test_client()
    assert _login(client, "kai@skylinewhitespace.com", "oldpass").status_code == 302
    assert auth.list_users()[0]["is_admin"] == 1
    client2 = web.create_app(start_scheduler=False).test_client()
    assert _login(client2, "someone@else.com", "oldpass").status_code == 200  # only works before any account exists


def test_every_page_renders(show, fake_apis, monkeypatch):
    from app import prospecting, web
    from app import settings as cfg

    monkeypatch.setenv("ADMIN_EMAIL", "kai@skylinewhitespace.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse")
    monkeypatch.setattr(web, "_bg", lambda fn, *a: None)
    cfg.save({"default_sequence_id": "seq-1", "pitch": "We design and build stands."})
    client = web.create_app(start_scheduler=False).test_client()
    from app.db import connect
    with connect() as db:
        db.execute("UPDATE shows SET owner_id = 1 WHERE id = ?", (show,))
    prospecting.research_due(max_companies=10)
    _login(client)
    for path in ["/", f"/shows/{show}", f"/shows/{show}?view=queued", f"/shows/{show}?view=skipped&sort=score",
                 "/new", "/outreach", "/outreach?view=scheduled", "/outreach?view=sent", "/outreach?view=replied",
                 "/outreach?view=other", f"/outreach?show={show}", "/discover", "/settings", "/users", "/me",
                 "/briefing", f"/export.csv?show={show}", "/export.csv"]:
        r = client.get(path)
        assert r.status_code == 200, path
    page = client.get("/outreach").get_data(as_text=True)
    assert "Bigcorp Health" in page and "Saw Bigcorp Health" in page and "Approve selected" in page

    token = _csrf(client)
    lead_ids = re.findall(r'name="lead" value="(\d+)"', page)
    r = client.post(f"/leads/{lead_ids[0]}/edit", data={"_csrf": token, "subject": "New subject", "opener": "Edited."},
                    headers={"X-Requested-With": "fetch"})
    assert r.get_json() == {"ok": True}
    client.post("/outreach/decide", data={"_csrf": token, "action": "approve", "lead": lead_ids})
    assert "Edited." in client.get("/outreach?view=scheduled").get_data(as_text=True)

    r = client.post("/settings", data={"_csrf": token, "min_score": "70", "pitch": "Stands that work.",
                                       "default_sequence_id": "seq-1"})
    assert r.status_code == 302 and cfg.get("min_score") == 70 and cfg.get("accept_risky_emails") is False
    assert "Show outreach" in client.get("/settings").get_data(as_text=True)


def test_salespeople_have_their_own_workspace(show, fake_apis, monkeypatch):
    from app import auth, prospecting, web
    from app import settings as cfg
    from app.db import connect

    monkeypatch.setenv("ADMIN_EMAIL", "kai@skylinewhitespace.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse")
    monkeypatch.setattr(web, "_bg", lambda fn, *a: None)
    app = web.create_app(start_scheduler=False)
    hugo = auth.create_user("hugo@skylinewhitespace.com", "hugopass1", "Hugo White", False, "BDM", "1")
    adam = auth.create_user("adam@skylinewhitespace.com", "adampass1", "Adam King", False, "AM", "1")
    auth.update_outreach(hugo, mailbox="hugo@skylinewhitespace.com", sequence_id="seq-hugo", briefing=True)
    with connect() as db:
        db.execute("UPDATE shows SET owner_id = ? WHERE id = ?", (hugo, show))
    cfg.save({"default_sequence_id": "seq-1"})
    prospecting.research_due(max_companies=10)
    with connect() as db:
        leads = db.all("SELECT id, owner_id, sequence_id FROM leads")
    assert leads and all(l["owner_id"] == hugo and l["sequence_id"] == "seq-hugo" for l in leads)

    h, a = app.test_client(), app.test_client()
    _login(h, "hugo@skylinewhitespace.com", "hugopass1")
    _login(a, "adam@skylinewhitespace.com", "adampass1")
    assert "Bigcorp Health" in h.get("/outreach").get_data(as_text=True)
    assert "Bigcorp Health" not in a.get("/outreach").get_data(as_text=True)
    assert "MedTech Expo 2027" in a.get("/?who=all").get_data(as_text=True)      # everyone can see the show list
    assert a.get("/settings").status_code == 403 and a.get("/users").status_code == 403
    # Adam can't approve or edit Hugo's leads, or change his show
    page = h.get("/outreach").get_data(as_text=True)
    ids = re.findall(r'name="lead" value="(\d+)"', page)
    a_token = _csrf(a)
    a.post("/outreach/decide", data={"_csrf": a_token, "action": "approve", "lead": ids})
    assert a.post(f"/leads/{ids[0]}/edit", data={"_csrf": a_token, "subject": "x", "opener": "y"}).status_code == 403
    assert a.post(f"/shows/{show}/prospecting", data={"_csrf": a_token, "prospecting": "0"}).status_code == 403
    with connect() as db:
        assert db.one("SELECT COUNT(*) AS n FROM leads WHERE status = 'approved'")["n"] == 0
    # Hugo approves; sending goes from his mailbox
    h.post("/outreach/decide", data={"_csrf": _csrf(h), "action": "approve", "lead": ids})
    sent_with = []
    from app import amplemarket
    monkeypatch.setattr(amplemarket, "add_leads", lambda seq, leads, mailbox=None: sent_with.append((seq, mailbox)) or {})
    prospecting.send_due()
    assert sent_with == [("seq-hugo", "hugo@skylinewhitespace.com")]
    # personal briefing only covers Hugo's work; the team version shows everyone
    from app import jobs
    _, body = jobs.build_briefing(auth.get_user(adam))
    assert "Bigcorp" not in body
    _, body = jobs.build_briefing(None)
    assert "Hugo White" in body
    # removing Hugo hands his show to the manager doing it
    k = app.test_client(); _login(k)
    k.post(f"/users/{hugo}/delete", data={"_csrf": _csrf(k)})
    with connect() as db:
        assert db.one("SELECT owner_id FROM shows WHERE id = ?", (show,))["owner_id"] == 1


def test_industry_campaign(fake_apis, monkeypatch):
    from app import amplemarket, prospecting, tracker
    from app import settings as cfg
    from app.db import connect, now

    found = [{"name": "Smile Labs", "website": "https://smilelabs.com", "estimated_number_of_employees": 300,
              "industry": "Medical Devices"},
             {"name": "Ortho Kit Ltd", "website": "https://orthokit.com", "estimated_number_of_employees": 80}]
    monkeypatch.setattr(amplemarket, "search_companies",
                        lambda **kw: (found if kw.get("page") == 1 else [], 2))
    with connect() as db:
        iid = db.insert("INSERT INTO industries (name, keywords, locations, owner_id, prospecting, created_at) "
                        "VALUES ('Dental', 'dental', 'United Kingdom', NULL, 1, ?)", (now(),))
    assert prospecting.refresh_industry(iid) == 2
    # Smile Labs is also exhibiting at a tracked London show, so its outreach mentions it
    sid = tracker.add_show("https://z.org/2027/exhibitor_list.cfm", name="Dentistry Show London 2027",
                           start_date=_soon(150), location="ExCeL London")
    tracker.check_show(sid, lambda url, p=None: _rows(["Smile Labs"], sqft={"Smile Labs": 600}))
    cfg.save({"default_sequence_id": "seq-1"})
    out = prospecting.research_due(max_companies=10)
    assert out.get("queued") == 2
    with connect() as db:
        leads = db.all(prospecting.LEAD_QUERY + " ORDER BY co.name")
    by_co = {l["company_name"]: l for l in leads}
    assert by_co["Smile Labs"]["show_name"] == "Dentistry Show London 2027" and by_co["Smile Labs"]["industry_name"] == "Dental"
    assert by_co["Ortho Kit Ltd"]["show_id"] is None and by_co["Ortho Kit Ltd"]["industry_name"] == "Dental"
    assert {o["industry"] for o in fake_apis["openers"]} == {"Dental"}
    assert prospecting.lead_data(by_co["Ortho Kit Ltd"])["industry"] == "Dental"


# ---------------------------------------------------------------- PDF floorplans
def test_pdf_floorplan_show(fake_apis, monkeypatch):
    from app import ai, notify, tracker, web
    from app.db import connect

    monkeypatch.setenv("ADMIN_EMAIL", "kai@skylinewhitespace.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse")
    monkeypatch.setattr(notify, "new_exhibitors", lambda *a: [])
    plans = iter([
        [{"name": "Smile Labs", "booth": "D10", "sqm": 36.0, "hall": "Hall 1"},
         {"name": "Ortho Kit Ltd", "booth": "D12", "sqm": None, "hall": ""}],
        [{"name": "Smile Labs", "booth": "D10", "sqm": 36.0, "hall": "Hall 1"},
         {"name": "Ortho Kit Ltd", "booth": "D12", "sqm": None, "hall": ""},
         {"name": "Brite Dental", "booth": "E1", "sqm": 60.0, "hall": "Hall 2"}],
    ])
    reads = []
    monkeypatch.setattr(ai, "extract_floorplan", lambda pdf, name="": reads.append(name) or next(plans))
    ran = []
    monkeypatch.setattr(web, "_run_in_background", lambda sid: ran.append(sid) or tracker.check_show(sid))
    client = web.create_app(start_scheduler=False).test_client()
    _login(client)
    token = _csrf(client)
    import io
    r = client.post("/shows", data={"_csrf": token, "source": "pdf", "name": "Dentistry Show 2027",
                                    "location": "NEC Birmingham", "start_date": _soon(150),
                                    "floorplan": (io.BytesIO(b"%PDF-1.7 fake"), "plan-v1.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    sid = ran[0]
    with connect() as db:
        show = db.one("SELECT * FROM shows WHERE id = ?", (sid,))
        ex = {e["name"]: e for e in db.all("SELECT * FROM exhibitors WHERE show_id = ?", (sid,))}
    assert show["platform"] == "pdf" and show["prospecting"] == 1 and show["owner_id"] == 1
    assert set(ex) == {"Smile Labs", "Ortho Kit Ltd"} and ex["Smile Labs"]["booth_sqft"] == 388
    page = client.get(f"/shows/{sid}").get_data(as_text=True)
    assert "Upload newer floorplan" in page and "plan-v1.pdf" in page
    # a newer floorplan: Brite Dental is new; the old file isn't read again
    client.post(f"/shows/{sid}/floorplan", data={"_csrf": token, "floorplan": (io.BytesIO(b"%PDF-1.7 v2"), "plan-v2.pdf")},
                content_type="multipart/form-data")
    with connect() as db:
        new = db.all("SELECT name FROM exhibitors WHERE show_id = ? AND in_first_snapshot = 0", (sid,))
    assert [n["name"] for n in new] == ["Brite Dental"] and reads == ["plan-v1.pdf", "plan-v2.pdf"]
    # not a PDF: refused
    r = client.post("/shows", data={"_csrf": token, "name": "X", "floorplan": (io.BytesIO(b"hello"), "x.pdf")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "isn&#39;t a PDF" in r.get_data(as_text=True)


# ---------------------------------------------------------------- opportunities
def test_first_timers_and_upsizers(monkeypatch):
    from app import notify, opportunities, tracker
    from app.db import connect

    monkeypatch.setattr(notify, "new_exhibitors", lambda *a: [])
    last = tracker.add_show("https://a.org/2026/exhibitor_list.cfm", name="Dental Expo 2026", start_date=_soon(-200))
    tracker.check_show(last, lambda url, p=None: _rows(["Old Faithful", "Grower"], sqft={"Old Faithful": 200, "Grower": 100}))
    this = tracker.add_show("https://a.org/2027/exhibitor_list.cfm", name="Dental Expo 2027", start_date=_soon(160),
                            location="ExCeL London")
    tracker.check_show(this, lambda url, p=None: _rows(["Old Faithful", "Grower", "Newbie"],
                                                       sqft={"Old Faithful": 200, "Grower": 400, "Newbie": 300}))
    with connect() as db:
        opps = {(o["kind"], o["company_name"]): o for o in db.all("SELECT * FROM opportunities")}
    assert ("first_time", "Newbie") in opps and "previous edition" in opps[("first_time", "Newbie")]["detail"]
    assert ("upsized", "Grower") in opps and "Dental Expo 2026" in opps[("upsized", "Grower")]["detail"]
    assert ("first_time", "Old Faithful") not in opps and ("upsized", "Old Faithful") not in opps
    assert opps[("upsized", "Grower")]["value_low"] == int(round(400 / 10.7639, 1) * 800)
    # a stand that grows between checks is caught too
    tracker.check_show(this, lambda url, p=None: _rows(["Old Faithful", "Grower", "Newbie"],
                                                       sqft={"Old Faithful": 200, "Grower": 400, "Newbie": 700}))
    with connect() as db:
        grew = db.one("SELECT * FROM opportunities WHERE kind = 'upsized' AND company_name = 'Newbie'")
    assert grew and "since we started tracking" in grew["detail"] and "28 m² to 65 m²" in grew["detail"]
    assert opportunities.series_key("NAB Show 2027") == opportunities.series_key("NAB Show 26")


def test_opportunities_researched_in_background(fake_apis, monkeypatch):
    from app import auth, notify, prospecting, tracker, web
    from app.db import connect

    monkeypatch.setattr(notify, "new_exhibitors", lambda *a: [])
    a = auth.create_user("kai@skylinewhitespace.com", "kaipass11", "Kai McKechnie", True, "Director", "1")
    b = auth.create_user("hugo@skylinewhitespace.com", "hugopass1", "Hugo White", False, "BDM", "1")
    sid = tracker.add_show("https://a.org/x/exhibitor_list.cfm", name="Healthcare Show 2027", start_date=_soon(150),
                           location="Olympia London")
    tracker.check_show(sid, lambda url, p=None: _rows(["Acme Medical", "Bigcorp Health"],
                                                      sqft={"Acme Medical": 600, "Bigcorp Health": 2400}))
    with connect() as db:
        assert db.one("SELECT COUNT(*) AS n FROM opportunities WHERE research_stage IS NULL")["n"] == 2
    assert prospecting.research_opportunities_due(limit=1) == 1      # best first, within the daily limit
    with connect() as db:
        done = db.all("SELECT * FROM opportunities WHERE research_stage IS NOT NULL")
    assert [o["company_name"] for o in done] == ["Bigcorp Health"]
    assert done[0]["research_stage"] == "ready" and done[0]["contacts_found"] and done[0]["score"]
    assert prospecting.research_opportunities_due(limit=1) == 0      # today's budget used
    with connect() as db:
        assert db.one("SELECT COUNT(*) AS n FROM leads")["n"] == 0   # nothing queued or sent by the background
    # the dashboard shows it ready, with the people
    app = web.create_app(start_scheduler=False)
    c = app.test_client()
    _login(c, "kai@skylinewhitespace.com", "kaipass11")
    page = c.get("/opportunities?ready=1").get_data(as_text=True)
    assert "Bigcorp Health" in page and "Sam Lee" in page and "Acme Medical" not in page
    # Start outreach reuses the research and fills the approval queue
    assert prospecting.process_opportunity(done[0]["id"], a) == "queued"
    with connect() as db:
        leads = db.all("SELECT owner_id, status FROM leads")
        assert db.one("SELECT research_stage FROM opportunities WHERE id = ?", (done[0]["id"],))["research_stage"] == "started"
    assert leads and all(l["owner_id"] == a and l["status"] == "review" for l in leads)
    assert prospecting.teammate_on("bigcorp health", b)              # Hugo can't be given it


def test_overlaps_are_blocked(monkeypatch):
    from app import auth, web
    from app.db import connect

    monkeypatch.setenv("ADMIN_EMAIL", "kai@skylinewhitespace.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse")
    monkeypatch.setattr(web, "_run_in_background", lambda sid: None)
    app = web.create_app(start_scheduler=False)
    auth.create_user("hugo@skylinewhitespace.com", "hugopass1", "Hugo White", False, "BDM", "1")
    auth.create_user("adam@skylinewhitespace.com", "adampass1", "Adam King", False, "AM", "1")
    h, a = app.test_client(), app.test_client()
    _login(h, "hugo@skylinewhitespace.com", "hugopass1")
    _login(a, "adam@skylinewhitespace.com", "adampass1")
    h.post("/shows", data={"_csrf": _csrf(h), "url": "https://dental27.mapyourshow.com/8_0/", "name": "Dentistry Show 2027"})
    r = a.post("/shows", data={"_csrf": _csrf(a), "url": "https://dental27.mapyourshow.com/8_0/", "name": "Anything"},
               follow_redirects=True)
    assert "already being worked by Hugo White" in r.get_data(as_text=True)
    r = a.post("/shows", data={"_csrf": _csrf(a), "url": "https://other.org/list", "name": "Dentistry Show 2027"},
               follow_redirects=True)
    assert "already being worked by Hugo White" in r.get_data(as_text=True)
    with connect() as db:
        assert db.one("SELECT COUNT(*) AS n FROM shows")["n"] == 1
        sid = db.one("SELECT id FROM shows")["id"]
    assert a.get(f"/shows/{sid}").status_code == 302                       # can't open Hugo's show
    a.get("/")                                                              # (clears the notice)
    assert "Dentistry Show 2027" not in a.get("/").get_data(as_text=True)    # and doesn't see it
    h.post("/industries", data={"_csrf": _csrf(h), "name": "Dental", "keywords": "dental\northodontic"})
    r = a.post("/industries", data={"_csrf": _csrf(a), "name": "Dentistry", "keywords": "dentists"}, follow_redirects=True)
    assert "Hugo White is already running a Dental campaign" in r.get_data(as_text=True)
    r = a.post("/industries", data={"_csrf": _csrf(a), "name": "Oral care", "keywords": "orthodontic\ndental implants"},
               follow_redirects=True)
    assert "already running" in r.get_data(as_text=True)
    a.post("/industries", data={"_csrf": _csrf(a), "name": "Food & Drink", "keywords": "food\nbeverage"})
    with connect() as db:
        assert {r["name"] for r in db.all("SELECT name FROM industries")} == {"Dental", "Food & Drink"}


def test_every_new_page_renders(show, fake_apis, monkeypatch):
    from app import opportunities, prospecting, web
    from app import settings as cfg
    from app.db import connect, now

    monkeypatch.setenv("ADMIN_EMAIL", "kai@skylinewhitespace.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse")
    monkeypatch.setattr(web, "_bg", lambda fn, *a: None)
    client = web.create_app(start_scheduler=False).test_client()
    with connect() as db:
        db.execute("UPDATE shows SET owner_id = 1 WHERE id = ?", (show,))
        iid = db.insert("INSERT INTO industries (name, keywords, locations, owner_id, prospecting, created_at) "
                        "VALUES ('Medtech', 'medical', 'United Kingdom', 1, 1, ?)", (now(),))
        db.execute("UPDATE shows SET industry_id = ? WHERE id = ?", (iid, show))
    cfg.save({"default_sequence_id": "seq-1"})
    prospecting.research_due(max_companies=10)
    opportunities.scan_show(show)
    _login(client)
    for path in ["/industries", f"/industries/{iid}", "/companies", "/companies?only=all", "/companies?only=priority",
                 "/companies/bigcorp health", "/opportunities", "/opportunities?kind=first_time",
                 "/opportunities?status=dismissed", f"/shows/{show}", "/outreach?who=all", "/settings", "/me"]:
        r = client.get(path)
        assert r.status_code == 200, path
    page = client.get("/companies?only=all").get_data(as_text=True)
    assert "Bigcorp Health" in page and "£" in page
    assert "223 m²" in client.get("/companies/bigcorp health").get_data(as_text=True)
    assert "First-time" in client.get("/opportunities").get_data(as_text=True)


def test_first_timer_withdrawn_when_seen_elsewhere(monkeypatch):
    from app import notify, tracker
    from app.db import connect

    monkeypatch.setattr(notify, "new_exhibitors", lambda *a: [])
    a = tracker.add_show("https://a.org/x/exhibitor_list.cfm", name="Show A 2027", start_date=_soon(100))
    tracker.check_show(a, lambda url, p=None: _rows(["Newco"], sqft={"Newco": 300}))
    with connect() as db:
        assert db.one("SELECT COUNT(*) AS n FROM opportunities WHERE kind = 'first_time'")["n"] == 1
    b = tracker.add_show("https://b.org/x/exhibitor_list.cfm", name="Show B 2027", start_date=_soon(200))
    tracker.check_show(b, lambda url, p=None: _rows(["Newco"], sqft={"Newco": 300}))
    with connect() as db:
        assert db.one("SELECT COUNT(*) AS n FROM opportunities WHERE kind = 'first_time'")["n"] == 0


def test_next_edition_not_duplicated(monkeypatch):
    from app import notify, tracker
    from app.db import connect

    monkeypatch.setattr(notify, "new_exhibitors", lambda *a: [])
    mine = tracker.add_show("https://dentistry.example/2027/exhibitors", name="Dentistry Show 2027", start_date=_soon(150))
    old = tracker.add_show("https://dentistry26.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm",
                           name="Dentistry Show 2026", start_date=_soon(-200))
    tracker.check_show(old, lambda url, p=None: _rows(["Acme"]))
    with connect() as db:
        names = [r["name"] for r in db.all("SELECT name FROM shows ORDER BY id")]
        parent = db.one("SELECT parent_show_id FROM shows WHERE id = ?", (mine,))["parent_show_id"]
    assert names == ["Dentistry Show 2027", "Dentistry Show 2026"] and parent == old


def test_alert_emails_off_by_default(monkeypatch):
    from app import jobs, notify, settings as cfg

    sent = []
    monkeypatch.setattr(notify, "send_email", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(notify, "email_configured", lambda: True)
    cfg.save({"briefing_to": "kai@skylinewhitespace.com"})
    notify.alert_email("Big booth", "<p>x</p>", ["kai@skylinewhitespace.com"])
    assert jobs.maybe_send_briefing(force=True) == 0 and not sent
    cfg.save({"email_alerts": True})
    notify.alert_email("Big booth", "<p>x</p>", ["kai@skylinewhitespace.com"])
    assert len(sent) == 1
