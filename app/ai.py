"""Claude API calls: personalised opening lines, and finding new shows with web search."""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger("showtracker")

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")


class AIError(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _call(system: str, user: str, *, max_tokens: int = 2000, tools: list | None = None, timeout: int = 180) -> str:
    if not configured():
        raise AIError("Anthropic API key isn't set (ANTHROPIC_API_KEY).")
    body = {"model": MODEL, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    if tools:
        body["tools"] = tools
    for attempt in range(4):
        try:
            r = requests.post(API_URL, json=body, timeout=timeout, headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            })
        except requests.RequestException as exc:
            if attempt == 3:
                raise AIError(f"Couldn't reach Claude: {exc.__class__.__name__}") from exc
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (429, 500, 529) and attempt < 3:
            time.sleep(float(r.headers.get("retry-after") or 2 ** (attempt + 1)))
            continue
        if r.status_code >= 400:
            raise AIError(f"Claude returned {r.status_code}: {r.text[:300]}")
        blocks = r.json().get("content") or []
        return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    raise AIError("Claude is busy; try again shortly.")


def _json(text: str):
    """Pull the JSON out of a reply, tolerating code fences or a sentence before it."""
    text = re.sub(r"```(?:json)?", "", text)
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    raise AIError("Claude's answer wasn't in the expected format.")


# ---------------------------------------------------------------- outreach copy
COPY_SYSTEM = """You write the first lines of cold emails for a London-based exhibition stand design-and-build company.
Each email goes to a named person at a company that exhibits at trade shows. Sometimes there is a specific
upcoming show to anchor on; sometimes the outreach is about their industry and their show programme in general.
The rest of the email (offer, call to action, follow-ups) is already written in the sales tool; you only
write a subject line and a 1-2 sentence personalised opening line that leads naturally into the offer.

Rules:
- Use only facts given to you. Never invent clients, awards, numbers, or things about the recipient.
- Mention only proof points from the list provided, and only if they genuinely fit.
- If a show is given, refer to it by name. If they also have a bigger stand at another show, you may mention
  that show instead when it's the better hook.
- Give stand sizes in square metres, and only when it helps. Never mention prices or budgets.
- Don't comment on their current or past stand design; you haven't seen it.
- Write the way a thoughtful salesperson would type it: plain, specific, no buzzwords, no flattery.
- Subject lines: 2-6 words, sentence case, no clickbait, no emojis.
- Return JSON only."""


def write_openers(settings: dict, show: dict | None, exhibitor: dict, company: dict, contacts: list[dict],
                  stands: list[dict] | None = None, industry: str | None = None) -> dict[int, dict]:
    """Returns {contact_id: {"subject": ..., "opener": ...}}."""
    from .value import sqm

    upcoming = [{"show": s["show"], "date": s.get("date"), "location": s.get("location"),
                 "stand_m2": round(s["sqm"]) if s.get("sqm") else None}
                for s in (stands or []) if s.get("upcoming") and not s.get("removed")][:8]
    facts = {
        "your_company": settings.get("company_name"),
        "what_you_offer": settings.get("pitch") or "Custom exhibition stand design and build.",
        "proof_points": [p for p in str(settings.get("proof_points") or "").splitlines() if p.strip()],
        "tone": settings.get("tone"),
        "example_opening_line": settings.get("example_opener") or None,
        "campaign": ("show" if show else "industry"),
        "industry_campaign": industry,
        "show": {"name": show["name"], "date": show.get("start_date"), "location": show.get("location")} if show else None,
        "their_stand_at_this_show": {"number": exhibitor.get("booth") or None,
                                     "size_m2": round(sqm(exhibitor.get("booth_sqft"))) if exhibitor.get("booth_sqft") else None,
                                     "hall": exhibitor.get("hall") or None} if show else None,
        "their_upcoming_shows": upcoming or None,
        "their_company": {"name": company.get("name") or exhibitor["name"],
                          "industry": company.get("industry"),
                          "employees": company.get("employees"),
                          "what_they_do": ((exhibitor.get("description") or company.get("overview") or "")[:600]) or None},
        "recipients": [{"id": c["id"], "first_name": c.get("first_name"), "title": c.get("title")} for c in contacts],
    }
    user = ("Write a subject and opening line for each recipient. Vary them so colleagues at the same company "
            "don't get identical emails, and fit each to the person's role.\n\n"
            f"FACTS:\n{json.dumps(facts, indent=1, default=str)}\n\n"
            'Return: {"emails": [{"id": <recipient id>, "subject": "...", "opener": "..."}]}')
    data = _json(_call(COPY_SYSTEM, user, max_tokens=1500))
    out = {}
    for e in data.get("emails", []) if isinstance(data, dict) else []:
        try:
            out[int(e["id"])] = {"subject": str(e.get("subject", ""))[:120].strip(),
                                 "opener": str(e.get("opener", ""))[:700].strip()}
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- company research
RESEARCH_SYSTEM = """You research companies for an exhibition stand builder's sales team, to judge how valuable
and long-term a client each could be. Use web search. Report only what you find evidence for, and say
"unknown" rather than guess. Return JSON only."""


def research_company(name: str, domain: str | None, industry: str | None, known_shows: list[str], today: str) -> dict:
    user = (f"Today is {today}. Company: {name}" + (f" ({domain})" if domain else "") +
            (f", industry: {industry}" if industry else "") + ".\n"
            f"We already know they exhibit at: {', '.join(known_shows) or 'nothing yet'}.\n\n"
            "Find:\n"
            "1. Other trade shows and exhibitions they exhibit at in the last year or coming 18 months, with stand size "
            "in square metres if published (exhibitor lists, floorplans, press releases, their news/events pages).\n"
            "2. Revenue and how it's trending (latest figures, e.g. from Companies House filings or annual reports).\n"
            "3. Headcount and whether it's growing.\n"
            "4. Funding, acquisitions, expansion, new products or markets in the last 18 months.\n"
            "5. One or two sentences on whether they look like a profitable, long-term stand client, and why.\n\n"
            'Return: {"other_shows": [{"name": "...", "date": "YYYY-MM-DD or null", "location": "...", '
            '"stand_sqm": number or null, "source": "url"}], "revenue": "e.g. £42m (FY2025)", '
            '"revenue_trend": "growing / flat / shrinking / unknown, with figures", "headcount": "...", '
            '"headcount_trend": "...", "recent_news": [{"what": "...", "when": "YYYY-MM", "source": "url"}], '
            '"verdict": "...", "sources": ["url", ...]}')
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]
    data = _json(_call(RESEARCH_SYSTEM, user, max_tokens=3000, tools=tools, timeout=300))
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- show discovery
DISCOVERY_SYSTEM = """You research upcoming trade shows for an exhibition stand builder's sales team.
Find real, upcoming trade shows with a sizeable exhibition floor (ideally 100+ exhibitors) that match the
topics and regions given. Prefer shows whose public exhibitor list is online (Map Your Show, A2Z, eShow,
ExpoFP, or the organiser's own list), and give the direct link to that exhibitor list when you can find it.
Only include shows you found evidence for in your searches. Return JSON only."""


