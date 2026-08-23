#!/usr/bin/env python3
"""
WAVV -> SQL sync tool.

Commands:
    python sync.py init                          Create tables/views in the target database.
    python sync.py backfill --since 2026-01-01    Pull historical calls into the database.
    python sync.py sync                           Incremental pull since the last successful run.
    python sync.py summary                        Print a quick rollup from wavv_daily_summary.

Config is read from environment variables (or a .env file next to this script):
    WAVV_API_KEY, WAVV_BASE_URL, DATABASE_URL, WAVV_BACKFILL_SINCE
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import psycopg2.extensions
from dotenv import load_dotenv

from db import Db, UPSERT_SQL
from wavv_client import WavvClient, WavvApiError

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
    }
    required = ["api_key"] + (["database_url"] if require_db else [])
    missing = [k for k in required if not cfg[k]]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}. Set them in .env or the environment.")
    return cfg


def normalize_call(call: dict) -> dict:
    """Map WAVV's camelCase call object 1:1 onto the row shape db.upsert_calls expects."""
    # psycopg2 handles ISO8601 strings and None fine for timestamptz/uuid/bool columns,
    # so we just pass the fields straight through.
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


def run_pull(client: WavvClient, db: Db, since: str, until: str | None, label: str) -> int:
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
            print(f"[{label}] upserted {total} calls so far...")
            batch = []

    if batch:
        db.upsert_calls(batch)
        total += len(batch)

    if total > 0:
        db.set_state(STATE_KEY_HIGH_WATER_MARK, latest_started_at)

    print(f"[{label}] done. {total} calls upserted.")
    return total


def sql_literal(value) -> str:
    """Safely quote a Python value as a SQL literal without a live DB connection."""
    if value is None:
        return "NULL"
    return psycopg2.extensions.adapt(value).getquoted().decode("utf-8")


def build_upsert_sql(calls: list[dict]) -> str:
    """Render a batch of normalized calls as a single multi-row INSERT ... ON CONFLICT
    statement. Used in environments that can't open a direct Postgres connection
    (e.g. this tool running inside a network-restricted sandbox) — the statement text
    is applied to the database through another channel, such as an MCP Postgres/Neon
    connector, instead of psycopg2.
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

    update_clause = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in cols if c != "id"
    ) + ", synced_at = now()"

    return (
        f"INSERT INTO wavv_calls ({', '.join(cols)}, synced_at)\nVALUES\n"
        + ",\n".join(value_rows)
        + f"\nON CONFLICT (id) DO UPDATE SET {update_clause};"
    )


def cmd_emit_sql(args, cfg):
    client = WavvClient(cfg["api_key"], cfg["base_url"])
    since = parse_since(args.since or cfg["backfill_since"])
    until = parse_since(args.until) if args.until else None

    calls = [normalize_call(c) for c in client.iter_calls(started_after=since, started_before=until)]
    if not calls:
        print("No calls found in range; nothing to write.")
        # Still touch the output file so callers can detect "ran, found nothing".
        open(args.out, "w").close()
        return

    latest_started_at = max(c["startedAt"] for c in calls)
    sql = build_upsert_sql(calls)
    sql += (
        f"\n\nINSERT INTO wavv_sync_state (key, value, updated_at) "
        f"VALUES ('{STATE_KEY_HIGH_WATER_MARK}', {sql_literal(latest_started_at)}, now())\n"
        f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();\n"
    )

    with open(args.out, "w") as f:
        f.write(sql)
    print(f"Wrote {len(calls)} calls as upsert SQL to {args.out} (newest startedAt: {latest_started_at}).")


def cmd_init(args, cfg):
    db = Db(cfg["database_url"])
    db.ensure_schema()
    print("Schema is up to date (wavv_calls, wavv_sync_state, wavv_daily_summary).")
    db.close()


def cmd_backfill(args, cfg):
    db = Db(cfg["database_url"])
    db.ensure_schema()
    client = WavvClient(cfg["api_key"], cfg["base_url"])
    since = parse_since(args.since or cfg["backfill_since"])
    until = parse_since(args.until) if args.until else None
    print(f"Backfilling calls from {since} to {until or 'now'}...")
    run_pull(client, db, since, until, label="backfill")
    db.close()


def cmd_sync(args, cfg):
    db = Db(cfg["database_url"])
    db.ensure_schema()
    client = WavvClient(cfg["api_key"], cfg["base_url"])

    high_water_mark = db.get_state(STATE_KEY_HIGH_WATER_MARK)
    if high_water_mark:
        since_dt = datetime.fromisoformat(high_water_mark.replace("Z", "+00:00")) - INCREMENTAL_OVERLAP
        since = since_dt.isoformat()
    else:
        since = parse_since(cfg["backfill_since"])
        print(f"No prior sync state found; running initial backfill from {since}.")

    run_pull(client, db, since, None, label="sync")
    db.close()


def cmd_summary(args, cfg):
    db = Db(cfg["database_url"])
    with db.conn.cursor() as cur:
        cur.execute(
            "SELECT call_date, direction, total_calls, conversations, answered_calls, "
            "avg_talk_seconds, total_talk_seconds FROM wavv_daily_summary LIMIT 14"
        )
        rows = cur.fetchall()
    if not rows:
        print("No data yet. Run `python sync.py backfill` first.")
    else:
        print(f"{'date':<12} {'dir':<9} {'calls':>6} {'convos':>7} {'answered':>9} {'avg_sec':>8} {'total_sec':>10}")
        for r in rows:
            call_date, direction, total_calls, conversations, answered, avg_sec, total_sec = r
            print(f"{str(call_date.date()):<12} {direction:<9} {total_calls:>6} {conversations:>7} "
                  f"{answered:>9} {str(avg_sec):>8} {str(total_sec):>10}")
    db.close()


def main():
    parser = argparse.ArgumentParser(description="Sync WAVV dialer call data into Postgres.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create/update the database schema.")

    p_backfill = sub.add_parser("backfill", help="Pull historical calls.")
    p_backfill.add_argument("--since", help="ISO date/time or shorthand like 90d, 24h. Defaults to WAVV_BACKFILL_SINCE.")
    p_backfill.add_argument("--until", help="ISO date/time or shorthand. Defaults to now.")

    sub.add_parser("sync", help="Incrementally pull calls since the last successful run.")
    sub.add_parser("summary", help="Print a quick daily rollup from the database.")

    p_emit = sub.add_parser(
        "emit-sql",
        help="Fetch calls over HTTPS only and write an upsert SQL file, without connecting to Postgres. "
             "For environments that can't open a direct DB connection (apply the file via another channel, "
             "e.g. an MCP Postgres/Neon connector).",
    )
    p_emit.add_argument("--since", required=True, help="ISO date/time or shorthand like 90d, 24h.")
    p_emit.add_argument("--until", help="ISO date/time or shorthand. Defaults to now.")
    p_emit.add_argument("--out", required=True, help="Path to write the generated .sql file to.")

    args = parser.parse_args()
    cfg = load_config(require_db=(args.command != "emit-sql"))

    try:
        {
            "init": cmd_init,
            "backfill": cmd_backfill,
            "sync": cmd_sync,
            "summary": cmd_summary,
            "emit-sql": cmd_emit_sql,
        }[args.command](args, cfg)
    except WavvApiError as e:
        sys.exit(f"WAVV API error: {e}")


if __name__ == "__main__":
    main()
