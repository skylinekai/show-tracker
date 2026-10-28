# Show Tracker · Skyline Whitespace

Your business development robot for trade shows. It:

1. **Tracks shows.** Watches each show's exhibitor list daily (Map Your Show, A2Z, eShow or any exhibitor table), flags new exhibitors and drop-outs, and pulls exact booth sizes from Map Your Show floorplans.
2. **Finds the right companies.** On shows with **Prospecting** switched on, it scores every exhibitor on booth size, company size, industry and timing, and skips poor fits and competitors before spending any credits.
3. **Finds the right people.** For the best fits, it uses Amplemarket to find decision-makers (events, marketing, trade show managers), reveal their emails and check they're deliverable.
4. **Writes the emails.** Claude writes a personalised subject and opening line for each person, mentioning the show, their booth and their company, using only the facts you give it.
5. **Waits for you.** Everything lands in **Outreach** for you to edit and approve in a click. You can set a show to send without approval.
6. **Sends at the right time.** Approved contacts go into your Amplemarket sequence when the show enters your outreach window (by default 270 to 45 days before the show). Amplemarket handles the sending, follow-ups and opt-outs.
7. **Stays on top of things.** A daily briefing email, instant alerts for replies and big booths, automatic tracking of next year's edition, and a weekly web search for new shows in your industries.

## Environment variables (Render → your service → Environment)

| Variable | What it's for | Needed? |
|---|---|---|
| `DATABASE_URL` | Where data is stored, e.g. `sqlite:////var/data/showtracker.db` with a disk mounted at `/var/data` | Yes |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | The first sign-in account (you). Add teammates later under **Users**. | Yes, for first sign-in |
| `SECRET_KEY` | Keeps people signed in across restarts. Any long random string. | Recommended |
| `AMPLEMARKET_API_KEY` | Finding people, checking emails, adding contacts to sequences | For outreach |
| `ANTHROPIC_API_KEY` | Writing opening lines and searching for new shows | For outreach |
| `APP_BASE_URL` | Your app's address, e.g. `https://show-tracker.onrender.com`, used for links in emails and Slack | Recommended |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Sending the daily briefing and alerts | For the briefing |
| `SLACK_WEBHOOK_URL` | Alerts in Slack | Optional |
| `CLAY_WEBHOOK_URL` | Also send new exhibitors to Clay, as before | Optional |
| `ADOBE_FONTS_KIT` | Your Adobe Fonts kit ID (e.g. `abc1234`) for Europa. Add your app's domain to the kit in Adobe Fonts. Without it the app uses Jost, a close free match. | For the brand font |
| `CHECK_INTERVAL_HOURS` | How often shows are re-checked (default 24) | Optional |

If you deployed the first version with `APP_PASSWORD`, it still works: the first person to sign in with any email and that password becomes the admin. After that, sign in with your email and that password.

## Setting up outreach

1. **Amplemarket API key:** in Amplemarket, open Settings and find the API section. Create a key, then add it as `AMPLEMARKET_API_KEY`.
2. **Anthropic API key:** sign up at console.anthropic.com, add billing, create a key and add it as `ANTHROPIC_API_KEY`.
3. **Create the sequence in Amplemarket.** Use these fields in the first email:
   - `{{opening_line}}`: the personalised 1–2 sentences. Start the email with this.
   - `{{subject_line}}`: the personalised subject.
   - `{{first_name}}`, `{{company_name}}`, `{{show_name}}`, `{{show_date}}`, `{{show_location}}`, `{{booth_number}}`, `{{booth_size}}`, `{{title}}`

   Activate or pause the sequence. Amplemarket won't accept contacts into a draft sequence.
4. **In the app's Settings:**
   - Fill in **What you offer** and **Proof points**. The AI never claims anything beyond these.
   - Pick the default sequence and check the targeting and timing.
   - Add your email to **Send the daily briefing to**.
5. **Replies:** copy the webhook link from **Settings → Replies from Amplemarket** into Amplemarket's reply webhook (JSON data integration). Replies then show up in **Outreach → Replied** and trigger alerts.
6. **Switch on prospecting** on a show (the switch at the top of its page). Researched contacts appear in **Outreach** over the following hours, up to your daily limit.

## Email for the briefing (SMTP)

The app can send email through most mail providers:

| Provider | `SMTP_HOST` | `SMTP_PORT` | Notes |
|---|---|---|---|
| Microsoft 365 | `smtp.office365.com` | 587 | Your IT admin may need to enable "Authenticated SMTP" for the mailbox |
| Google Workspace | `smtp.gmail.com` | 587 | Use an app password |
| Postmark, Resend, SendGrid | the provider's SMTP host | | Usually the simplest option |

Set `SMTP_USER` and `SMTP_PASSWORD` to the mailbox login, and `SMTP_FROM` to the address the emails should come from.

## Costs to keep an eye on

- **Amplemarket credits:** each researched company uses credits for email reveals and validation. **Companies researched per day** in Settings caps this (default 60). A company researched for one show is reused for the next for 120 days, at no extra cost.
- **Claude:** writing openers costs a fraction of a penny per contact. The weekly show search runs up to 10 web searches.

## Run it on your computer

```bash
pip install -r requirements.txt
ADMIN_EMAIL=you@example.com ADMIN_PASSWORD=choose-one python wsgi.py   # http://localhost:8000
```

Tests: `pip install pytest && python -m pytest -q`

## Project layout

```
app/scrapers.py     reading exhibitor lists (+ Map Your Show booth sizes)
app/tracker.py      checking shows, new / dropped exhibitors, next-edition discovery
app/prospecting.py  scoring, Amplemarket research, openers, approval, sending, replies
app/amplemarket.py  Amplemarket API
app/ai.py           Claude: opening lines and show search
app/jobs.py         the 15-minute background loop, daily briefing, weekly show search
app/notify.py       email, Slack, Clay
app/settings.py     everything on the Settings page, with defaults
app/auth.py         user accounts
app/web.py          pages
```
