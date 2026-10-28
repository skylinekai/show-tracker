# Show Tracker

Watches trade-show exhibitor lists and tells you who's new. Add a show once; the app re-checks
it every day, records every exhibitor it has ever seen, and flags the ones that weren't there
last time. New exhibitors can be sent straight to your Clay table, so your existing Clay →
Amplemarket flow keeps working.

This is stage 1 of the business-development robot. PDF floorplan upload, enrichment and
sequencing come next and plug into the same database.

## What it does

- **Tracks shows on the big platforms**
  - **Map Your Show** (`<show>.mapyourshow.com/8_0/...`). The full exhibitor list comes from one request, no 25-company limit.
  - **A2Z** (`…a2zinc.net/…/Public/Exhibitors.aspx`)
  - **eShow** (`…/exhibitor_list.cfm`)
  - **Any page with an exhibitor table**
- **Spots new exhibitors.** The first check is the baseline. After that, anyone who appears goes into the **New exhibitors** feed and, if configured, to Clay and Slack.
- **Spots drop-outs.** Exhibitors who disappear are marked "dropped out", not deleted. If a check suddenly returns far fewer names than last time, the app assumes the site misbehaved and doesn't mark anyone removed.
- **Filters out non-companies.** Rows like "Innovation Theater", "Ask an Expert Presentations" and "Guest Nation Booth" are kept aside under "Not companies". They're never sent to Clay, so you don't pay to enrich them.
- **Finds next year's show automatically (Map Your Show).** When `nab27.mapyourshow.com` is tracked, the app also checks `nab28…`. As soon as that directory goes live, it starts tracking it and tells you.
- **Shows repeat exhibitors.** The New exhibitors feed shows how many other tracked shows each company is at.
- **Exports CSV** per show (all, new, dropped) or for the whole feed.

## Run it on your computer

```bash
pip install -r requirements.txt
python wsgi.py            # open http://localhost:8000
```

Data is stored in `showtracker.db` (SQLite) in the project folder.

Tests: `pip install pytest && python -m pytest -q`

## Deploy to Render

1. Push this folder to a new GitHub repo.
2. In Render: **New + → Blueprint**, then pick the repo. `render.yaml` creates:
   - `show-tracker`: the web app, which also runs the daily checks
   - `show-tracker-db`: a Postgres database
3. Render asks for these values:
   - `APP_PASSWORD`: anyone opening the app is asked for it (any username works).
   - `CLAY_WEBHOOK_URL`: the same webhook URL your n8n "Post to Clay" step uses. Leave it blank to skip Clay.
   - `SLACK_WEBHOOK_URL`: optional, for a message whenever new exhibitors appear.
4. When the deploy finishes, open the URL and paste in your first show's exhibitor list link.

The web service needs a paid instance type so it stays awake, because the scheduler runs inside it. Free Render web services go to sleep and would stop checking.

## Settings (environment variables)

| Variable | What it does | Default |
|---|---|---|
| `DATABASE_URL` | Postgres connection string (set by the Blueprint) | local SQLite file |
| `APP_PASSWORD` | Password for the app | none (open) |
| `CHECK_INTERVAL_HOURS` | How often each show is re-checked | 24 |
| `CLAY_WEBHOOK_URL` | Sends each new exhibitor to Clay, one row per company | off |
| `WEBHOOK_URL` | Sends a batch of new exhibitors to any webhook (n8n, Make, Zapier) | off |
| `SLACK_WEBHOOK_URL` | Slack message per show when new exhibitors appear | off |
| `APP_BASE_URL` | Your app's URL, used for links in Slack messages | none |
| `RUN_SCHEDULER` | Set to `0` to turn off automatic checks | 1 |

### What gets sent to Clay

Each new exhibitor is sent as one JSON row:

```json
{
  "company_name": "Halcyon Robotics",
  "booth": "1717",
  "website": "",
  "description": "",
  "profile_url": "https://vive27.mapyourshow.com/8_0/exhibitor/exhibitor-details.cfm?exhid=1001",
  "show_name": "ViVE 27",
  "show_date": "2027-03-14",
  "show_location": "Los Angeles",
  "show_url": "https://vive27.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm",
  "source": "showtracker"
}
```

Only exhibitors found *after* a show's first check are sent. That way, adding a show with 600
existing exhibitors doesn't flood Clay. To send the full list for a brand-new show, export
the show's CSV and import it into Clay.

## Finding the right link for a show

- **Map Your Show:** open the show's exhibitor list. The link contains `/8_0/`; any page under that address works.
- **A2Z:** the exhibitor list page ends in `/Public/Exhibitors.aspx`. A floor-plan link ending `/Public/eventmap.aspx` also works; the app switches to the list.
- **eShow:** the page ends in `exhibitor_list.cfm`.
- **Anything else:** a page that shows exhibitors in a table. Pages that load exhibitors with JavaScript (many custom show sites) won't work yet. The app says so rather than guessing.

## Project layout

```
app/scrapers.py   platform detection and one scraper per platform
app/tracker.py    checking a show, diffing against last time, scheduler, next-edition discovery
app/notify.py     Clay / webhook / Slack
app/web.py        the web pages and CSV export
app/db.py         database (SQLite locally, Postgres on Render)
tests/            tests, with saved sample pages for each platform
```
