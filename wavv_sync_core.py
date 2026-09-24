"""Shared sync logic used by both the CLI (sync.py) and the web API (app.py)."""

from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2.extensions
from dotenv import load_dotenv

from db import Db
from wavv_client import WavvClient, WavvApiError  # noqa: F401 (re-exported)

STATE_KEY_HIGH_WATER_MARK = "last_started_at_synced"  # namespaced per agent: "...:<agent_id>"
# Re-pull a small trailing window on every incremental run so we never lose a
# call that was still being written when the previous run's cursor passed it.
INCREMENTAL_OVERLAP = timedelta(minutes=15)


def parse_since(value: str | None) -> str | None:
    """Accepts an ISO 8601 timestamp, shorthand like '90d' / '24h' meaning 'N ago',
    or 'all' (also 'full'/'none', or empty) meaning no lower bound -- full call history."""
    if not value or value.strip().lower() in ("all", "full", "none"):
        return None
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
        # "all" = no lower bound -- pull every call WAVV has, not just a trailing window.
        "backfill_since": os.environ.get("WAVV_BACKFILL_SINCE", "all"),
        "sync_api_key": os.environ.get("SYNC_API_KEY"),
        "allowed_origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    }
    # Dialers are normally configured via the wavv_agents table (see agent CRUD
    # below), not a single env var, so WAVV_API_KEY is optional once a database
    # is in play -- it only matters as a one-time migration seed (see
    # ensure_default_agent) or for the standalone `emit-sql` path below, which
    # has no database and talks to WAVV directly with just this one key.
    required = (["database_url"] if require_db else ["api_key"])
    missing = [k for k in required if not cfg[k]]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}. Set them in .env or the environment.")
    return cfg


def normalize_call(call: dict, agent: dict | None = None) -> dict:
    """Map WAVV's camelCase call object 1:1 onto the row shape db.upsert_calls expects,
    tagging it with whichever connected dialer/agent it was pulled with."""
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
        "agentId": agent["id"] if agent else None,
        "agentName": agent["agent_name"] if agent else None,
    }


def run_pull(
    client: WavvClient, db: Db, since: str | None, until: str | None, label: str,
    agent: dict | None = None, on_progress=None,
) -> int:
    total = 0
    latest_started_at = since
    batch = []
    BATCH_SIZE = 500
    state_key = f"{STATE_KEY_HIGH_WATER_MARK}:{agent['id']}" if agent else STATE_KEY_HIGH_WATER_MARK

    # WAVV's API now requires an explicit direction on every /calls request
    # (as of the Sept 2026 API relaunch) -- a query without one, or with both,
    # is rejected with 400 INVALID_REQUEST. So pull each direction separately
    # and merge them here. `since=None` means no lower bound at all: WAVV
    # simply omits startedAfter and returns full history, paginated by cursor.
    for direction in ("inbound", "outbound"):
        for call in client.iter_calls(direction=direction, started_after=since, started_before=until):
            batch.append(normalize_call(call, agent))
            if latest_started_at is None or call["startedAt"] > latest_started_at:
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

    if total > 0 and latest_started_at is not None:
        db.set_state(state_key, latest_started_at)

    return total


def ensure_default_agent(cfg: dict, db: Db) -> None:
    """Backward-compat migration: if no dialers are registered yet but a
    single WAVV_API_KEY is set in the environment (the pre-multi-agent setup),
    register it as one agent named "Default" so existing installs keep
    syncing without any manual step."""
    if cfg.get("api_key") and not db.list_agents():
        db.create_agent(str(uuid.uuid4()), "Default", cfg["api_key"], cfg["base_url"])


def do_backfill_agent(cfg: dict, agent: dict, since: str | None = "all", until: str | None = None) -> dict:
    db = Db(cfg["database_url"])
    db.ensure_schema()
    client = WavvClient(agent["api_key"], agent["base_url"])
    since = parse_since(since)
    until = parse_since(until) if until else None
    try:
        total = run_pull(client, db, since, until, label="backfill", agent=agent)
        db.set_agent_sync_result(agent["id"], error=None)
    except Exception as e:  # noqa: BLE001 - record per-agent so one bad key doesn't kill the batch
        db.set_agent_sync_result(agent["id"], error=str(e))
        db.close()
        raise
    db.close()
    return {"synced": total, "since": since, "until": until}


