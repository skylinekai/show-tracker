"""Small Amplemarket API client: people search, email reveal, email validation, sequences.

Docs: https://docs.amplemarket.com  (auth: `Authorization: Bearer <API key>`)
"""
from __future__ import annotations

import logging
import os
import time
from urllib.parse import urljoin

import requests

log = logging.getLogger("showtracker")

BASE = os.environ.get("AMPLEMARKET_BASE_URL", "https://api.amplemarket.com")
TIMEOUT = 60


class AmplemarketError(Exception):
    pass


def api_key() -> str:
    return os.environ.get("AMPLEMARKET_API_KEY", "")


def configured() -> bool:
    return bool(api_key())


def _request(method: str, path: str, *, json=None, params=None, retries: int = 4) -> requests.Response:
    if not configured():
        raise AmplemarketError("Amplemarket API key isn't set (AMPLEMARKET_API_KEY).")
    url = path if path.startswith("http") else urljoin(BASE + "/", path.lstrip("/"))
    for attempt in range(retries):
        try:
            r = requests.request(method, url, json=json, params=params, timeout=TIMEOUT,
                                 headers={"Authorization": f"Bearer {api_key()}", "Accept": "application/json"})
        except requests.RequestException as exc:
            if attempt == retries - 1:
                raise AmplemarketError(f"Couldn't reach Amplemarket: {exc.__class__.__name__}") from exc
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 and attempt < retries - 1:
            time.sleep(min(float(r.headers.get("Retry-After") or 5), 60))
            continue
        if r.status_code >= 500 and attempt < retries - 1:
            time.sleep(2 ** attempt)
            continue
        if r.status_code >= 400:
            raise AmplemarketError(f"Amplemarket {r.status_code} on {path}: {r.text[:300]}")
        return r
    raise AmplemarketError(f"Amplemarket kept rate-limiting {path}.")


# ---------------------------------------------------------------- sequences
def list_sequences() -> list[dict]:
    out, url = [], "/sequences"
    while url:
        data = _request("GET", url).json()
        for s in data.get("sequences") or data.get("results") or []:
            out.append({"id": str(s.get("id")), "name": s.get("name") or "", "status": s.get("status") or ""})
        nxt = ((data.get("_links") or {}).get("next") or {}).get("href")
        url = nxt if nxt and nxt != url else None
    return out


def add_leads(sequence_id: str, leads: list[dict], mailbox: str | None = None) -> dict:
    """leads: [{"email": ..., "linkedin_url": ..., "data": {...}}]  (max 250 per call).
    With a mailbox, the emails go out from that salesperson's own connected mailbox."""
    body: dict = {"leads": leads}
    if mailbox:
        body["settings"] = {"leads_distribution": "custom_mailboxes", "mailboxes": [mailbox]}
    return _request("POST", f"/sequences/{sequence_id}/leads", json=body).json()


def list_mailboxes() -> list[dict]:
    out, url, params = [], "/mailboxes", {"page[size]": 20}
    while url:
        data = _request("GET", url, params=params).json()
        params = None  # the next link already carries the paging
        for m in data.get("mailboxes") or []:
            out.append({"email": (m.get("email") or "").lower(), "status": m.get("status") or "",
                        "owner": ((m.get("user") or {}).get("email") or "").lower(),
                        "owner_name": (m.get("user") or {}).get("name") or ""})
        nxt = ((data.get("_links") or {}).get("next") or {}).get("href")
        url = nxt if nxt and nxt != url else None
    return out


# ---------------------------------------------------------------- people
def search_people(*, company_domain: str = "", company_name: str = "", titles=(), seniorities=(),
                  page_size: int = 25) -> list[dict]:
    body: dict = {"page": 1, "page_size": max(1, min(page_size, 100))}
    if company_domain:
        body["company_domains"] = [company_domain]
    elif company_name:
        body["company_names"] = [company_name]
    else:
        return []
    if titles:
        body["person_titles"] = list(titles)
    if seniorities:
        body["person_seniorities"] = list(seniorities)
    data = _request("POST", "/people/search", json=body).json()
    return data.get("results") or []


