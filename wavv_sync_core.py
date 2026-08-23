"""Shared sync logic used by both the CLI (sync.py) and the web API (app.py)."""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone

import psycopg2.extensions
from dotenv import load_dotenv

from db import Db
from wavv_client import WavvClient, WavvApiError  # noqa: F401 (re-exported)

STATE_KEY_HIGH_WATER_MARK = "last_started_at_synced"
# Re-pull a small trailing window on every incremental run so we never lose a
# call that was still being written when the previous run's cursor passed it.
INCREMENTAL_OVERLAP = timedelta(minutes=15)


def parse_since(value: str) -> str:
    """Accepts an ISO 8601 timestamp, or shorthand like '90d' / '24h' meaning 'N ago'."""
    m = re.fullmatch(r"(\d+)([dh])", value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(days=n) if unit == "d" else timedelta(hours=n)
        return (datetime.now(timezone.utc) - delta).isoformat()
    return value


def load_config(require_db: bool = True) -> dict:
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    cfg = {
        "api_key": os.environ.get("WAVV_API_KEY"),
        "base_url": os.environ.get("WAVV_BASE_URL", "https://api.wavv.com/v3"),
        "database_url": os.environ.get("DATABASE_URL"),
        "backfill_since": os.environ.get("WAVV_BACKFILL_SINCE", "90d"),
        "sync_api_key": os.environ.get("SYNC_API_KEY"),
        "allowed_origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    }
    required = ["api_key"] + (["database_url"] if require_db else [])
    missing = [k for k in required if not cfg[k]]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}. Set them in .env or the environment.")
    return cfg


def normalize_call(call: dict) -> dict:
    """Map WAVV's camelCase call object 1:1 onto the row shape db.upsert_calls expects."""
    return {
        "id": call["id"],
        "teamId": call["teamId"],
        "campaignId": call.get("campaignId"),
        "direction": call["direction"],
        "phone": call["phone"],
        "callerId": call.get("callerId"),
        "contactId": call.get("contactId"),
        "contactName": call.get("contactName"),
        "startedAt": call["startedAt"],
        "answeredAt": call.get("answeredAt"),
        "endedAt": call.get("endedAt"),
        "seconds": call.get("seconds"),
        "outcome": call.get("outcome"),
        "disposition": call.get("disposition"),
        "human": call.get("human"),
        "note": call.get("note"),
        "summary": call.get("summary"),
        "recorded": call.get("recorded", False),
    }


def run_pull(client: WavvClient, db: Db, since: str, until: str | None, label: str, on_progress=None) -> int:
    total = 0
    latest_started_at = since
    batch = []
    BATCH_SIZE = 500

    for call in client.iter_calls(started_after=since, started_before=until):
        batch.append(normalize_call(call))
        if call["startedAt"] > latest_started_at:
            latest_started_at = call["startedAt"]
        if len(batch) >= BATCH_SIZE:
            db.upsert_calls(batch)
            total += len(batch)
            if on_progress:
                on_progress(total)
            batch = []

    if batch:
        db.upsert_calls(batch)
        total += len(batch)

    if total > 0:
        db.set_state(STATE_KEY_HIGH_WATER_MARK, latest_started_at)

    return total


def do_backfill(cfg: dict, since: str, until: str | None = None) -> dict:
    db = Db(cfg["database_url"])
    db.ensure_schema()
    client = WavvClient(cfg["api_key"], cfg["base_url"])
    since = parse_since(since)
    until = parse_since(until) if until else None
    total = run_pull(client, db, since, until, label="backfill")
    db.close()
    return {"synced": total, "since": since, "until": until}


def do_incremental_sync(cfg: dict) -> dict:
    db = Db(cfg["database_url"])
    db.ensure_schema()
    client = WavvClient(cfg["api_key"], cfg["base_url"])

    high_water_mark = db.get_state(STATE_KEY_HIGH_WATER_MARK)
    bootstrapped = False
    if high_water_mark:
        since_dt = datetime.fromisoformat(high_water_mark.replace("Z", "+00:00")) - INCREMENTAL_OVERLAP
        since = since_dt.isoformat()
    else:
        since = parse_since(cfg["backfill_since"])
        bootstrapped = True

    total = run_pull(client, db, since, None, label="sync")
    db.close()
    return {"synced": total, "since": since, "bootstrapped": bootstrapped}


def get_status(cfg: dict) -> dict:
    db = Db(cfg["database_url"])
    with db.conn.cursor() as cur:
        cur.execute("SELECT value, updated_at FROM wavv_sync_state WHERE key = %s", (STATE_KEY_HIGH_WATER_MARK,))
        row = cur.fetchone()
        cur.execute("SELECT count(*), max(synced_at) FROM wavv_calls")
        total_calls, last_synced_at = cur.fetchone()
    db.close()
    return {
        "last_call_started_at_synced": row[0] if row else None,
        "last_sync_run_at": row[1].isoformat() if row else None,
        "total_calls": total_calls,
        "last_row_synced_at": last_synced_at.isoformat() if last_synced_at else None,
    }


def get_summary(cfg: dict, days: int = 14) -> list[dict]:
    db = Db(cfg["database_url"])
    with db.conn.cursor() as cur:
        cur.execute(
            "SELECT call_date, direction, total_calls, conversations, answered_calls, "
            "avg_talk_seconds, total_talk_seconds FROM wavv_daily_summary LIMIT %s",
            (days * 2,),  # 2 rows/day (inbound+outbound)
        )
        rows = cur.fetchall()
    db.close()
    return [
        {
            "date": r[0].date().isoformat(),
            "direction": r[1],
            "total_calls": r[2],
            "conversations": r[3],
            "answered_calls": r[4],
            "avg_talk_seconds": float(r[5]) if r[5] is not None else None,
            "total_talk_seconds": r[6],
        }
        for r in rows
    ]


def sql_literal(value) -> str:
    """Safely quote a Python value as a SQL literal without a live DB connection."""
    if value is None:
        return "NULL"
    return psycopg2.extensions.adapt(value).getquoted().decode("utf-8")


def build_upsert_sql(calls: list[dict]) -> str:
    """Render a batch of normalized calls as a single multi-row INSERT ... ON CONFLICT
    statement, for environments that can't open a direct Postgres connection.
    """
    cols = [
        "id", "team_id", "campaign_id", "direction", "phone", "caller_id",
        "contact_id", "contact_name", "started_at", "answered_at", "ended_at",
        "seconds", "outcome", "disposition", "human", "note", "summary", "recorded",
    ]
    key_map = {
        "id": "id", "team_id": "teamId", "campaign_id": "campaignId", "direction": "direction",
        "phone": "phone", "caller_id": "callerId", "contact_id": "contactId",
        "contact_name": "contactName", "started_at": "startedAt", "answered_at": "answeredAt",
        "ended_at": "endedAt", "seconds": "seconds", "outcome": "outcome",
        "disposition": "disposition", "human": "human", "note": "note",
        "summary": "summary", "recorded": "recorded",
    }
    value_rows = []
    for call in calls:
        vals = ", ".join(sql_literal(call[key_map[c]]) for c in cols)
        value_rows.append(f"({vals}, now())")

    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id") + ", synced_at = now()"

    return (
        f"INSERT INTO wavv_calls ({', '.join(cols)}, synced_at)\nVALUES\n"
        + ",\n".join(value_rows)
        + f"\nON CONFLICT (id) DO UPDATE SET {update_clause};"
    )
