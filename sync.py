#!/usr/bin/env python3
"""
WAVV -> SQL sync tool (CLI).

Commands:
    python sync.py init                          Create tables/views in the target database.
    python sync.py backfill --since 2026-01-01    Pull historical calls into the database.
    python sync.py sync                           Incremental pull since the last successful run.
    python sync.py summary                        Print a quick rollup from wavv_daily_summary.
    python sync.py emit-sql --since 90d --out f    Fetch over HTTPS only, write upsert SQL to a file
                                                    (for environments without a direct Postgres connection).

Config is read from environment variables (or a .env file next to this script):
    WAVV_API_KEY, WAVV_BASE_URL, DATABASE_URL, WAVV_BACKFILL_SINCE

This same logic also runs behind a small web API (app.py) for on-demand syncs and a
dashboard — see README.md.
"""

from __future__ import annotations

import argparse
import sys

from db import Db
from wavv_client import WavvApiError
import wavv_sync_core as core


def cmd_init(args, cfg):
    db = Db(cfg["database_url"])
    db.ensure_schema()
    print("Schema is up to date (wavv_calls, wavv_sync_state, wavv_daily_summary).")
    db.close()


def cmd_backfill(args, cfg):
    since = args.since or cfg["backfill_since"]
    print(f"Backfilling calls from {since} to {args.until or 'now'}...")
    result = core.do_backfill(cfg, since, args.until)
    print(f"[backfill] done. {result['synced']} calls upserted.")


def cmd_sync(args, cfg):
    result = core.do_incremental_sync(cfg)
    if result["bootstrapped"]:
        print(f"No prior sync state found; ran initial backfill from {result['since']}.")
    print(f"[sync] done. {result['synced']} calls upserted.")


def cmd_summary(args, cfg):
    rows = core.get_summary(cfg)
    if not rows:
        print("No data yet. Run `python sync.py backfill` first.")
    else:
        print(f"{'date':<12} {'dir':<9} {'calls':>6} {'convos':>7} {'answered':>9} {'avg_sec':>8} {'total_sec':>10}")
        for r in rows:
            print(f"{r['date']:<12} {r['direction']:<9} {r['total_calls']:>6} {r['conversations']:>7} "
                  f"{r['answered_calls']:>9} {str(r['avg_talk_seconds']):>8} {str(r['total_talk_seconds']):>10}")


def cmd_emit_sql(args, cfg):
    from wavv_client import WavvClient

    client = WavvClient(cfg["api_key"], cfg["base_url"])
    since = core.parse_since(args.since or cfg["backfill_since"])
    until = core.parse_since(args.until) if args.until else None

    calls = [core.normalize_call(c) for c in client.iter_calls(started_after=since, started_before=until)]
    if not calls:
        print("No calls found in range; nothing to write.")
        open(args.out, "w").close()
        return

    latest_started_at = max(c["startedAt"] for c in calls)
    sql = core.build_upsert_sql(calls)
    sql += (
        f"\n\nINSERT INTO wavv_sync_state (key, value, updated_at) "
        f"VALUES ('{core.STATE_KEY_HIGH_WATER_MARK}', {core.sql_literal(latest_started_at)}, now())\n"
        f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();\n"
    )

    with open(args.out, "w") as f:
        f.write(sql)
    print(f"Wrote {len(calls)} calls as upsert SQL to {args.out} (newest startedAt: {latest_started_at}).")


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
        help="Fetch calls over HTTPS only and write an upsert SQL file, without connecting to Postgres.",
    )
    p_emit.add_argument("--since", required=True, help="ISO date/time or shorthand like 90d, 24h.")
    p_emit.add_argument("--until", help="ISO date/time or shorthand. Defaults to now.")
    p_emit.add_argument("--out", required=True, help="Path to write the generated .sql file to.")

    args = parser.parse_args()
    cfg = core.load_config(require_db=(args.command != "emit-sql"))

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
