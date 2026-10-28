"""Settings you can change on the Settings page, with sensible defaults for an exhibition stand builder.

Secrets (API keys, SMTP password) stay in environment variables, not here.
"""
from __future__ import annotations

import json
import os
import secrets as _secrets

from .db import connect

# key: (default, type, label, help)
FIELDS: dict[str, tuple] = {
    # --- About you (used when writing emails) ---
    "company_name": ("Skyline Whitespace", "text", "Your company", ""),
    "sender_name": ("", "text", "Signed off by", "Who the emails come from, e.g. Kai"),
    "pitch": ("", "textarea", "What you offer",
              "Two or three sentences on what you do for exhibitors. The AI builds every opening line from this."),
    "proof_points": ("", "textarea", "Proof points",
                     "Clients, shows you've built for, results, awards. One per line. Only these are ever mentioned."),
    "tone": ("Friendly, direct, British English. Short sentences. No hype, no exclamation marks.", "text",
             "Tone of voice", ""),
    "example_opener": ("", "textarea", "Example of a great opening line",
                       "Optional. The AI matches its style."),

    # --- Who to target ---
    "target_titles": ("Head of Marketing\nMarketing Director\nEvents Manager\nEvent Marketing Manager\n"
                      "Trade Show Manager\nExhibitions Manager\nField Marketing Manager\nBrand Manager\n"
                      "Marketing Manager\nCMO", "textarea", "Job titles to contact",
                      "One per line, best first."),
    "target_seniorities": ("C-Suite\nVP\nHead\nDirector\nManager", "textarea", "Seniority levels", "One per line."),
    "contacts_per_company": (2, "int", "Contacts per company", ""),
    "min_booth_sqft": (200, "int", "Smallest booth worth chasing (sq ft)",
                       "Shell-scheme sized booths below this are skipped. Only used when the size is known."),
    "ideal_booth_sqft": (400, "int", "Ideal booth size from (sq ft)", "Booths this size and up score highest."),
    "min_employees": (20, "int", "Smallest company (employees)", ""),
    "exclude_industries": ("Event Services\nEvents Services", "textarea", "Industries to skip",
                           "One per line, e.g. other stand builders."),
    "exclude_keywords": ("exhibition stand\nexhibit design\nexhibits\nstand builder\nbooth builder\n"
                         "display stand\ntrade show display\nevent fabrication", "textarea",
                         "Skip companies whose name contains",
                         "One per line. Keeps competitors out."),
    "min_score": (55, "int", "Minimum score to prepare outreach (0–100)", ""),

    # --- Timing ---
    "window_start_days": (270, "int", "Start contacting this many days before the show", ""),
    "window_end_days": (45, "int", "Stop contacting this many days before the show",
                        "Too late to design and build after this."),

    # --- Amplemarket ---
    "default_sequence_id": ("", "sequence", "Default Amplemarket sequence", "Each show can override this."),
    "accept_risky_emails": (False, "bool", "Also send to 'risky' emails (catch-all domains)", ""),
    "daily_research_limit": (60, "int", "Companies researched per day",
                             "Each one uses Amplemarket credits. Raise it once you've seen the quality."),

    # --- Alerts & briefing ---
    "big_booth_alert_sqft": (1000, "int", "Alert me when a new exhibitor's booth is at least (sq ft)", ""),
    "briefing_hour": (7, "int", "Daily briefing time (hour, UK time)", ""),
    "briefing_to": ("", "text", "Send the daily briefing to", "Email address(es), comma separated."),

    # --- Show discovery ---
    "discovery_topics": ("", "textarea", "Industries / show types to look for",
                         "One per line, e.g. medtech, construction, food & drink, defence."),
    "discovery_regions": ("UK\nEurope\nUSA", "textarea", "Regions", "One per line."),
    "discovery_enabled": (True, "bool", "Look for new shows every week", ""),
}

SECTIONS = [
    ("About you", ["company_name", "sender_name", "pitch", "proof_points", "tone", "example_opener"]),
    ("Who to target", ["target_titles", "target_seniorities", "contacts_per_company", "min_booth_sqft",
                       "ideal_booth_sqft", "min_employees", "exclude_industries", "exclude_keywords", "min_score"]),
    ("Timing", ["window_start_days", "window_end_days"]),
    ("Amplemarket", ["default_sequence_id", "accept_risky_emails", "daily_research_limit"]),
    ("Alerts & daily briefing", ["big_booth_alert_sqft", "briefing_hour", "briefing_to"]),
    ("Finding new shows", ["discovery_enabled", "discovery_topics", "discovery_regions"]),
]


def _decode(key: str, raw):
    default, kind = FIELDS[key][0], FIELDS[key][1]
    if raw is None:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = raw
    if kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if kind == "bool":
        return bool(value)
    return "" if value is None else str(value)


def get(key: str):
    with connect() as db:
        row = db.one("SELECT value FROM settings WHERE key = ?", (key,))
    return _decode(key, row["value"] if row else None)


def all_settings() -> dict:
    with connect() as db:
        rows = {r["key"]: r["value"] for r in db.all("SELECT key, value FROM settings")}
    return {k: _decode(k, rows.get(k)) for k in FIELDS}


def save(values: dict) -> None:
    with connect() as db:
        for key, value in values.items():
            if key not in FIELDS and not key.startswith("_"):
                continue
            raw = json.dumps(value)
            if db.one("SELECT key FROM settings WHERE key = ?", (key,)):
                db.execute("UPDATE settings SET value = ? WHERE key = ?", (raw, key))
            else:
                db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, raw))


def lines(value: str) -> list[str]:
    return [x.strip() for x in str(value or "").splitlines() if x.strip()]


def internal(key: str, default=None):
    """Hidden values the app keeps for itself (prefixed with _)."""
    with connect() as db:
        row = db.one("SELECT value FROM settings WHERE key = ?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default


def set_internal(key: str, value) -> None:
    save({key: value})


def webhook_token() -> str:
    token = os.environ.get("WEBHOOK_TOKEN") or internal("_webhook_token")
    if not token:
        token = _secrets.token_urlsafe(24)
        set_internal("_webhook_token", token)
    return token
