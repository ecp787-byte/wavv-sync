"""Postgres access layer: schema bootstrap, upserts, and sync-state bookkeeping."""

from __future__ import annotations

import os
from typing import Iterable, Optional

import psycopg2
import psycopg2.extras

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

UPSERT_SQL = """
INSERT INTO wavv_calls (
    id, team_id, campaign_id, direction, phone, caller_id,
    contact_id, contact_name, started_at, answered_at, ended_at,
    seconds, outcome, disposition, human, note, summary, recorded,
    agent_id, agent_name, synced_at
) VALUES (
    %(id)s, %(teamId)s, %(campaignId)s, %(direction)s, %(phone)s, %(callerId)s,
    %(contactId)s, %(contactName)s, %(startedAt)s, %(answeredAt)s, %(endedAt)s,
    %(seconds)s, %(outcome)s, %(disposition)s, %(human)s, %(note)s, %(summary)s, %(recorded)s,
    %(agentId)s, %(agentName)s, now()
)
ON CONFLICT (id) DO UPDATE SET
    campaign_id  = EXCLUDED.campaign_id,
    direction    = EXCLUDED.direction,
    phone        = EXCLUDED.phone,
    caller_id    = EXCLUDED.caller_id,
    contact_id   = EXCLUDED.contact_id,
    contact_name = EXCLUDED.contact_name,
    answered_at  = EXCLUDED.answered_at,
    ended_at     = EXCLUDED.ended_at,
    seconds      = EXCLUDED.seconds,
    outcome      = EXCLUDED.outcome,
    disposition  = EXCLUDED.disposition,
    human        = EXCLUDED.human,
    note         = EXCLUDED.note,
    summary      = EXCLUDED.summary,
    recorded     = EXCLUDED.recorded,
    agent_id     = EXCLUDED.agent_id,
    agent_name   = EXCLUDED.agent_name,
    synced_at    = now();
"""


class Db:
    def __init__(self, database_url: str):
        self.conn = psycopg2.connect(database_url)
        self.conn.autocommit = False
        # Every reporting-period boundary in wavv_sync_core.py ("today", "this week",
        # etc.) is computed with date_trunc()/now() against this session's timezone --
        # there's no separate timezone concept anywhere else in the app. Pin it to
        # US Eastern (which observes the EST/EDT switch automatically, unlike a fixed
        # UTC-5 offset) so those boundaries match the business's local day, not UTC.
        # SET (not SET LOCAL) persists for the whole session, but a later ROLLBACK on
        # the transaction it's issued in would undo it too -- commit right away so it
        # sticks regardless of what happens after.
        with self.conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'America/New_York'")
        self.conn.commit()

    def close(self):
        self.conn.close()

    def ensure_schema(self):
        with open(SCHEMA_PATH, "r") as f:
            ddl = f.read()
        with self.conn.cursor() as cur:
            cur.execute(ddl)
        self.conn.commit()

    def upsert_calls(self, calls: Iterable[dict]) -> int:
        rows = list(calls)
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows, page_size=200)
        self.conn.commit()
        return len(rows)

    def get_state(self, key: str) -> Optional[str]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT value FROM wavv_sync_state WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None

    def set_state(self, key: str, value: str):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wavv_sync_state (key, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (key, value),
            )
        self.conn.commit()

    # --- Agents (connected WAVV dialers, one per agent) ---

    def list_agents(self, active_only: bool = False) -> list[dict]:
        where = "WHERE active" if active_only else ""
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT id, agent_name, api_key, base_url, npn, active, created_at, "
                f"last_synced_at, last_error FROM wavv_agents {where} ORDER BY agent_name"
            )
            rows = cur.fetchall()
        return [
            {
                "id": str(r[0]), "agent_name": r[1], "api_key": r[2], "base_url": r[3], "npn": r[4],
                "active": r[5], "created_at": r[6].isoformat() if r[6] else None,
                "last_synced_at": r[7].isoformat() if r[7] else None, "last_error": r[8],
            }
            for r in rows
        ]

    def get_agent(self, agent_id: str) -> Optional[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id, agent_name, api_key, base_url, npn, active, created_at, "
                "last_synced_at, last_error FROM wavv_agents WHERE id = %s",
                (agent_id,),
            )
            r = cur.fetchone()
        if not r:
            return None
        return {
            "id": str(r[0]), "agent_name": r[1], "api_key": r[2], "base_url": r[3], "npn": r[4],
            "active": r[5], "created_at": r[6].isoformat() if r[6] else None,
            "last_synced_at": r[7].isoformat() if r[7] else None, "last_error": r[8],
        }

    def create_agent(self, agent_id: str, agent_name: str, api_key: str, base_url: str, npn: Optional[str] = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO wavv_agents (id, agent_name, api_key, base_url, npn) VALUES (%s, %s, %s, %s, %s)",
                (agent_id, agent_name, api_key, base_url, npn),
            )
        self.conn.commit()

    def update_agent(self, agent_id: str, **fields) -> bool:
        if not fields:
            return False
        allowed = {"agent_name", "api_key", "base_url", "npn", "active"}
        cols = [c for c in fields if c in allowed]
        if not cols:
            return False
        set_clause = ", ".join(f"{c} = %({c})s" for c in cols)
        params = {c: fields[c] for c in cols}
        params["id"] = agent_id
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE wavv_agents SET {set_clause} WHERE id = %(id)s", params)
            updated = cur.rowcount > 0
            # wavv_calls.agent_name is a denormalized copy (so summary/performance
            # queries don't need a join) -- keep it in sync, or a rename would
            # leave every call synced before it still labeled with the old name.
            if updated and "agent_name" in cols:
                cur.execute(
                    "UPDATE wavv_calls SET agent_name = %(agent_name)s WHERE agent_id = %(id)s",
                    params,
                )
        self.conn.commit()
        return updated

    def delete_agent(self, agent_id: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM wavv_agents WHERE id = %s", (agent_id,))
            deleted = cur.rowcount > 0
        self.conn.commit()
        return deleted

    def set_agent_sync_result(self, agent_id: str, error: Optional[str]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE wavv_agents SET last_synced_at = now(), last_error = %s WHERE id = %s",
                (error, agent_id),
            )
        self.conn.commit()
