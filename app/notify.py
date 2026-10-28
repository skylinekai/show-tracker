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


def big_booths(show: dict, exhibitors: list[dict]) -> None:
    rows = "".join(
        f"<li><b>{_esc(e['name'])}</b>: {e['booth_sqft']:,} sq ft{(' (' + _esc(e['booth_dims']) + ')') if e.get('booth_dims') else ''}"
        f"{', booth ' + _esc(e['booth']) if e.get('booth') else ''}</li>" for e in exhibitors)
    names = ", ".join(f"{e['name']} ({e['booth_sqft']:,} sq ft)" for e in exhibitors[:8])
    _slack(f":large_green_circle: *Big booth{'s' if len(exhibitors) > 1 else ''} just added at {show['name']}*: {names}")
    path = "/shows/%s?view=new" % show["id"]
    alert_email(f"Big booth at {show['name']}: {exhibitors[0]['name']}"
                + (f" and {len(exhibitors) - 1} more" if len(exhibitors) > 1 else ""),
                f"<p>New exhibitor{'s' if len(exhibitors) > 1 else ''} with a large booth at <b>{_esc(show['name'])}</b>:</p>"
                f"<ul>{rows}</ul>" + _link(path, "Open the show"))


def hot_reply(lead: dict, label: str, snippet: str) -> None:
    who = f"{lead.get('contact_name') or lead.get('email')} ({lead.get('company_name')})"
    _slack(f":fire: *{label.replace('_', ' ').title() if label else 'New reply'}* from {who} "
           f"re {lead.get('show_name')}: _{snippet[:200]}_")
    alert_email(f"Reply from {who}",
                f"<p><b>{_esc(who)}</b> replied about <b>{_esc(lead.get('show_name') or '')}</b>"
                f"{' and was marked <b>' + _esc(label.replace('_', ' ')) + '</b>' if label else ''}.</p>"
                f"<blockquote style='border-left:3px solid #1a1a40;margin:0;padding:4px 12px;color:#444'>"
                f"{_esc(snippet[:600])}</blockquote><p>Reply from Amplemarket to keep the thread together.</p>"
                + _link("/outreach?view=replied", "See all replies"))


# ---------------------------------------------------------------- email
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER


def email_configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def send_email(to: list[str], subject: str, html: str) -> bool:
    import smtplib
    from email.message import EmailMessage

    to = [t.strip() for t in to if t and t.strip()]
    if not to or not email_configured():
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Show Tracker <{SMTP_FROM}>", ", ".join(to)
    msg.set_content("Open this email in an HTML-capable mail app.")
    msg.add_alternative(html, subtype="html")
    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            server.starttls()
        with server:
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("Email failed: %s", exc)
        return False


def alert_email(subject: str, body_html: str) -> None:
    from . import settings as cfg

    to = [t for t in str(cfg.get("briefing_to") or "").split(",") if t.strip()]
    if to:
        send_email(to, subject, wrap_email(subject, body_html))


def wrap_email(title: str, body_html: str) -> str:
    return (
        "<div style=\"font-family:'Jost',Futura,'Helvetica Neue',Arial,sans-serif;background:#f7f8fa;padding:24px\">"
        "<div style='max-width:640px;margin:0 auto;background:#fff;border:1px solid #d7d7e2;padding:28px 32px'>"
        "<div style='font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#6b6b85;margin-bottom:6px'>"
        "Skyline Whitespace · Show Tracker</div>"
        f"<h1 style='font-size:22px;font-weight:500;color:#1a1a40;margin:0 0 18px'>{_esc(title)}</h1>"
        f"<div style='color:#1a1a40;font-size:15px;line-height:1.55'>{body_html}</div></div></div>"
    )


def _esc(s) -> str:
    import html as _html

    return _html.escape(str(s or ""))


def _link(path: str, label: str) -> str:
    if not APP_BASE_URL:
        return ""
    return (f"<p style='margin-top:20px'><a href='{APP_BASE_URL}{path}' style='background:#1a1a40;color:#fff;"
            f"padding:10px 20px;border-radius:999px;text-decoration:none;display:inline-block'>{_esc(label)}</a></p>")


def _slack(text: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=20)
    except requests.RequestException as exc:
        log.warning("Slack webhook failed: %s", exc)
