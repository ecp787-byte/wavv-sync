-- WAVV call data warehouse schema
-- Safe to run repeatedly (idempotent).

-- One row per connected WAVV dialer account, attributed to the agent who owns
-- it. api_key is the credential the sync engine calls WAVV with on that
-- agent's behalf; it's stored in plaintext here the same way it previously
-- lived in a plaintext env var -- fine for a small internal tool, but this is
-- the one table in this schema that holds a real credential.
CREATE TABLE IF NOT EXISTS wavv_agents (
    id             UUID PRIMARY KEY,
    agent_name     TEXT NOT NULL UNIQUE,
    api_key        TEXT NOT NULL,
    base_url       TEXT NOT NULL DEFAULT 'https://api.wavv.com/v3',
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_synced_at TIMESTAMPTZ,
    last_error     TEXT
);

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
    -- which connected dialer/agent this call came from (nullable for rows
    -- pulled before multi-agent support existed)
    agent_id        UUID REFERENCES wavv_agents(id) ON DELETE SET NULL,
    agent_name      TEXT,
    -- convenience flag: a "conversation" = a human-answered, connected call
    is_conversation BOOLEAN GENERATED ALWAYS AS (answered_at IS NOT NULL AND human IS DISTINCT FROM FALSE) STORED,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migrate a table created before multi-agent support existed.
ALTER TABLE wavv_calls ADD COLUMN IF NOT EXISTS agent_id UUID REFERENCES wavv_agents(id) ON DELETE SET NULL;
ALTER TABLE wavv_calls ADD COLUMN IF NOT EXISTS agent_name TEXT;

CREATE INDEX IF NOT EXISTS idx_wavv_calls_started_at ON wavv_calls (started_at);
CREATE INDEX IF NOT EXISTS idx_wavv_calls_direction   ON wavv_calls (direction);
CREATE INDEX IF NOT EXISTS idx_wavv_calls_campaign     ON wavv_calls (campaign_id);
CREATE INDEX IF NOT EXISTS idx_wavv_calls_contact      ON wavv_calls (contact_id);
CREATE INDEX IF NOT EXISTS idx_wavv_calls_agent        ON wavv_calls (agent_id);

-- Tracks sync progress so incremental runs know where to pick up.
-- key is namespaced per agent: "last_started_at_synced:<agent_id>".
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

-- Disposition breakdown: how calls resolved, per direction, with a share-of-total
-- percentage and the talk time associated with each disposition.
CREATE OR REPLACE VIEW wavv_disposition_summary AS
SELECT
    direction,
    COALESCE(disposition, '(none)')                                                        AS disposition,
    COUNT(*)                                                                                AS call_count,
    ROUND(100.0 * COUNT(*) / NULLIF(SUM(COUNT(*)) OVER (PARTITION BY direction), 0), 1)      AS pct_of_direction,
    COUNT(*) FILTER (WHERE is_conversation)                                                  AS conversations,
    ROUND(AVG(seconds) FILTER (WHERE seconds IS NOT NULL), 1)                                AS avg_talk_seconds,
    SUM(seconds)                                                                             AS total_talk_seconds
FROM wavv_calls
GROUP BY direction, COALESCE(disposition, '(none)')
ORDER BY direction, call_count DESC;

-- Talk-time distribution per direction: average, median, min/max, and total --
-- the median in particular is a much better "typical call" number than the
-- average once a few very long or very short calls are in the mix.
CREATE OR REPLACE VIEW wavv_talktime_stats AS
SELECT
    direction,
    COUNT(*)                                            AS calls_with_talktime,
    ROUND(AVG(seconds), 1)                              AS avg_seconds,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY seconds)::numeric, 1) AS median_seconds,
    MIN(seconds)                                        AS min_seconds,
    MAX(seconds)                                        AS max_seconds,
    SUM(seconds)                                         AS total_seconds
FROM wavv_calls
WHERE seconds IS NOT NULL
GROUP BY direction;

-- Per-agent (per connected dialer) totals -- answers "map the API to an agent's
-- name": every call is attributed to whichever wavv_agents row pulled it.
CREATE OR REPLACE VIEW wavv_agent_summary AS
SELECT
    COALESCE(agent_name, '(unassigned)')                AS agent_name,
    COUNT(*)                                             AS total_calls,
    COUNT(*) FILTER (WHERE is_conversation)              AS conversations,
    COUNT(*) FILTER (WHERE answered_at IS NOT NULL)      AS answered_calls,
    SUM(seconds)                                         AS total_talk_seconds,
    MAX(started_at)                                      AS last_call_at
FROM wavv_calls
GROUP BY COALESCE(agent_name, '(unassigned)')
ORDER BY total_calls DESC;

-- Weekly rollup (calendar week, Monday start) -- the historical/week-to-week
-- counterpart to wavv_daily_summary, for tracking performance trend over time
-- rather than just a single day's or a single all-time number.
CREATE OR REPLACE VIEW wavv_weekly_summary AS
SELECT
    date_trunc('week', started_at)                              AS week_start,
    direction,
    COUNT(*)                                                    AS total_calls,
    COUNT(*) FILTER (WHERE is_conversation)                     AS conversations,
    COUNT(*) FILTER (WHERE answered_at IS NOT NULL)             AS answered_calls,
    ROUND(AVG(seconds) FILTER (WHERE seconds IS NOT NULL), 1)   AS avg_talk_seconds,
    SUM(seconds)                                                AS total_talk_seconds
FROM wavv_calls
GROUP BY 1, 2
ORDER BY 1 DESC, 2;