def discover_shows(topics: list[str], regions: list[str], already: list[str], today: str) -> list[dict]:
    user = (f"Today is {today}. Find up to 12 trade shows taking place between 3 and 15 months from today.\n"
            f"Topics: {', '.join(topics) or 'any B2B industry'}\nRegions, most important first: "
            f"{', '.join(regions) or 'any'}. Our workshop is in-house for London and Birmingham venues (ExCeL, Olympia, "
            "NEC, Business Design Centre, ICC), so find those first, then the rest.\n"
            f"Skip these, we already track them: {', '.join(already[:80]) or 'none'}\n\n"
            'Return: {"shows": [{"name": "Show name and year", "start_date": "YYYY-MM-DD", '
            '"location": "City, Country", "industry": "...", "website": "https://...", '
            '"exhibitor_list_url": "https://... or null", "why": "one short line on fit"}]}')
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}]
    data = _json(_call(DISCOVERY_SYSTEM, user, max_tokens=4000, tools=tools, timeout=300))
    shows = data.get("shows", []) if isinstance(data, dict) else data
    return [s for s in shows if isinstance(s, dict) and s.get("name")]


# ---------------------------------------------------------------- PDF floorplans
FLOORPLAN_SYSTEM = """You read trade show floorplans and exhibitor lists (PDFs) and list every exhibiting company.
Include each company once, exactly as named on the plan. Leave out anything that isn't an exhibiting company:
theatres, stages, cafés, lounges, registration, toilets, entrances, "available"/"reserved" stands, show features,
and the organiser's own areas. If a stand size is printed (e.g. "6x4", "24m²", "24 sqm") give it in square metres;
if only the stand outline is drawn with a scale, leave the size blank rather than guess."""