def do_incremental_sync_agent(cfg: dict, agent: dict) -> dict:
    db = Db(cfg["database_url"])
    db.ensure_schema()
    client = WavvClient(agent["api_key"], agent["base_url"])

    state_key = f"{STATE_KEY_HIGH_WATER_MARK}:{agent['id']}"
    high_water_mark = db.get_state(state_key)
    bootstrapped = False
    if high_water_mark:
        since_dt = datetime.fromisoformat(high_water_mark.replace("Z", "+00:00")) - INCREMENTAL_OVERLAP
        since = since_dt.isoformat()
    else:
        since = parse_since(cfg["backfill_since"])
        bootstrapped = True

    try:
        total = run_pull(client, db, since, None, label="sync", agent=agent)
        db.set_agent_sync_result(agent["id"], error=None)
    except Exception as e:  # noqa: BLE001
        db.set_agent_sync_result(agent["id"], error=str(e))
        db.close()
        raise
    db.close()
    return {"synced": total, "since": since, "bootstrapped": bootstrapped}


def _run_for_all_agents(cfg: dict, run_one) -> dict:
    """Shared loop: run `run_one(agent)` for every active connected dialer,
    continuing past a single agent's failure so one bad/expired key doesn't
    block everyone else's sync. Returns a per-agent result/error breakdown."""
    db = Db(cfg["database_url"])
    db.ensure_schema()
    ensure_default_agent(cfg, db)
    agents = db.list_agents(active_only=True)
    db.close()

    results = {}
    total_synced = 0
    for agent in agents:
        try:
            r = run_one(agent)
            results[agent["agent_name"]] = {"ok": True, **r}
            total_synced += r.get("synced", 0)
        except Exception as e:  # noqa: BLE001
            results[agent["agent_name"]] = {"ok": False, "error": str(e)}

    return {"synced": total_synced, "agents": results, "agent_count": len(agents)}


def do_backfill(cfg: dict, since: str | None = "all", until: str | None = None) -> dict:
    """Full-history backfill across every connected dialer."""
    return _run_for_all_agents(cfg, lambda agent: do_backfill_agent(cfg, agent, since, until))


def do_incremental_sync(cfg: dict) -> dict:
    """Catch-up-since-last-run sync across every connected dialer."""
    return _run_for_all_agents(cfg, lambda agent: do_incremental_sync_agent(cfg, agent))


# --- Agent (connected dialer) management ---

def _mask_key(api_key: str) -> str:
    if not api_key:
        return ""
    return ("•" * max(0, len(api_key) - 4)) + api_key[-4:]


def list_agents(cfg: dict, reveal_key: bool = False) -> list[dict]:
    db = Db(cfg["database_url"])
    db.ensure_schema()
    ensure_default_agent(cfg, db)
    agents = db.list_agents()
    db.close()
    for a in agents:
        a["api_key"] = a["api_key"] if reveal_key else _mask_key(a["api_key"])
    return agents


def create_agent(cfg: dict, agent_name: str, api_key: str, base_url: str | None = None) -> dict:
    agent_name = (agent_name or "").strip()
    api_key = (api_key or "").strip()
    if not agent_name:
        raise ValueError("agent_name is required")
    if not api_key:
        raise ValueError("api_key is required")
    db = Db(cfg["database_url"])
    db.ensure_schema()
    agent_id = str(uuid.uuid4())
    db.create_agent(agent_id, agent_name, api_key, base_url or cfg["base_url"])
    agent = db.get_agent(agent_id)
    db.close()
    agent["api_key"] = _mask_key(agent["api_key"])
    return agent


def update_agent(cfg: dict, agent_id: str, **fields) -> bool:
    db = Db(cfg["database_url"])
    db.ensure_schema()
    updated = db.update_agent(agent_id, **fields)
    db.close()
    return updated


def delete_agent(cfg: dict, agent_id: str) -> bool:
    db = Db(cfg["database_url"])
    db.ensure_schema()
    deleted = db.delete_agent(agent_id)
    db.close()
    return deleted


def get_status(cfg: dict) -> dict:
    db = Db(cfg["database_url"])
    db.ensure_schema()
    ensure_default_agent(cfg, db)
    with db.conn.cursor() as cur:
        cur.execute("SELECT count(*), max(synced_at) FROM wavv_calls")
        total_calls, last_synced_at = cur.fetchone()
        cur.execute("SELECT count(*) FROM wavv_agents WHERE active")
        (agent_count,) = cur.fetchone()
        cur.execute("SELECT max(last_synced_at) FROM wavv_agents")
        (last_agent_sync_at,) = cur.fetchone()
    db.close()
    return {
        "total_calls": total_calls,
        "last_row_synced_at": last_synced_at.isoformat() if last_synced_at else None,
        "last_sync_run_at": last_agent_sync_at.isoformat() if last_agent_sync_at else None,
        "agent_count": agent_count,
    }


