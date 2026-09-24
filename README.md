# WAVV → SQL sync

Pulls call data (outbound calls, inbound calls, talk time, conversations, dispositions, etc.)
from the WAVV dialer's public API into a Postgres database, and keeps it up to date.

## What it does

- `wavv_agents` table: one row per connected WAVV dialer, attributed to the agent who
  owns it. Each agent has their own WAVV API key and an optional NPN (National Producer
  Number) — add as many as you have agents.
- `wavv_calls` table: one row per call, mirroring WAVV's `Call` object (direction, phone
  numbers, matched CRM contact, timestamps, talk time in seconds, outcome, disposition,
  human/machine detection, notes, AI summary, whether it was recorded), tagged with the
  agent whose dialer it came from.
- `is_conversation` column: `true` when a call was answered by a human — use it to count
  real conversations vs. dials.
- Full historical backfill by default — `WAVV_BACKFILL_SINCE` defaults to `all`, so a
  fresh backfill pulls everything WAVV has, not just a recent window.
- `wavv_daily_summary` / `wavv_weekly_summary` views: daily and weekly rollups of total
  calls, conversations, answered calls, and talk time, split by direction.
- `wavv_disposition_summary` view: call counts and talk time broken down by disposition,
  per direction.
- `wavv_talktime_stats` view: avg/median/min/max/total talk time per direction.
- `wavv_agent_summary` view: total calls/conversations/talk time per connected agent.
- `wavv_sync_state` table: internal bookkeeping so incremental syncs know where they left off.

## Setup

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Get a WAVV API key for each agent/dialer** you want to track, from the WAVV
   Integrations panel. Each key is tied to a single agent's dialer.

3. **Get a Postgres connection string.** Any Postgres works (Neon, Supabase, RDS, etc.).

4. **Configure**
   ```bash
   cp .env.example .env
   # then edit .env and fill in DATABASE_URL (WAVV_API_KEY is optional now — see below)
   ```

5. **Create the schema**
   ```bash
   python sync.py init
   ```

