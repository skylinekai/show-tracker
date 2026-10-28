import json
import os
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db()
    yield


# ---------------------------------------------------------------- parsers
def test_platform_detection():
    from app.scrapers import detect_platform

    assert detect_platform("https://nab27.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm") == "mapyourshow"
    assert detect_platform("https://directory.conexpoconagg.com/8_0/explore/exhibitor-gallery.cfm") == "mapyourshow"
    assert detect_platform("https://s36.a2zinc.net/clients/SME/FABTECH2026/Public/Exhibitors.aspx") == "a2z"
    assert detect_platform("https://meetings.ausa.org/annual/2026/exhibitor_exhibitor_list.cfm") == "eshow"
    assert detect_platform("https://2027.eshg.org/exhibition") == "generic"


def test_mys_parse():
    from app.scrapers import parse_mys_json

    rows = parse_mys_json(json.loads((FIX / "mys.json").read_text()), "https://vive27.mapyourshow.com/8_0")
    names = [r["name"] for r in rows]
    assert names[0] == "IMO - Intelligent Medical Objects"
    assert rows[0]["booth"] == "2341"  # 'randomstring' stripped
    assert rows[1]["booth"] == "1180, 1182"
    assert rows[0]["profile_url"].endswith("exhibitor-details.cfm?exhid=1001")


def test_mys_next_edition():
    from app.scrapers import mys_next_edition_url

    assert mys_next_edition_url("https://nab27.mapyourshow.com/8_0/x").startswith("https://nab28.mapyourshow.com/8_0/")
    assert mys_next_edition_url("https://clean2025.mapyourshow.com/8_0/").startswith("https://clean2026.mapyourshow.com")
    assert mys_next_edition_url("https://directory.conexpoconagg.com/8_0/") is None


def test_a2z_parse_and_url():
    from app.scrapers import a2z_list_url, parse_a2z_html

    assert a2z_list_url("https://s36.a2zinc.net/clients/SME/FABTECH2026/Public/eventmap.aspx").endswith(
        "/Public/Exhibitors.aspx?Index=All")
    rows = parse_a2z_html((FIX / "a2z.html").read_text(), "https://s36.a2zinc.net/clients/SME/X/Public/Exhibitors.aspx")
    assert [r["name"] for r in rows] == ["DSR Performance", "Thomas Scientific, LLC", "Petit Auto"]
    assert rows[1]["booth"] == "C330, C332"
    assert rows[0]["profile_url"].startswith("https://s36.a2zinc.net/clients/SME/X/Public/eBooth.aspx")


def test_eshow_and_generic_parse():
    from app.scrapers import parse_eshow_html, parse_generic_html

    rows = parse_eshow_html((FIX / "eshow.html").read_text(), "https://x.org/2026/exhibitor_list.cfm")
    assert [(r["name"], r["booth"]) for r in rows] == [("Rovertown", "612"), ("Nlight, Inc.", "714")]
    rows = parse_generic_html((FIX / "generic.html").read_text(), "https://2027.eshg.org/exhibition")
    assert [r["name"] for r in rows] == ["10x Genomics", "Agilent Technologies", "MDPI"]
    assert rows[0]["booth"] == "538" and rows[0]["website"] == "https://www.10xgenomics.com"


def test_name_key_and_junk():
    from app.scrapers import looks_like_junk, name_key

    assert name_key("Acme, Inc.") == name_key("ACME Inc") == name_key("The Acme Company")
    assert looks_like_junk("Innovation Theater")
    assert looks_like_junk("AAOS Ask an Expert Presentations")
    assert looks_like_junk("Guest Nation Booth")
    assert not looks_like_junk("OrthoDome")
    assert not looks_like_junk("Stagecoach Logistics")  # 'stage' only as a whole word


# ---------------------------------------------------------------- tracking
def _fake(lists):
    """A scraper that returns the next list each time it's called for the main show."""
    calls = iter(lists)

    def scraper(url, platform=None):
        if "28." in url:  # next-edition probe: not live
            raise RuntimeError("not live")
        return [dict(name=n, booth="", description="", website="", profile_url="") for n in next(calls)]

    return scraper


