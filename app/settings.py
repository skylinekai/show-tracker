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
    "target_titles": ("Marketing Director\nHead of Marketing\nEvents Manager\nHead of Events\nEvent Marketing Manager\n"
                      "Trade Show Manager\nExhibitions Manager\nBrand Manager\nHead of Brand\nMarketing Manager\n"
                      "Field Marketing Manager\nMarketing Communications Manager\nCMO", "textarea", "Job titles to contact",
                      "One per line, best first."),
    "target_seniorities": ("C-Suite\nVP\nHead\nDirector\nManager", "textarea", "Seniority levels", "One per line."),
    "contacts_per_company": (3, "int", "Contacts per company", "2–3 gives more chances of a reply."),
    "priority_locations": ("London\nBirmingham\nExCeL\nOlympia\nNEC\nBusiness Design Centre\nQEII Centre\n"
                           "Tobacco Dock\nICC Birmingham", "textarea", "Priority locations",
                           "Your most profitable work. Shows here are researched first and score higher. One per line."),
    "rate_low": (800, "int", "In-house build rate from (£ per m²)", ""),
    "rate_high": (1000, "int", "In-house build rate to (£ per m²)", ""),
    "rate_outsourced": (1500, "int", "Outsourced build rate (£ per m²)", "e.g. North America."),
    "outsourced_regions": ("USA\nUnited States\nCanada\nNorth America\nLas Vegas\nOrlando\nChicago\nNew York\n"
                           "Atlanta\nBoston\nSan Diego\nSan Francisco\nLos Angeles\nAnaheim\nHouston\nDallas\n"
                           "Miami\nPhiladelphia\nWashington DC\nNashville\nNew Orleans\nDenver\nPhoenix\nSeattle\n"
                           "Toronto\nVancouver\nMontreal", "textarea", "Places you outsource (not in-house)",
                           "Shows in these places use the outsourced rate and gross profit. One per line."),
    "min_project_value": (30000, "int", "Smallest project worth chasing (£)",
                          "Judged on the company's biggest stand at any show, not just this one."),
    "gp_inhouse_pct": (60, "int", "Gross profit on in-house work (%)", ""),
    "gp_elsewhere_pct": (40, "int", "Gross profit on outsourced work (%)", "Used for the places you outsource."),
    "min_employees": (20, "int", "Smallest company (employees)", ""),
    "exclude_industries": ("Event Services\nEvents Services", "textarea", "Industries to skip",
                           "One per line, e.g. other stand builders."),
    "exclude_keywords": ("exhibition stand\nexhibit design\nexhibits\nstand builder\nbooth builder\n"
                         "display stand\ntrade show display\nevent fabrication", "textarea",
                         "Skip companies whose name contains",
                         "One per line. Keeps competitors out."),
    "min_score": (55, "int", "Minimum score to prepare outreach: shows (0–100)", ""),
    "min_score_industry": (45, "int", "Minimum score to prepare outreach: industry campaigns (0–100)",
                           "Lower, because companies found by industry often have no known stand size yet."),

    # --- Timing ---
    "window_start_days": (270, "int", "Start contacting this many days before the show", ""),
    "window_end_days": (45, "int", "Stop contacting this many days before the show",
                        "Too late to design and build after this."),

    # --- Amplemarket ---
    "default_sequence_id": ("", "sequence", "Default Amplemarket sequence", "Each show can override this."),
    "accept_risky_emails": (False, "bool", "Also send to 'risky' emails (catch-all domains)", ""),
    "daily_research_limit": (60, "int", "Companies researched per day",
                             "Each one uses Amplemarket credits. Raise it once you've seen the quality."),
    "daily_opportunity_limit": (30, "int", "Opportunities researched per day",
                                "Runs in the background so the Opportunities list always has contacts ready. Uses credits."),
    "daily_web_research_limit": (15, "int", "Deep web research per day (best accounts)",
                                 "Claude searches the web for their other shows, growth and news. A few pence each."),

    # --- Alerts & briefing ---
    "email_alerts": (False, "bool", "Send alert and briefing emails",
                     "Off: big stands, replies and new exhibitors only show in the app. Needs the SMTP settings in Render when on."),
    "big_stand_alert_sqm": (100, "int", "Alert me when a new exhibitor's stand is at least (m²)", ""),
    "briefing_time": ("07:30", "text", "Morning briefing time (UK time)",
                      "For anyone who's switched the briefing on under My details, e.g. 07:30."),
    "briefing_to": ("", "text", "Also send the team briefing to",
                    "Optional extra addresses, comma separated. Each salesperson gets their own briefing anyway "
                    "(they can switch it off under My details)."),

    # --- Show discovery ---
    "discovery_topics": ("", "textarea", "Industries / show types to look for",
                         "One per line, e.g. medtech, construction, food & drink, defence."),
    "discovery_regions": ("London\nBirmingham\nRest of UK\nEurope\nUSA\nMiddle East", "textarea", "Regions",
                          "One per line, most important first."),
    "discovery_enabled": (True, "bool", "Look for new shows every week", ""),
}

SECTIONS = [
    ("About you", ["company_name", "sender_name", "pitch", "proof_points", "tone", "example_opener"]),
    ("Project value", ["rate_low", "rate_high", "gp_inhouse_pct", "rate_outsourced", "gp_elsewhere_pct",
                       "min_project_value", "priority_locations", "outsourced_regions"]),
    ("Who to target", ["target_titles", "target_seniorities", "contacts_per_company", "min_employees",
                       "exclude_industries", "exclude_keywords", "min_score", "min_score_industry"]),
    ("Timing", ["window_start_days", "window_end_days"]),
    ("Amplemarket", ["default_sequence_id", "accept_risky_emails", "daily_research_limit", "daily_opportunity_limit",
                     "daily_web_research_limit"]),
    ("Alerts & morning briefing", ["email_alerts", "big_stand_alert_sqm", "briefing_time", "briefing_to"]),
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
