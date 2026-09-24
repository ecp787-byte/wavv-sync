#!/usr/bin/env python3
"""
Small web API in front of the WAVV -> Postgres sync tool.

Endpoints:
    GET    /api/health        -> {"ok": true}
    GET    /api/status        -> DB-level sync status (row count, connected dialer count)
    GET    /api/summary       -> daily rollup (calls, conversations, talk time) for a dashboard
    GET    /api/weekly        -> weekly rollup, for a longer trend chart
    GET    /api/weekly/compare-> this-week-to-date vs last-week comparison + % change
    GET    /api/dispositions  -> call counts/talk time broken down by disposition, per direction
    GET    /api/talktime      -> avg/median/min/max/total talk time per direction
    GET    /api/agents        -> connected dialers, with masked API keys (requires X-API-Key header)
    POST   /api/agents        -> connect a new dialer: {agent_name, api_key, base_url?, npn?} (requires X-API-Key header)
    PATCH  /api/agents/<id>   -> rename, re-key, set NPN, or activate/deactivate a dialer (requires X-API-Key header)
    DELETE /api/agents/<id>   -> disconnect a dialer (requires X-API-Key header)
    GET    /api/agents/summary-> per-agent call totals (which agent's dialer produced what)
    GET    /api/agents/performance -> per-agent rollup by period=daily|weekly|monthly|quarterly, optional agent_name filter
    GET    /api/scorecard     -> one agent's (or everyone's) dialing scorecard over an arbitrary
                                  date range: agent_name?, start? (YYYY-MM-DD), end? (YYYY-MM-DD, inclusive)
    GET    /api/leaderboard   -> agents ranked by weighted points for the current period=daily|weekly|monthly|quarterly
    POST   /api/backfill      -> pull ALL historical calls for every connected dialer (requires X-API-Key header)
    POST   /api/sync          -> on-demand incremental sync for every connected dialer (requires X-API-Key header)
    GET    /api/sync/status   -> status of the most recent manually-triggered sync/backfill job

Intended to sit behind a front-end (e.g. a Webflow page) that calls these over HTTPS.
Runs alongside the existing Render Cron Job, which handles the reliable hourly schedule;
this service adds a dashboard, dialer management, and a manual "sync now" button.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request
from flask_cors import CORS

import wavv_sync_core as core
from wavv_client import WavvApiError

cfg = core.load_config()

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": cfg["allowed_origin"]}})

_DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "webflow-dashboard.html")


@app.get("/")
def dashboard():
    """Serve the full-page dashboard, self-configured for same-origin use.
    This is a standalone dark full-bleed page (sidebar nav + top bar), meant to be
    the only content on its page -- not a Webflow Embed fragment.
    """
    try:
        with open(_DASHBOARD_PATH) as f:
            fragment = f.read()
    except FileNotFoundError:
        return jsonify({"error": "webflow-dashboard.html not found next to app.py"}), 500

    fragment = fragment.replace("https://YOUR-RENDER-SERVICE.onrender.com", "")
    fragment = fragment.replace("YOUR_SYNC_API_KEY", cfg["sync_api_key"] or "")
    page = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>WAVV Call Sync</title></head><body style='margin:0;padding:0;"
        "background:#0a0d12;min-height:100vh;'>" + fragment + "</body></html>"
    )
    return Response(page, mimetype="text/html")

# Single in-process job slot: this is a small internal tool, not a job queue.
# A lock keeps two manual triggers from racing each other.
_job_lock = threading.Lock()
_job_state = {
    "running": False,
    "last_kind": None,
    "last_started_at": None,
    "last_finished_at": None,
    "last_result": None,
    "last_error": None,
}


def _run_job(kind: str):
    try:
        if kind == "backfill":
            result = core.do_backfill(cfg, since="all")
        else:
            result = core.do_incremental_sync(cfg)
        _job_state["last_result"] = result
        _job_state["last_error"] = None
    except Exception as e:  # noqa: BLE001 - surface any failure to the dashboard
        _job_state["last_error"] = str(e)
    finally:
        _job_state["running"] = False
        _job_state["last_kind"] = kind
        _job_state["last_finished_at"] = datetime.now(timezone.utc).isoformat()


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/status")
def status():
    try:
        return jsonify(core.get_status(cfg))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.get("/api/summary")
def summary():
    days = request.args.get("days", default=14, type=int)
    try:
        return jsonify(core.get_summary(cfg, days=days))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.get("/api/dispositions")
def dispositions():
    try:
        return jsonify(core.get_dispositions(cfg))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.get("/api/talktime")
def talktime():
    try:
        return jsonify(core.get_talktime_stats(cfg))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.get("/api/weekly")
def weekly():
    weeks = request.args.get("weeks", default=12, type=int)
    try:
        return jsonify(core.get_weekly_summary(cfg, weeks=weeks))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.get("/api/weekly/compare")
def weekly_compare():
    try:
        return jsonify(core.get_week_over_week(cfg))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.get("/api/agents/summary")
def agents_summary():
    try:
        return jsonify(core.get_agent_summary(cfg))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.get("/api/agents/performance")
def agents_performance():
    period = request.args.get("period", default="weekly")
    agent_name = request.args.get("agent_name") or None
    limit = request.args.get("limit", default=12, type=int)
    try:
        return jsonify(core.get_agent_performance(cfg, period=period, agent_name=agent_name, limit=limit))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.get("/api/scorecard")
def scorecard():
    agent_name = request.args.get("agent_name") or None
    start = request.args.get("start") or None
    end = request.args.get("end") or None
    try:
        return jsonify(core.get_scorecard(cfg, agent_name=agent_name, start=start, end=end))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.get("/api/leaderboard")
def leaderboard():
    period = request.args.get("period", default="daily")
    try:
        return jsonify(core.get_leaderboard(cfg, period=period))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


def _admin_authorized() -> bool:
    provided_key = request.headers.get("X-API-Key", "")
    return bool(cfg["sync_api_key"]) and provided_key == cfg["sync_api_key"]


# --- Agent (connected dialer) management -- each row is one WAVV API key,
# attributed to the agent whose dialer it belongs to. Gated behind the same
# admin key as /api/sync and /api/backfill since this manages credentials. ---

@app.get("/api/agents")
def list_agents():
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        return jsonify(core.list_agents(cfg))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.post("/api/agents")
def add_agent():
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    try:
        agent = core.create_agent(
            cfg, body.get("agent_name"), body.get("api_key"), body.get("base_url"), body.get("npn")
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001 - e.g. duplicate agent_name (unique constraint)
        return jsonify({"error": str(e)}), 400

    # Kick off a backfill right away so the newly connected dialer's history
    # starts flowing without an extra manual step.
    with _job_lock:
        if not _job_state["running"]:
            _job_state["running"] = True
            _job_state["last_started_at"] = datetime.now(timezone.utc).isoformat()
            threading.Thread(target=_run_job, args=("backfill",), daemon=True).start()

    return jsonify({"status": "added", "agent": agent}), 201


@app.patch("/api/agents/<agent_id>")
def edit_agent(agent_id):
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    fields = {k: v for k, v in body.items() if k in ("agent_name", "api_key", "base_url", "npn", "active")}
    if not fields:
        return jsonify({"error": "no updatable fields provided"}), 400
    try:
        ok = core.update_agent(cfg, agent_id, **fields)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "updated"})


@app.delete("/api/agents/<agent_id>")
def remove_agent(agent_id):
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401
    ok = core.delete_agent(cfg, agent_id)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "deleted"})


@app.get("/api/sync/status")
def sync_job_status():
    return jsonify(_job_state)


def _start_job(kind: str):
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401

    with _job_lock:
        if _job_state["running"]:
            return jsonify({"status": "already_running", "job": _job_state}), 409
        _job_state["running"] = True
        _job_state["last_started_at"] = datetime.now(timezone.utc).isoformat()
        threading.Thread(target=_run_job, args=(kind,), daemon=True).start()

    return jsonify({"status": "started", "kind": kind})


@app.post("/api/sync")
def trigger_sync():
    return _start_job("sync")


@app.post("/api/backfill")
def trigger_backfill():
    return _start_job("backfill")


if __name__ == "__main__":
    import os

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
