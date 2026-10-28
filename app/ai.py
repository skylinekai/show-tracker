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
COPY_SYSTEM = """You write the first lines of cold emails for an exhibition stand design-and-build company.
Each email goes to a named person at a company that is exhibiting at a specific trade show.
The rest of the email (offer, call to action, follow-ups) is already written in the sales tool; you only
write a subject line and a 1-2 sentence personalised opening line that leads naturally into the offer.

Rules:
- Use only facts given to you. Never invent clients, awards, numbers, or things about the recipient.
- Mention only proof points from the list provided, and only if they genuinely fit.
- Refer to the show by name. Mention the booth size or number only when it's given and it helps.
- Don't comment on their current or past stand design; you haven't seen it.
- Write the way a thoughtful salesperson would type it: plain, specific, no buzzwords, no flattery.
- Subject lines: 2-6 words, sentence case, no clickbait, no emojis.
- Return JSON only."""


def write_openers(settings: dict, show: dict, exhibitor: dict, company: dict, contacts: list[dict]) -> dict[int, dict]:
    """Returns {contact_id: {"subject": ..., "opener": ...}}."""
    facts = {
        "your_company": settings.get("company_name"),
        "what_you_offer": settings.get("pitch") or "Custom exhibition stand design and build.",
        "proof_points": [p for p in str(settings.get("proof_points") or "").splitlines() if p.strip()],
        "tone": settings.get("tone"),
        "example_opening_line": settings.get("example_opener") or None,
        "show": {"name": show["name"], "date": show.get("start_date"), "location": show.get("location")},
        "their_booth": {"number": exhibitor.get("booth") or None,
                        "size_sq_ft": exhibitor.get("booth_sqft") or None,
                        "dimensions": exhibitor.get("booth_dims") or None,
                        "hall": exhibitor.get("hall") or None},
        "their_company": {"name": company.get("name") or exhibitor["name"],
                          "industry": company.get("industry"),
                          "employees": company.get("employees"),
                          "what_they_do": (exhibitor.get("description") or "")[:600] or None},
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


# ---------------------------------------------------------------- show discovery
DISCOVERY_SYSTEM = """You research upcoming trade shows for an exhibition stand builder's sales team.
Find real, upcoming trade shows with a sizeable exhibition floor (ideally 100+ exhibitors) that match the
topics and regions given. Prefer shows whose public exhibitor list is online (Map Your Show, A2Z, eShow,
ExpoFP, or the organiser's own list), and give the direct link to that exhibitor list when you can find it.
Only include shows you found evidence for in your searches. Return JSON only."""


def discover_shows(topics: list[str], regions: list[str], already: list[str], today: str) -> list[dict]:
    user = (f"Today is {today}. Find up to 12 trade shows taking place between 3 and 15 months from today.\n"
            f"Topics: {', '.join(topics) or 'any B2B industry'}\nRegions: {', '.join(regions) or 'any'}\n"
            f"Skip these, we already track them: {', '.join(already[:80]) or 'none'}\n\n"
            'Return: {"shows": [{"name": "Show name and year", "start_date": "YYYY-MM-DD", '
            '"location": "City, Country", "industry": "...", "website": "https://...", '
            '"exhibitor_list_url": "https://... or null", "why": "one short line on fit"}]}')
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}]
    data = _json(_call(DISCOVERY_SYSTEM, user, max_tokens=4000, tools=tools, timeout=300))
    shows = data.get("shows", []) if isinstance(data, dict) else data
    return [s for s in shows if isinstance(s, dict) and s.get("name")]