def extract_floorplan(pdf: bytes, filename: str = "") -> list[dict]:
    """Returns [{"name", "booth", "sqm", "hall"}] read from a floorplan or exhibitor-list PDF."""
    import base64

    if not configured():
        raise AIError("Anthropic API key isn't set (ANTHROPIC_API_KEY), so PDFs can't be read yet.")
    if len(pdf) > 32 * 1024 * 1024:
        raise AIError("That PDF is over 32 MB. Try exporting a smaller version, or split it.")
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                        "data": base64.b64encode(pdf).decode()}},
        {"type": "text", "text": (
            f"File: {filename}\nList every exhibiting company, one per line, as tab-separated columns:\n"
            "company name<TAB>stand number<TAB>stand size in m² (number only, or blank)<TAB>hall (or blank)\n"
            "No header, no numbering, no commentary. After the last company write END on its own line.")},
    ]
    text = _stream(FLOORPLAN_SYSTEM, content, max_tokens=60000)
    rows, seen = [], set()
    for line in text.splitlines():
        line = line.strip().strip("|")
        if not line or line == "END":
            continue
        parts = [p.strip() for p in (line.split("\t") if "\t" in line else line.split("|"))]
        name = parts[0].strip("-•* ").strip()
        if not name or name.lower() in ("company name", "company") or name.lower() in seen:
            continue
        seen.add(name.lower())
        area = None
        if len(parts) > 2 and parts[2]:
            m = re.search(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)", parts[2])
            try:
                area = float(m.group(1)) * float(m.group(2)) if m else float(re.sub(r"[^\d.]", "", parts[2]) or 0) or None
            except ValueError:
                area = None
        rows.append({"name": name[:200], "booth": parts[1][:60] if len(parts) > 1 else "",
                     "sqm": area, "hall": parts[3][:120] if len(parts) > 3 else ""})
    if not rows:
        raise AIError("Couldn't find any exhibitors in that PDF. Is it the floorplan or exhibitor list?")
    return rows


def _stream(system: str, content: list, max_tokens: int) -> str:
    """Streaming call, needed for long answers like a 600-exhibitor list."""
    body = {"model": MODEL, "max_tokens": max_tokens, "system": system, "stream": True,
            "messages": [{"role": "user", "content": content}]}
    for attempt in range(3):
        try:
            with requests.post(API_URL, json=body, stream=True, timeout=(30, 900), headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                "content-type": "application/json"}) as r:
                if r.status_code in (429, 500, 529) and attempt < 2:
                    time.sleep(float(r.headers.get("retry-after") or 5 * (attempt + 1)))
                    continue
                if r.status_code >= 400:
                    raise AIError(f"Claude returned {r.status_code}: {r.text[:300]}")
                out = []
                for raw in r.iter_lines(decode_unicode=True):
                    if not raw or not raw.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(raw[5:].strip())
                    except ValueError:
                        continue
                    if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                        out.append(ev["delta"]["text"])
                    elif ev.get("type") == "error":
                        raise AIError(f"Claude stopped: {ev.get('error', {}).get('message', 'unknown error')}")
                return "".join(out)
        except requests.RequestException as exc:
            if attempt == 2:
                raise AIError(f"Couldn't reach Claude: {exc.__class__.__name__}") from exc
            time.sleep(3)
    raise AIError("Claude is busy; try again shortly.")
