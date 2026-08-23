-- WAVV call data warehouse schema
-- Safe to run repeatedly (idempotent).

CREATE TABLE IF NOT EXISTS wavv_calls (
    id              UUID PRIMARY KEY,
    team_id         UUID NOT NULL,
    campaign_id     UUID,
    direction       TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    phone           TEXT NOT NULL,
    caller_id       TEXT,
    contact_id      TEXT,
    contact_name    TEXT,
    started_at      TIMESTAMPTZ NOT NULL,
    answered_at     TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    seconds         INTEGER,
    outcome         TEXT,
    disposition     TEXT,
    human           BOOLEAN,
    note            TEXT,
    summary         TEXT,
    recorded        BOOLEAN NOT NULL DEFAULT FALSE,
    -- convenience flag: a "conversation" = a human-answered, connected call
    is_conversation BOOLEAN GENERATED ALWAYS AS (answered_at IS NOT NULL AND human IS DISTINCT FROM FALSE) STORED,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wavv_calls_started_at ON wavv_calls (started_at);
CREATE INDEX IF NOT EXISTS idx_wavv_calls_direction   ON wavv_calls (direction);
CREATE INDEX IF NOT EXISTS idx_wavv_calls_campaign     ON wavv_calls (campaign_id);
CREATE INDEX IF NOT EXISTS idx_wavv_calls_contact      ON wavv_calls (contact_id);

-- Tracks sync progress so incremental runs know where to pick up.
CREATE TABLE IF NOT EXISTS wavv_sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Handy rollup view: daily call volume, talk time, and conversation rate per direction.
CREATE OR REPLACE VIEW wavv_daily_summary AS
SELECT
    date_trunc('day', started_at)  AS call_date,
    direction,
    COUNT(*)                                                   AS total_calls,
    COUNT(*) FILTER (WHERE is_conversation)                    AS conversations,
    COUNT(*) FILTER (WHERE answered_at IS NOT NULL)             AS answered_calls,
    ROUND(AVG(seconds) FILTER (WHERE seconds IS NOT NULL), 1)   AS avg_talk_seconds,
    SUM(seconds)                                                AS total_talk_seconds
FROM wavv_calls
GROUP BY 1, 2
ORDER BY 1 DESC, 2;
