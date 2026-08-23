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

## Files

| File              | Purpose                                                          |
|-------------------|-------------------------------------------------------------------|
| `wavv_client.py`  | REST client for WAVV's `/calls` endpoints, with retry/backoff.   |
| `db.py`           | Postgres schema bootstrap, upsert, and sync-state helpers.       |
| `schema.sql`      | Table/view DDL — safe to re-run.                                 |
| `sync.py`         | CLI: `init`, `backfill`, `sync`, `summary`.                      |
| `.env.example`    | Copy to `.env` and fill in your credentials.                     |

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