def test_first_check_is_baseline_then_diffs(monkeypatch):
    from app import notify, tracker
    from app.db import connect

    sent = []
    monkeypatch.setattr(notify, "new_exhibitors", lambda show, added: sent.append([a["name"] for a in added]) or [])
    sid = tracker.add_show("https://vive27.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm", name="ViVE 27")
    scraper = _fake([
        ["IMO", "OrthoDome", "Innovation Theater"],
        ["IMO", "OrthoDome", "Innovation Theater", "DSR Performance", "Tetraflon USA LLC"],
        ["IMO", "DSR Performance", "Tetraflon USA LLC", "Kara Co"],
    ])

    r1 = tracker.check_show(sid, scraper)
    assert r1["first_check"] and r1["added"] == 0 and sent == []

    r2 = tracker.check_show(sid, scraper)
    assert r2["added"] == 2 and sent == [["DSR Performance", "Tetraflon USA LLC"]]

    r3 = tracker.check_show(sid, scraper)
    assert r3["added"] == 1 and r3["removed"] == 2  # OrthoDome and Innovation Theater gone
    with connect() as db:
        gone = {r["name"] for r in db.all("SELECT name FROM exhibitors WHERE removed_at IS NOT NULL")}
        junk = {r["name"] for r in db.all("SELECT name FROM exhibitors WHERE likely_junk = 1")}
    assert gone == {"OrthoDome", "Innovation Theater"} and junk == {"Innovation Theater"}


def test_partial_result_does_not_mark_removals(monkeypatch):
    from app import notify, tracker

    monkeypatch.setattr(notify, "new_exhibitors", lambda *a: [])
    sid = tracker.add_show("https://x.org/2026/exhibitor_list.cfm", name="X")
    names = [f"Company {i}" for i in range(20)]
    scraper = _fake([names, names[:3]])
    tracker.check_show(sid, scraper)
    r = tracker.check_show(sid, scraper)
    assert r["removed"] == 0 and "not marking" in r["warning"]


def test_next_edition_auto_tracked(monkeypatch):
    from app import notify, tracker
    from app.db import connect

    monkeypatch.setattr(notify, "new_exhibitors", lambda *a: [])
    monkeypatch.setattr(notify, "next_edition_found", lambda *a: None)

    def scraper(url, platform=None):
        return [dict(name="Acme", booth="1", description="", website="", profile_url="")]

    sid = tracker.add_show("https://nab27.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm", name="NAB Show 2027")
    tracker.check_show(sid, scraper)
    with connect() as db:
        child = db.one("SELECT * FROM shows WHERE parent_show_id = ?", (sid,))
    assert child and child["name"] == "NAB Show 2028" and "nab28.mapyourshow.com" in child["url"]


def test_clay_payload(monkeypatch):
    from app import notify

    posted = []

    class R:
        ok = True
        status_code = 200

    monkeypatch.setattr(notify, "CLAY_WEBHOOK_URL", "https://api.clay.com/v3/sources/webhook/x")
    monkeypatch.setattr(notify.requests, "post", lambda url, json, timeout: posted.append(json) or R())
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    show = {"id": 1, "name": "ViVE 27", "url": "u", "start_date": "2027-03-14", "location": "LA"}
    ids = notify.new_exhibitors(show, [{"id": 5, "name": "IMO", "booth": "2341"}])
    assert ids == [5]
    assert posted[0]["company_name"] == "IMO" and posted[0]["show_name"] == "ViVE 27" and posted[0]["booth"] == "2341"


# ---------------------------------------------------------------- web
def test_pages_render(monkeypatch):
    from app import notify, tracker, web

    monkeypatch.setattr(notify, "new_exhibitors", lambda *a: [])
    monkeypatch.setattr(web, "_run_in_background", lambda sid: None)
    client = web.create_app(start_scheduler=False).test_client()

    assert client.get("/").status_code == 200
    resp = client.post("/shows", data={"url": "s36.a2zinc.net/clients/SME/FABTECH2026/Public/Exhibitors.aspx",
                                       "name": "FABTECH 2026", "start_date": "2026-11-10", "location": "Chicago"})
    assert resp.status_code == 302
    sid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    scraper = _fake([["DSR Performance", "Petit Auto"], ["DSR Performance", "Petit Auto", "Rovertown"]])
    tracker.check_show(sid, scraper)
    tracker.check_show(sid, scraper)

    page = client.get("/").get_data(as_text=True)
    assert "FABTECH 2026" in page and "+1" in page
    for view in ("all", "new", "removed", "junk"):
        assert client.get(f"/shows/{sid}?view={view}").status_code == 200
    assert "Rovertown" in client.get(f"/shows/{sid}?view=new").get_data(as_text=True)
    assert "Rovertown" in client.get("/new").get_data(as_text=True)
    csv = client.get(f"/export.csv?show={sid}").get_data(as_text=True)
    assert csv.splitlines()[0].startswith("company,booth") and "Petit Auto" in csv
    assert client.get("/export.csv?days=7").status_code == 200


def test_password(monkeypatch):
    from app import web

    monkeypatch.setattr(web, "APP_PASSWORD", "s3cret")
    client = web.create_app(start_scheduler=False).test_client()
    assert client.get("/").status_code == 401
    assert client.get("/healthz").status_code == 200
    import base64

    ok = client.get("/", headers={"Authorization": "Basic " + base64.b64encode(b"me:s3cret").decode()})
    assert ok.status_code == 200