6. **Connect your dialers, one per agent**
   ```bash
   python sync.py agents add --name "Jordan" --key "<Jordan's WAVV API key>" --npn "<optional NPN>"
   python sync.py agents add --name "Taylor" --key "<Taylor's WAVV API key>"
   python sync.py agents list                        # see everyone connected
   python sync.py agents edit --id <id> --name "New Name" --npn "12345678"
   python sync.py agents remove --id <id>
   ```
   `--npn` is optional — it's just a place to keep each agent's National
   Producer Number alongside their dialer, shown on the dashboard. Renaming an
   agent (CLI or the dashboard's "Edit" link) relabels their past calls too,
   so historical rollups always show the current name.

   (If you set `WAVV_API_KEY` in `.env` instead — the old single-key setup — it's
   auto-registered as one agent named "Default" the first time the tool runs, so
   existing installs keep working with no manual step.)

7. **Backfill history**
   ```bash
   python sync.py backfill                 # full history for every connected dialer (default)
   python sync.py backfill --since 90d     # or a specific window
   ```

8. **Keep it current** — run this on a schedule (cron, Task Scheduler, or a Cowork
   scheduled task):
   ```bash
   python sync.py sync
   ```
   Each run picks up from the last successful sync automatically, for every connected
   dialer (with a 15-minute overlap re-checked for safety — duplicates are impossible
   since rows are upserted by call `id`). One dialer's failure doesn't block the others.

9. **Quick sanity check**
   ```bash
   python sync.py summary
   python sync.py weekly           # this week vs last week, plus a weekly rollup
   python sync.py performance --period monthly --agent "Jordan"   # per-agent, any period
   python sync.py scorecard --agent "Jordan" --start 2026-01-01 --end 2026-01-31   # one slice's scorecard
   python sync.py leaderboard --period daily   # rank agents by points for today/week/month/quarter
   python sync.py dispositions
   python sync.py talktime
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

## Front-end dashboard + manual trigger

`app.py` is a small web API that sits in front of the same sync logic, for a front-end
to call:

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/health` | GET | none | liveness check |
| `/api/status` | GET | none | total rows synced, connected dialer count, last sync time |
| `/api/summary?days=14` | GET | none | daily rollup for charts/tables |
| `/api/weekly?weeks=12` | GET | none | weekly rollup, for a longer trend chart |
| `/api/weekly/compare` | GET | none | this-week-to-date vs last-week comparison + % change |
| `/api/dispositions` | GET | none | call counts/talk time by disposition, per direction |
| `/api/talktime` | GET | none | avg/median/min/max/total talk time per direction |
| `/api/agents` | GET | `X-API-Key` header | connected dialers, with masked API keys |
| `/api/agents` | POST | `X-API-Key` header | connect a new dialer: `{agent_name, api_key, base_url?}` |
| `/api/agents/<id>` | PATCH | `X-API-Key` header | rename, re-key, set NPN, or activate/deactivate a dialer (rename also relabels that agent's past calls) |
| `/api/agents/<id>` | DELETE | `X-API-Key` header | disconnect a dialer |
| `/api/agents/summary` | GET | none | per-agent call totals |
| `/api/agents/performance?period=weekly&agent_name=` | GET | none | per-agent rollup by daily/weekly/monthly/quarterly, optionally filtered to one agent |
| `/api/scorecard?agent_name=&start=&end=` | GET | none | one agent's (or everyone's) dialing scorecard over any date range: answer rate, conversation rate, talk time, top dispositions |
| `/api/leaderboard?period=` | GET | none | agents ranked by weighted points (appointments, conversations, talk time, call attempts) for the current daily/weekly/monthly/quarterly period |
| `/api/backfill` | POST | `X-API-Key` header | pull ALL historical calls for every connected dialer |
| `/api/sync` | POST | `X-API-Key` header | trigger an on-demand sync (runs in the background; poll `/api/sync/status`) |
| `/api/sync/status` | GET | none | status of the most recent manual sync/backfill job |

**Deploy it** as its own Render Web Service (separate from the `wavv-sync` cron job,
which keeps handling the reliable hourly schedule):
- Runtime: Python · Build: `pip install -r requirements.txt` · Start: `gunicorn app:app`
- Env vars: `DATABASE_URL`, `WAVV_BASE_URL` (default `https://api.wavv.com/v3`),
  `WAVV_BACKFILL_SINCE` (default `all`), plus `SYNC_API_KEY` (a random secret — the
  front-end sends this to manage dialers and trigger syncs) and `ALLOWED_ORIGIN` (your
  front-end's URL, or `*`). `WAVV_API_KEY` is optional — only needed for the legacy
  single-dialer setup described above.

**`webflow-dashboard.html`** is a self-contained, full-page dark dashboard: a fixed
left sidebar (Overview / Leaderboard / Agent performance / Dispositions / Manage
dialers) plus a top bar with "Sync now" / "Backfill" buttons. It's meant to be the
*only* content on its page, not a small embedded widget — Overview has a greeting
banner, a "needs your attention" panel for dialers with sync errors, KPI tiles
(calls today, today's leader, answer rate, total talk time), a "Production over time"
chart plus a "Where you stand" mini-leaderboard, and week-over-week/7-day trend
charts. The other tabs hold: connected-dialer management with inline rename/NPN
editing; a "Leaderboard" tab with a top-3 podium and full ranked list for
today/this week/this month/this quarter; an "Explore & scorecard" section that
slices any agent over any date range (dropdown, typed dates, or quick presets like
7D/30D/This month/This quarter/YTD); a per-agent daily/weekly/monthly/quarterly
performance table; talk-time stats; a disposition breakdown; and a daily detail table.

The easiest way to run it is to just deploy `app.py` to Render and open its root URL
(`/`) — the page auto-fills its own API base and key from the server's env vars, no
editing required. To host it yourself instead (e.g. on your own domain, or as a
Webflow page's sole content via an Embed element that fills the whole page):
1. Deploy `app.py` to Render first and grab its URL.
2. Open `webflow-dashboard.html`, fill in the two `TODO` values at the top of the
   `<script>` block: `apiBase` (your Render web service URL) and `apiKey` (your
   `SYNC_API_KEY` — this same key is reused to manage connected dialers from the page).
3. Publish it as the entire content of its own page (it renders its own sidebar/top
   bar chrome, so it shouldn't be dropped into a page that already has other
   navigation around it).
4. Note: the API key ships in the page's client-side JS, so anyone who views source
   can see it — fine for a low-stakes internal tool, but consider password-protecting
   that page if you want it locked down further.

## Files

| File                     | Purpose                                                          |
|--------------------------|-------------------------------------------------------------------|
| `wavv_client.py`         | REST client for WAVV's `/calls` endpoints, with retry/backoff.   |
| `db.py`                  | Postgres schema bootstrap, upsert, agent CRUD, and sync-state helpers. |
| `wavv_sync_core.py`      | Shared sync logic used by both the CLI and the web API, including multi-agent orchestration. |
| `schema.sql`             | Table/view DDL — safe to re-run.                                 |
| `sync.py`                | CLI: `init`, `backfill`, `sync`, `summary`, `weekly`, `dispositions`, `talktime`, `agents`, `emit-sql`. |
| `app.py`                 | Web API (Flask) for a front-end: status, summary, dialer management, manual trigger. |
| `webflow-dashboard.html` | Self-contained full-page dark dashboard (sidebar nav + top bar).  |
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
- **Multiple WAVV teams/dialers**: handled natively now — connect one dialer per agent
  with `python sync.py agents add` (or the dashboard's "Connected dialers" card), and
  every call is tagged with both `team_id` (from WAVV) and the agent it came from.
- **Leaderboard**: ranks connected agents for the current day/week/month/quarter by a
  weighted points score (appointment-set calls count most, then conversations, then
  call attempts, then a small talk-time bonus) — built entirely from call data, since
  WAVV doesn't give us appointments/texts/emails from any other system. See
  `python sync.py leaderboard --period daily` or the dashboard's "Leaderboard" card.
  Tune the weights via `LEADERBOARD_POINTS` in `wavv_sync_core.py`.