def reveal_email(person: dict) -> dict:
    """Look the person up again with email reveal switched on (uses credits)."""
    params = {"reveal_email": "true"}
    if person.get("linkedin_url"):
        params["linkedin_url"] = person["linkedin_url"]
    else:
        params.update({"name": person.get("name", ""), "title": person.get("title", ""),
                       "company_domain": (person.get("company") or {}).get("website", "")})
    data = _request("GET", "/people/find", params=params).json()
    return data.get("person") or data


def validate_emails(emails: list[str], max_wait: float = 180) -> dict[str, str]:
    """Returns {email: deliverable|risky|unknown|undeliverable}."""
    emails = [e for e in dict.fromkeys(emails) if e]
    if not emails:
        return {}
    job = _request("POST", "/email-validations", json={"emails": [{"email": e} for e in emails]}).json()
    url = ((job.get("_links") or {}).get("self") or {}).get("href") or f"/email-validations/{job.get('id')}"
    deadline = time.time() + max_wait
    data = job
    while data.get("status") in ("queued", "processing") and time.time() < deadline:
        time.sleep(5)
        data = _request("GET", url).json()
    results: dict[str, str] = {}
    while True:
        for r in data.get("results") or []:
            results[(r.get("email") or "").lower()] = r.get("result") or "unknown"
        nxt = ((data.get("_links") or {}).get("next") or {}).get("href")
        if not nxt or data.get("status") != "completed":
            break
        data = _request("GET", nxt).json()
    return {e.lower(): results.get(e.lower(), "unknown") for e in emails}


# ---------------------------------------------------------------- companies
def find_company(domain: str = "", linkedin_url: str = "") -> dict:
    params = {"domain": domain} if domain else {"linkedin_url": linkedin_url}
    if not any(params.values()):
        return {}
    data = _request("GET", "/companies/find", params=params).json()
    return data.get("company") or data


def search_companies(*, industries=(), keywords=(), locations=(), sizes=(), domains=(), growth: dict | None = None,
                     last_funding: dict | None = None, job_openings: dict | None = None,
                     page: int = 1, page_size: int = 50) -> tuple[list[dict], int]:
    """Returns (companies, total matches)."""
    body: dict = {"page": page, "page_size": max(1, min(page_size, 50))}
    if last_funding:
        body["company_last_funding"] = last_funding
    if job_openings:
        body["job_openings"] = job_openings
    for key, val in (("company_industries", industries), ("company_keywords", keywords),
                     ("company_locations", locations), ("company_sizes", sizes), ("company_domains", domains)):
        if val:
            body[key] = list(val)
    if growth:
        body["headcount_growth"] = growth
    data = _request("POST", "/companies/search", json=body).json()
    total = (data.get("_pagination") or {}).get("total") or len(data.get("results") or [])
    return data.get("results") or [], int(total)


def headcount_growth(domain: str) -> str | None:
    """'growing' (10%+ in 12 months), 'shrinking' (-10% or worse), 'steady', or None if unknown."""
    if not domain:
        return None
    try:
        up, _ = search_companies(domains=[domain], page_size=1,
                                 growth={"growth_rates": [{"min": 10, "max": 10000}], "time_frame_in_months": 12})
        if up:
            return "growing"
        down, _ = search_companies(domains=[domain], page_size=1,
                                   growth={"growth_rates": [{"min": -100, "max": -10}], "time_frame_in_months": 12})
        return "shrinking" if down else "steady"
    except AmplemarketError:
        return None


MARKETING_ROLE = ("market", "event", "brand", "exhibit", "trade show", "communications", "pr ")


def job_openings(domain: str) -> tuple[int, list[str]]:
    """(number of open roles, titles of open marketing/events roles)."""
    if not domain:
        return 0, []
    data = _request("GET", "/job-openings", params={"domain": domain, "page[size]": 100}).json()
    jobs = data.get("job_openings") or []
    titles = [j.get("title") or "" for j in jobs]
    marketing = [t for t in titles if any(k in t.lower() for k in MARKETING_ROLE)]
    return len(jobs), marketing[:6]