def get_agent_summary(cfg: dict) -> list[dict]:
    db = Db(cfg["database_url"])
    with db.conn.cursor() as cur:
        cur.execute(
            "SELECT agent_name, total_calls, conversations, answered_calls, "
            "total_talk_seconds, last_call_at FROM wavv_agent_summary"
        )
        rows = cur.fetchall()
    db.close()
    return [
        {
            "agent_name": r[0],
            "total_calls": r[1],
            "conversations": r[2],
            "answered_calls": r[3],
            "total_talk_seconds": r[4],
            "last_call_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


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


def get_weekly_summary(cfg: dict, weeks: int = 12) -> list[dict]:
    db = Db(cfg["database_url"])
    with db.conn.cursor() as cur:
        cur.execute(
            "SELECT week_start, direction, total_calls, conversations, answered_calls, "
            "avg_talk_seconds, total_talk_seconds FROM wavv_weekly_summary LIMIT %s",
            (weeks * 2,),  # 2 rows/week (inbound+outbound)
        )
        rows = cur.fetchall()
    db.close()
    return [
        {
            "week_start": r[0].date().isoformat(),
            "direction": r[1],
            "total_calls": r[2],
            "conversations": r[3],
            "answered_calls": r[4],
            "avg_talk_seconds": float(r[5]) if r[5] is not None else None,
            "total_talk_seconds": r[6],
        }
        for r in rows
    ]


def _pct_change(current: float, previous: float) -> float | None:
    if not previous:
        return None
    return round(((current - previous) / previous) * 100, 1)


def get_week_over_week(cfg: dict) -> dict:
    """This calendar week (so far, Monday start) vs last week (full), combined
    across every direction and every connected dialer. "This week" is a
    week-to-date total, not a full week, until the week finishes -- the
    dashboard labels it that way rather than implying a like-for-like total."""
    db = Db(cfg["database_url"])
    with db.conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                CASE WHEN started_at >= date_trunc('week', now()) THEN 'this_week' ELSE 'last_week' END AS bucket,
                COUNT(*) AS total_calls,
                COUNT(*) FILTER (WHERE is_conversation) AS conversations,
                COUNT(*) FILTER (WHERE answered_at IS NOT NULL) AS answered_calls,
                SUM(seconds) AS total_talk_seconds
            FROM wavv_calls
            WHERE started_at >= date_trunc('week', now()) - interval '1 week'
            GROUP BY 1
            """
        )
        rows = {r[0]: r for r in cur.fetchall()}
    db.close()

    zero = (None, 0, 0, 0, 0)
    this_row = rows.get("this_week", zero)
    last_row = rows.get("last_week", zero)

    def _shape(r):
        return {
            "total_calls": r[1] or 0,
            "conversations": r[2] or 0,
            "answered_calls": r[3] or 0,
            "total_talk_seconds": r[4] or 0,
        }

    this_week, last_week = _shape(this_row), _shape(last_row)
    deltas = {
        k: _pct_change(this_week[k], last_week[k])
        for k in ("total_calls", "conversations", "answered_calls", "total_talk_seconds")
    }
    return {"this_week": this_week, "last_week": last_week, "deltas_pct": deltas}


def get_dispositions(cfg: dict) -> list[dict]:
    db = Db(cfg["database_url"])
    with db.conn.cursor() as cur:
        cur.execute(
            "SELECT direction, disposition, call_count, pct_of_direction, conversations, "
            "avg_talk_seconds, total_talk_seconds FROM wavv_disposition_summary"
        )
        rows = cur.fetchall()
    db.close()
    return [
        {
            "direction": r[0],
            "disposition": r[1],
            "call_count": r[2],
            "pct_of_direction": float(r[3]) if r[3] is not None else None,
            "conversations": r[4],
            "avg_talk_seconds": float(r[5]) if r[5] is not None else None,
            "total_talk_seconds": r[6],
        }
        for r in rows
    ]


def get_talktime_stats(cfg: dict) -> list[dict]:
    db = Db(cfg["database_url"])
    with db.conn.cursor() as cur:
        cur.execute(
            "SELECT direction, calls_with_talktime, avg_seconds, median_seconds, "
            "min_seconds, max_seconds, total_seconds FROM wavv_talktime_stats"
        )
        rows = cur.fetchall()
    db.close()
    return [
        {
            "direction": r[0],
            "calls_with_talktime": r[1],
            "avg_seconds": float(r[2]) if r[2] is not None else None,
            "median_seconds": float(r[3]) if r[3] is not None else None,
            "min_seconds": r[4],
            "max_seconds": r[5],
            "total_seconds": r[6],
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
