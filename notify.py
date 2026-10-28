"""Where new exhibitors go: your Clay table (same webhook your n8n flow posts to),
an optional generic webhook (n8n, Make, Zapier), and an optional Slack digest.

All are switched on by environment variables; with none set, new exhibitors simply
show up in the app's "New exhibitors" feed.
"""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("showtracker")

CLAY_WEBHOOK_URL = os.environ.get("CLAY_WEBHOOK_URL", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")


def _record(show: dict, e: dict) -> dict:
    return {
        "company_name": e["name"],
        "booth": e.get("booth") or "",
        "website": e.get("website") or "",
        "description": e.get("description") or "",
        "profile_url": e.get("profile_url") or "",
        "show_name": show["name"],
        "show_date": show.get("start_date") or "",
        "show_location": show.get("location") or "",
        "show_url": show["url"],
        "source": "showtracker",
    }


def new_exhibitors(show: dict, added: list[dict]) -> list[int]:
    """Send new exhibitors on. Returns ids that reached Clay or the webhook."""
    pushed: list[int] = []
    if CLAY_WEBHOOK_URL:
        for e in added:  # Clay webhooks take one row per request
            try:
                r = requests.post(CLAY_WEBHOOK_URL, json=_record(show, e), timeout=30)
                if r.ok:
                    pushed.append(e["id"])
                else:
                    log.warning("Clay webhook HTTP %s for %s", r.status_code, e["name"])
            except requests.RequestException as exc:
                log.warning("Clay webhook failed for %s: %s", e["name"], exc)
            time.sleep(0.2)
    if WEBHOOK_URL:
        try:
            r = requests.post(
                WEBHOOK_URL,
                json={"event": "new_exhibitors", "show": show["name"], "show_url": show["url"],
                      "exhibitors": [_record(show, e) for e in added]},
                timeout=60,
            )
            if r.ok:
                pushed.extend(e["id"] for e in added if e["id"] not in pushed)
        except requests.RequestException as exc:
            log.warning("Webhook failed: %s", exc)
    if SLACK_WEBHOOK_URL:
        names = ", ".join(e["name"] for e in added[:15]) + (f" and {len(added) - 15} more" if len(added) > 15 else "")
        link = f" <{APP_BASE_URL}/shows/{show['id']}?view=new|Open>" if APP_BASE_URL else ""
        _slack(f"*{len(added)} new exhibitor{'s' if len(added) != 1 else ''} at {show['name']}*: {names}{link}")
    return pushed


def next_edition_found(show: dict, url: str, count: int) -> None:
    _slack(f"*Next edition of {show['name']} is live* with {count} exhibitors so far. Now tracking it: {url}")
    if WEBHOOK_URL:
        try:
            requests.post(WEBHOOK_URL, json={"event": "next_edition_found", "show": show["name"],
                                             "url": url, "exhibitors": count}, timeout=30)
        except requests.RequestException:
            pass


def _slack(text: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=20)
    except requests.RequestException as exc:
        log.warning("Slack webhook failed: %s", exc)
