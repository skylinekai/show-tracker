# Show Tracker · Skyline Whitespace

The sales team's business development robot. Each salesperson signs in and works their own shows, industries and
opportunities. The app finds the companies, works out what they're worth, finds the right people, writes the
opening lines, and feeds approved contacts into Amplemarket sequences sent from that salesperson's own mailbox.

## The three dashboards

**Shows**: target a specific show.
- Add a show by pasting its exhibitor list link (Map Your Show, A2Z, eShow, any exhibitor table) **or by
  uploading a PDF floorplan**. A PDF is read by AI; upload a newer version as the organiser updates it.
- **Start tracking** gets every exhibitor and starts prospecting straight away: the best fits are researched
  (2–3 marketing, brand and events contacts each, with checked emails) and an opening line is written for each person.
- The show is re-checked every 24 hours. You approve the first batch; after that, new exhibitors go straight
  into the sequence (switchable per show).
- Next year's edition is picked up automatically when its directory goes live (Map Your Show).

**Industries**: target a whole industry, e.g. Dental.
- Tag your dental shows to it, and Amplemarket finds dental companies that aren't at any show you track.
- **Start campaign** researches the best fits weekly within your daily limit. Pick out companies yourself with
  **Target now**, or add one by name or website.
- **Find shows** searches the web for the industry's shows: London and Birmingham first, then the rest.

**Opportunities**: a continuous list of companies to call before anyone else does. It runs in the background all
the time; nothing is emailed out.
- **First-time exhibitors**: no record at any other show you track, including the show's previous edition.
- **Bigger stands**: more space than at their last show or last year's edition, or grown since tracking began.
- **Recently funded / hiring event staff / growing fast**: likely to start or step up exhibiting.
- New opportunities are added as shows are checked and from a daily search for funding, hiring and growth signals.
- Each one is **researched in the background**, best first (score, plus 2–3 contacts with checked emails), within
  **Opportunities researched per day** (default 30). Filter by **Ready to start** to see the ones with contacts.
- **Start outreach** writes the openers and puts the contacts in your Outreach queue to approve.

Also: **Companies** (every account ranked by estimated stand spend) and **Outreach** (your approval queue,
scheduled, in sequence, replies).

## How value is worked out

- Stand size (m²) × your build rate: **£800–£1,000/m² in-house (60% GP)**, **£1,500/m² outsourced (e.g. North
  America, 40% GP)**. All of this is editable in Settings.
- Judged across the whole account: a 9 m² booth at one show still qualifies if they take 100 m² at another.
- The cut-off is **£30k** (their biggest stand anywhere). **London and Birmingham** shows are researched first and score higher.
- Company strength adds to the score: size, revenue, headcount growth, recent funding, and open marketing/events roles.
- Deep research (Claude with web search) on the best accounts finds the other shows they do, revenue trend and
  recent news, with sources.

## Working as a team

- Each person sees only their own shows, campaigns, companies, opportunities and outreach, plus anything
  "up for grabs". Managers ("Can manage users and settings") see everyone's.
- **No stepping on toes:** adding a show or industry a teammate already has is blocked with their name, and a
  company already in someone's outreach is skipped for everyone else.
- Emails go from each person's own Amplemarket mailbox, signed off with their name, title and number.
- Replies alert the person who sent the email.

## Environment variables (Render → your service → Environment)

| Variable | What it's for |
|---|---|
| `DATABASE_URL` | e.g. `sqlite:////var/data/showtracker.db` with a disk at `/var/data` |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Your sign-in. Add teammates under **Users**. (An existing `APP_PASSWORD` still works for the first sign-in.) |
| `SECRET_KEY` | Any long random string; keeps people signed in across restarts |
| `AMPLEMARKET_API_KEY` | People and company search, email checks, sending sequences |
| `ANTHROPIC_API_KEY` | Opening lines, reading PDF floorplans, deep research, finding shows |
| `APP_BASE_URL` | e.g. `https://show-tracker.onrender.com`, used for links in emails |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Only if you switch on alert emails in Settings (off by default) |
| `ADOBE_FONTS_KIT` | Your Adobe Fonts kit ID for Europa (otherwise the free lookalike Jost is used) |
| `SLACK_WEBHOOK_URL`, `CLAY_WEBHOOK_URL` | Optional |

## Setting up Amplemarket

1. Create a sequence. Start the first email with `{{opening_line}}` and use `{{subject_line}}` as the subject. Other
   fields available: `{{first_name}}`, `{{company_name}}`, `{{show_name}}`, `{{show_date}}`, `{{show_location}}`,
   `{{booth_number}}`, `{{stand_size}}`, `{{industry}}`, `{{sender_first_name}}`, `{{sender_name}}`, `{{sender_title}}`,
   `{{sender_phone}}`.
2. Set it to active (or paused). Pick it in **Settings → Amplemarket**.
3. Each person picks their own mailbox under **My details → Your outreach**.
4. Paste the webhook link from **Settings → Replies from Amplemarket** into Amplemarket's reply webhook.

## Costs to keep an eye on

- **Amplemarket credits:** used for email reveals, validation and company look-ups. **Companies researched per day**
  (default 60) caps it, and research on a company is reused for 120 days.
- **Claude:** openers cost a fraction of a penny each. Reading a PDF floorplan costs pennies. Deep research
  (default 15 a day) and show searches use web search, about 1p per search.

## Run it on your computer

```bash
pip install -r requirements.txt
ADMIN_EMAIL=you@example.com ADMIN_PASSWORD=choose-one python wsgi.py   # http://localhost:8000
python -m pytest -q                                                    # tests
```

## Project layout

```
app/scrapers.py       exhibitor lists from show platforms and PDF floorplans
app/tracker.py        daily checks, new / dropped exhibitors, stand-size history, next editions
app/prospecting.py    scoring, research, openers, approvals, sending, replies (shows, industries, opportunities)
app/opportunities.py  first-timers, bigger stands, funding / hiring / growth signals
app/value.py          stand value, gross profit, priority and outsourced locations
app/amplemarket.py    Amplemarket API
app/ai.py             Claude: openers, PDF floorplans, deep research, finding shows
app/jobs.py           background loop, opportunity research, morning briefing, weekly/daily refreshes
app/web.py, app/web_accounts.py   pages
```
