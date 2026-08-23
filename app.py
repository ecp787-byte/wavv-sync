#!/usr/bin/env python3
"""
Small web API in front of the WAVV -> Postgres sync tool.

Endpoints:
    GET  /api/health        -> {"ok": true}
    GET  /api/status        -> DB-level sync status (last synced call, row count)
    GET  /api/summary       -> daily rollup (calls, conversations, talk time) for a dashboard
    POST /api/sync          -> trigger an on-demand incremental sync (requires X-API-Key header)
    GET  /api/sync/status   -> status of the most recent manually-triggered sync job

Intended to sit behind a front-end (e.g. a Webflow page) that calls these over HTTPS.
Runs alongside the existing Render Cron Job, which handles the reliable hourly schedule;
this service adds a dashboard and a manual "sync now" button.
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
    """Serve the same dashboard used in Webflow, self-configured for same-origin use.
    Handy for checking the tool works before wiring it into a Webflow Embed element.
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
        "<title>WAVV Call Sync</title></head><body style='margin:0;padding:24px;"
        "background:#f9f9f7;min-height:100vh;'>" + fragment + "</body></html>"
    )
    return Response(page, mimetype="text/html")

# Single in-process job slot: this is a small internal tool, not a job queue.
# A lock keeps two manual triggers from racing each other.
_job_lock = threading.Lock()
_job_state = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_result": None,
    "last_error": None,
}


def _run_sync_job():
    try:
        result = core.do_incremental_sync(cfg)
        _job_state["last_result"] = result
        _job_state["last_error"] = None
    except Exception as e:  # noqa: BLE001 - surface any failure to the dashboard
        _job_state["last_error"] = str(e)
    finally:
        _job_state["running"] = False
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


@app.get("/api/sync/status")
def sync_job_status():
    return jsonify(_job_state)


@app.post("/api/sync")
def trigger_sync():
    provided_key = request.headers.get("X-API-Key", "")
    if not cfg["sync_api_key"] or provided_key != cfg["sync_api_key"]:
        return jsonify({"error": "unauthorized"}), 401

    with _job_lock:
        if _job_state["running"]:
            return jsonify({"status": "already_running", "job": _job_state}), 409
        _job_state["running"] = True
        _job_state["last_started_at"] = datetime.now(timezone.utc).isoformat()
        threading.Thread(target=_run_sync_job, daemon=True).start()

    return jsonify({"status": "started"})


if __name__ == "__main__":
    import os

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
