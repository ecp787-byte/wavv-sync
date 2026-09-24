#!/usr/bin/env python3
"""
WAVV -> SQL sync tool (CLI).

Commands:
    python sync.py init                          Create tables/views in the target database.
    python sync.py backfill                       Pull ALL historical calls (default: full history).
    python sync.py backfill --since 2026-01-01    Or pull calls from a specific date forward.
    python sync.py sync                           Incremental pull since the last successful run
                                                    (does a full backfill automatically on first run).
    python sync.py summary                        Print a quick daily rollup from wavv_daily_summary.
    python sync.py dispositions                   Print call counts/talk time by disposition.
    python sync.py talktime                       Print avg/median/min/max talk time per direction.
    python sync.py emit-sql --since 90d --out f    Fetch over HTTPS only, write upsert SQL to a file
                                                    (for environments without a direct Postgres connection).

Config is read from environment variables (or a .env file next to this script):
    WAVV_API_KEY, WAVV_BASE_URL, DATABASE_URL, WAVV_BACKFILL_SINCE (default: "all" = full history)

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


def _print_agent_results(result):
    print(f"  {result['agent_count']} connected dialer(s):")
    for name, r in result["agents"].items():
        if r["ok"]:
            print(f"    - {name}: {r['synced']} calls upserted")
        else:
            print(f"    - {name}: FAILED - {r['error']}")


def cmd_backfill(args, cfg):
    since = args.since or cfg["backfill_since"]
    label = "all history" if str(since).strip().lower() in ("all", "full", "none", "") else since
    print(f"Backfilling calls from {label} to {args.until or 'now'} (all connected dialers)...")
    result = core.do_backfill(cfg, since, args.until)
    print(f"[backfill] done. {result['synced']} calls upserted total.")
    _print_agent_results(result)


def cmd_dispositions(args, cfg):
    rows = core.get_dispositions(cfg)
    if not rows:
        print("No data yet. Run `python sync.py backfill` first.")
    else:
        print(f"{'dir':<9} {'disposition':<24} {'calls':>6} {'% of dir':>9} {'convos':>7} {'avg_sec':>8} {'total_sec':>10}")
        for r in rows:
            print(f"{r['direction']:<9} {r['disposition']:<24} {r['call_count']:>6} "
                  f"{str(r['pct_of_direction']) + '%':>9} {r['conversations']:>7} "
                  f"{str(r['avg_talk_seconds']):>8} {str(r['total_talk_seconds']):>10}")


def cmd_talktime(args, cfg):
    rows = core.get_talktime_stats(cfg)
    if not rows:
        print("No data yet. Run `python sync.py backfill` first.")
    else:
        print(f"{'dir':<9} {'calls':>6} {'avg_sec':>8} {'median_sec':>11} {'min':>5} {'max':>6} {'total_sec':>10}")
        for r in rows:
            print(f"{r['direction']:<9} {r['calls_with_talktime']:>6} {str(r['avg_seconds']):>8} "
                  f"{str(r['median_seconds']):>11} {str(r['min_seconds']):>5} {str(r['max_seconds']):>6} "
                  f"{str(r['total_seconds']):>10}")


def cmd_sync(args, cfg):
    result = core.do_incremental_sync(cfg)
    print(f"[sync] done. {result['synced']} calls upserted total.")
    _print_agent_results(result)


def cmd_agents_list(args, cfg):
    agents = core.list_agents(cfg)
    if not agents:
        print("No dialers connected yet. Add one with `python sync.py agents add --name <agent> --key <api key>`.")
        return
    print(f"{'name':<20} {'api key':<18} {'active':<7} {'last synced':<26} last error")
    for a in agents:
        print(f"{a['agent_name']:<20} {a['api_key']:<18} {str(a['active']):<7} "
              f"{str(a['last_synced_at']):<26} {a['last_error'] or ''}")


def cmd_agents_add(args, cfg):
    agent = core.create_agent(cfg, args.name, args.key, args.base_url)
    print(f"Added dialer '{agent['agent_name']}' (id {agent['id']}). Run `python sync.py backfill` to pull its history.")


def cmd_agents_remove(args, cfg):
    ok = core.delete_agent(cfg, args.id)
    print("Removed." if ok else "No dialer found with that id. Run `python sync.py agents list` to see ids.")


def cmd_summary(args, cfg):
    rows = core.get_summary(cfg)
    if not rows:
        print("No data yet. Run `python sync.py backfill` first.")
    else:
        print(f"{'date':<12} {'dir':<9} {'calls':>6} {'convos':>7} {'answered':>9} {'avg_sec':>8} {'total_sec':>10}")
        for r in rows:
            print(f"{r['date']:<12} {r['direction']:<9} {r['total_calls']:>6} {r['conversations']:>7} "
                  f"{r['answered_calls']:>9} {str(r['avg_talk_seconds']):>8} {str(r['total_talk_seconds']):>10}")


def cmd_weekly(args, cfg):
    cmp = core.get_week_over_week(cfg)
    tw, lw, d = cmp["this_week"], cmp["last_week"], cmp["deltas_pct"]

    def fmt_delta(pct):
        if pct is None:
            return "n/a"
        return f"{'+' if pct >= 0 else ''}{pct}%"

    print("This week (to date) vs last week:")
    print(f"  {'metric':<18} {'this week':>10} {'last week':>10} {'change':>8}")
    for key, label in [
        ("total_calls", "Total calls"), ("conversations", "Conversations"),
        ("answered_calls", "Answered"), ("total_talk_seconds", "Talk time (s)"),
    ]:
        print(f"  {label:<18} {tw[key]:>10} {lw[key]:>10} {fmt_delta(d[key]):>8}")

    print()
    rows = core.get_weekly_summary(cfg)
    if rows:
        print(f"{'week of':<12} {'dir':<9} {'calls':>6} {'convos':>7} {'answered':>9} {'total_sec':>10}")
        for r in rows:
            print(f"{r['week_start']:<12} {r['direction']:<9} {r['total_calls']:>6} {r['conversations']:>7} "
                  f"{r['answered_calls']:>9} {str(r['total_talk_seconds']):>10}")


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
    p_backfill.add_argument(
        "--since",
        help="ISO date/time, shorthand like 90d/24h, or 'all' for full history. Defaults to WAVV_BACKFILL_SINCE (which itself defaults to 'all').",
    )
    p_backfill.add_argument("--until", help="ISO date/time or shorthand. Defaults to now.")

    sub.add_parser("sync", help="Incrementally pull calls since the last successful run, for every connected dialer.")
    sub.add_parser("summary", help="Print a quick daily rollup from the database.")
    sub.add_parser("weekly", help="Print this-week-vs-last-week comparison and a weekly rollup.")
    sub.add_parser("dispositions", help="Print call counts and talk time by disposition.")
    sub.add_parser("talktime", help="Print talk-time stats (avg/median/min/max) per direction.")

    p_agents = sub.add_parser("agents", help="Manage connected WAVV dialers (one per agent).")
    agents_sub = p_agents.add_subparsers(dest="agents_command", required=True)
    agents_sub.add_parser("list", help="List connected dialers.")
    p_agents_add = agents_sub.add_parser("add", help="Connect a new dialer.")
    p_agents_add.add_argument("--name", required=True, help="Agent name to attribute this dialer's calls to.")
    p_agents_add.add_argument("--key", required=True, help="That agent's WAVV API key.")
    p_agents_add.add_argument("--base-url", dest="base_url", help="Defaults to WAVV_BASE_URL / https://api.wavv.com/v3.")
    p_agents_remove = agents_sub.add_parser("remove", help="Disconnect a dialer.")
    p_agents_remove.add_argument("--id", required=True, help="The dialer's id, from `agents list`.")

    p_emit = sub.add_parser(
        "emit-sql",
        help="Fetch calls over HTTPS only and write an upsert SQL file, without connecting to Postgres.",
    )
    p_emit.add_argument("--since", required=True, help="ISO date/time or shorthand like 90d, 24h.")
    p_emit.add_argument("--until", help="ISO date/time or shorthand. Defaults to now.")
    p_emit.add_argument("--out", required=True, help="Path to write the generated .sql file to.")

    args = parser.parse_args()
    cfg = core.load_config(require_db=(args.command != "emit-sql"))

    if args.command == "agents":
        try:
            {
                "list": cmd_agents_list,
                "add": cmd_agents_add,
                "remove": cmd_agents_remove,
            }[args.agents_command](args, cfg)
        except WavvApiError as e:
            sys.exit(f"WAVV API error: {e}")
        return

    try:
        {
            "init": cmd_init,
            "backfill": cmd_backfill,
            "sync": cmd_sync,
            "summary": cmd_summary,
            "weekly": cmd_weekly,
            "dispositions": cmd_dispositions,
            "talktime": cmd_talktime,
            "emit-sql": cmd_emit_sql,
        }[args.command](args, cfg)
    except WavvApiError as e:
        sys.exit(f"WAVV API error: {e}")


if __name__ == "__main__":
    main()
