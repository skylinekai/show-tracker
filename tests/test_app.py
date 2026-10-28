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
        slug = re.sub(r"\W+", "", (company_name or company_domain).lower())
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

    def add_leads(seq, leads):
        calls["added"].append((seq, leads))
        return {"leads_added": len(leads), "skipped_leads": [{"email": l["email"], "reason": "already in sequence"}
                                                             for l in leads if l["email"].startswith("ana@acme")]}

    def write_openers(conf, show, ex, company, contacts):
        return {c["id"]: {"subject": f"{show['name']} stand", "opener": f"Saw {ex['name']} is at {show['name']}."}
                for c in contacts}

    monkeypatch.setattr(amplemarket, "search_people", search_people)
    monkeypatch.setattr(amplemarket, "reveal_email", reveal_email)
    monkeypatch.setattr(amplemarket, "validate_emails", validate)
    monkeypatch.setattr(amplemarket, "add_leads", add_leads)
    monkeypatch.setattr(amplemarket, "list_sequences", lambda: [{"id": "seq-1", "name": "Show outreach", "status": "active"}])
    monkeypatch.setattr(ai, "write_openers", write_openers)
    return calls


# ---------------------------------------------------------------- scoring & pipeline
def test_pre_score_rules():
    from app import prospecting
    from app import settings as cfg

    conf = cfg.all_settings()
    s, _, skip = prospecting.pre_score({"name": "Tiny", "booth_sqft": 100, "booth": "1"}, {"start_date": _soon(120)}, conf)
    assert skip and "100 sq ft" in skip
    s, _, skip = prospecting.pre_score({"name": "ABC Exhibition Stands", "booth_sqft": 900}, {"start_date": _soon(120)}, conf)
    assert skip and "exhibition stand" in skip.lower()
    s, _, skip = prospecting.pre_score({"name": "Late Co", "booth_sqft": 900}, {"start_date": _soon(10)}, conf)
    assert skip and "too close" in skip
    s, notes, skip = prospecting.pre_score({"name": "Good", "booth_sqft": 900, "booth": "5"}, {"start_date": _soon(120)}, conf)
    assert not skip and s == 70
    s, _, _ = prospecting.pre_score({"name": "Early", "booth_sqft": 300, "booth": ""}, {"start_date": _soon(400)}, conf)
    assert s == 20 + 10 + 15  # halfway between 200 and 400 sq ft, not yet in the window


def test_send_date_waits_for_window():
    from app import prospecting
    from app import settings as cfg

    conf = cfg.all_settings()
    assert prospecting.send_date({"start_date": _soon(100)}, conf) == date.today().isoformat()
    assert prospecting.send_date({"start_date": _soon(300)}, conf) == _soon(30)


def test_full_pipeline(show, fake_apis):
    from app import prospecting
    from app import settings as cfg
    from app.db import connect

    cfg.save({"default_sequence_id": "seq-1"})
    result = prospecting.research_due(max_companies=10)
    assert result.get("queued") == 2 and result.get("skipped") == 2
    # biggest booth researched first, small booth and competitor never cost a search
    assert fake_apis["search"] == ["Bigcorp Health", "Acme Medical"]

    with connect() as db:
        leads = db.all(prospecting.LEAD_QUERY + " ORDER BY co.name, c.name")
        stages = {r["name"]: r["stage"] for r in db.all("SELECT name, stage FROM exhibitors")}
    assert stages == {"Acme Medical": "queued", "Bigcorp Health": "queued", "Tiny Co": "skipped",
                      "Display Stands Ltd": "skipped"}
    # Bigcorp's Ana is 'risky' so left out by default; the wrong-company person is filtered out
    assert [(l["company_name"], l["contact_name"]) for l in leads] == [
        ("Acme Medical", "Ana Ruiz"), ("Acme Medical", "Sam Lee"), ("Bigcorp Health", "Sam Lee")]
    assert all(l["status"] == "review" and l["opener"].startswith("Saw ") for l in leads)
    assert leads[2]["score"] == 40 + 20 + 10 + 30

    # approve -> sent, Amplemarket skip honoured
    n = prospecting.approve([l["id"] for l in leads], "kai@example.com")
    assert n == 3
    out = prospecting.send_due()
    assert out == {"sent": 2, "failed": 1}
    seq, payload = fake_apis["added"][0]
    assert seq == "seq-1"
    data = {p["email"]: p["data"] for p in payload}
    assert data["sam@bigcorphealth.com"]["show_name"] == "MedTech Expo 2027"
    assert data["sam@bigcorphealth.com"]["booth_size"] == "2,400 sq ft"
    assert data["sam@bigcorphealth.com"]["opening_line"] == "Saw Bigcorp Health is at MedTech Expo 2027."
    with connect() as db:
        statuses = {r["email"]: (r["status"], r["error"]) for r in db.all(prospecting.LEAD_QUERY)}
    assert statuses["ana@acmemedical.com"][0] == "skipped" and "already in sequence" in statuses["ana@acmemedical.com"][1]

    # research isn't repeated, and the same people aren't queued again for another show
    assert prospecting.research_due(max_companies=10) == {}


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
                                       "name": "Sam"}).status_code == 302
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
    prospecting.research_due(max_companies=10)
    client = web.create_app(start_scheduler=False).test_client()
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
