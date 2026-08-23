# WAVV → SQL sync

Pulls call data (outbound calls, inbound calls, talk time, conversations, dispositions, etc.)
from the WAVV dialer's public API into a Postgres database, and keeps it up to date.

## What it does

- `wavv_calls` table: one row per call, mirroring WAVV's `Call` object (direction, phone
  numbers, matched CRM contact, timestamps, talk time in seconds, outcome, disposition,
  human/machine detection, notes, AI summary, whether it was recorded).
- `is_conversation` column: `true` when a call was answered by a human — use it to count
  real conversations vs. dials.
- `wavv_daily_summary` view: daily rollup of total calls, conversations, answered calls,
  and talk time, split by direction. Good starting point for dashboards/reporting.
- `wavv_sync_state` table: internal bookkeeping so incremental syncs know where they left off.

## Setup

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Get a WAVV API key** from the WAVV Integrations panel (team-scoped — one key per team).

3. **Get a Postgres connection string.** Any Postgres works (Neon, Supabase, RDS, etc.).

4. **Configure**
   ```bash
   cp .env.example .env
   # then edit .env and fill in WAVV_API_KEY and DATABASE_URL
   ```

5. **Create the schema**
   ```bash
   python sync.py init
   ```

6. **Backfill history**
   ```bash
   python sync.py backfill --since 90d     # or --since 2026-01-01
   ```

7. **Keep it current** — run this on a schedule (cron, Task Scheduler, or a Cowork
   scheduled task):
   ```bash
   python sync.py sync
   ```
   Each run picks up from the last successful sync automatically (with a 15-minute
   overlap re-checked for safety — duplicates are impossible since rows are upserted
   by call `id`).

8. **Quick sanity check**
   ```bash
   python sync.py summary
   ```

## Example queries once data is flowing

```sql
-- Conversations per rep's campaign, last 7 days
SELECT campaign_id, direction, count(*) AS conversations
FROM wavv_calls
WHERE is_conversation AND started_at > now() - interval '7 days'
GROUP BY 1, 2;

-- Total talk time today
SELECT direction, sum(seconds) AS talk_seconds
FROM wavv_calls
WHERE started_at >= date_trunc('day', now())
GROUP BY 1;

-- Contact-level call history (needs your CRM's contact id)
SELECT * FROM wavv_calls WHERE contact_id = 'ghl-99213' ORDER BY started_at DESC;
```

## Front-end dashboard + manual trigger (Webflow or anywhere else)

`app.py` is a small web API that sits in front of the same sync logic, for a front-end
to call:

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/health` | GET | none | liveness check |
| `/api/status` | GET | none | total rows synced, last sync time |
| `/api/summary?days=14` | GET | none | daily rollup for charts/tables |
| `/api/sync` | POST | `X-API-Key` header | trigger an on-demand sync (runs in the background; poll `/api/sync/status`) |
| `/api/sync/status` | GET | none | status of the most recent manual sync job |

**Deploy it** as its own Render Web Service (separate from the `wavv-sync` cron job,
which keeps handling the reliable hourly schedule):
- Runtime: Python · Build: `pip install -r requirements.txt` · Start: `gunicorn app:app`
- Env vars: same as the cron job (`WAVV_API_KEY`, `WAVV_BASE_URL`, `DATABASE_URL`,
  `WAVV_BACKFILL_SINCE`), plus `SYNC_API_KEY` (a random secret — the front-end sends
  this to trigger a sync) and `ALLOWED_ORIGIN` (your front-end's URL, or `*`).

**`webflow-dashboard.html`** is a self-contained dashboard (stat tiles, a 7-day
inbound/outbound chart, a daily detail table, and a "Sync now" button) meant to be
pasted into a Webflow **Embed** element:
1. Deploy `app.py` to Render first and grab its URL.
2. Open `webflow-dashboard.html`, fill in the two `TODO` values at the top of the
   `<script>` block: `apiBase` (your Render web service URL) and `apiKey` (your
   `SYNC_API_KEY`).
3. In Webflow: add an **Embed** element to a page, paste the whole file's contents in,
   publish.
4. Note: the API key ships in the page's client-side JS, so anyone who views source
   can see it — fine for a low-stakes internal tool, but consider Webflow's page
   password-protection (paid plans) if you want it locked down further.

## Files

| File                     | Purpose                                                          |
|--------------------------|-------------------------------------------------------------------|
| `wavv_client.py`         | REST client for WAVV's `/calls` endpoints, with retry/backoff.   |
| `db.py`                  | Postgres schema bootstrap, upsert, and sync-state helpers.       |
| `wavv_sync_core.py`      | Shared sync logic used by both the CLI and the web API.          |
| `schema.sql`             | Table/view DDL — safe to re-run.                                 |
| `sync.py`                | CLI: `init`, `backfill`, `sync`, `summary`, `emit-sql`.          |
| `app.py`                 | Web API (Flask) for a front-end: status, summary, manual trigger.|
| `webflow-dashboard.html` | Paste-in dashboard for a Webflow Embed element.                  |
| `.env.example`           | Copy to `.env` and fill in your credentials.                     |

## Notes / things you may want to extend later

- **Recordings & transcripts**: `wavv_client.py` already has `get_recording()` and
  `get_transcript()` methods (WAVV's `GET /calls/{id}/recording` and
  `GET /calls/{id}/transcript`). They aren't pulled automatically today — recording
  URLs expire after 72 hours and transcripts populate asynchronously, so a separate
  "backfill transcripts for calls missing a summary" job is the cleaner way to add them
  if you want that data in the database too.
- **Real-time instead of polling**: WAVV also supports webhooks (`call.started`,
  `call.incoming`, `call.ended`, `call.recorded`) for instant updates instead of polling.
  That needs a small always-on HTTP endpoint with a public URL, which is a bigger step
  than this polling-based tool — worth doing later if near-real-time matters.
- **Multiple WAVV teams**: API keys are team-scoped. To pull from more than one WAVV
  team/account, run this tool once per team (separate `.env` / `WAVV_API_KEY`), pointed
  at the same database — rows won't collide since `team_id` is stored on every call.
