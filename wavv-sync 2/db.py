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
    seconds, outcome, disposition, human, note, summary, recorded, synced_at
) VALUES (
    %(id)s, %(teamId)s, %(campaignId)s, %(direction)s, %(phone)s, %(callerId)s,
    %(contactId)s, %(contactName)s, %(startedAt)s, %(answeredAt)s, %(endedAt)s,
    %(seconds)s, %(outcome)s, %(disposition)s, %(human)s, %(note)s, %(summary)s, %(recorded)s, now()
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
    synced_at    = now();
"""


class Db:
    def __init__(self, database_url: str):
        self.conn = psycopg2.connect(database_url)
        self.conn.autocommit = False

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
