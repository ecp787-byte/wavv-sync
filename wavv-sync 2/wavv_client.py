"""Thin client for the WAVV public API v3 (https://docs.wavv.com/).

Handles auth, pagination, and rate-limit/5xx retry with backoff.
"""

from __future__ import annotations

import time
from typing import Iterator, Optional

import requests


class WavvApiError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(f"WAVV API error {status_code} [{code}]: {message}")
        self.status_code = status_code
        self.code = code


class WavvClient:
    def __init__(self, api_key: str, base_url: str = "https://api.wavv.com/v3", timeout: int = 30):
        if not api_key:
            raise ValueError("WAVV_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            }
        )

    def _get(self, path: str, params: Optional[dict] = None, max_retries: int = 5) -> dict:
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            resp = self.session.get(url, params=params, timeout=self.timeout)

            if resp.status_code == 200:
                return resp.json()

            # Try to pull WAVV's structured error shape; fall back gracefully.
            try:
                body = resp.json()
                code = body.get("code", "UNKNOWN")
                message = body.get("error", resp.text)
            except ValueError:
                code = "UNKNOWN"
                message = resp.text

            retriable = resp.status_code == 429 or resp.status_code >= 500
            if retriable and attempt <= max_retries:
                # Exponential backoff with a floor; honor Retry-After if WAVV sends it.
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(2 ** attempt, 30)
                time.sleep(delay)
                continue

            raise WavvApiError(resp.status_code, code, message)

    def list_calls(
        self,
        direction: Optional[str] = None,
        campaign_id: Optional[str] = None,
        started_after: Optional[str] = None,
        started_before: Optional[str] = None,
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> dict:
        """One page of GET /calls."""
        params = {"limit": limit}
        if direction:
            params["direction"] = direction
        if campaign_id:
            params["campaignId"] = campaign_id
        if started_after:
            params["startedAfter"] = started_after
        if started_before:
            params["startedBefore"] = started_before
        if cursor:
            params["cursor"] = cursor
        return self._get("/calls", params=params)

    def iter_calls(
        self,
        direction: Optional[str] = None,
        campaign_id: Optional[str] = None,
        started_after: Optional[str] = None,
        started_before: Optional[str] = None,
        page_size: int = 200,
    ) -> Iterator[dict]:
        """Yield every call across all pages for the given filters, newest first."""
        cursor = None
        while True:
            page = self.list_calls(
                direction=direction,
                campaign_id=campaign_id,
                started_after=started_after,
                started_before=started_before,
                limit=page_size,
                cursor=cursor,
            )
            for call in page.get("data", []):
                yield call
            cursor = page.get("nextCursor")
            if not cursor:
                break

    def get_call(self, call_id: str) -> dict:
        return self._get(f"/calls/{call_id}")

    def get_transcript(self, call_id: str) -> dict:
        return self._get(f"/calls/{call_id}/transcript")

    def get_recording(self, call_id: str) -> dict:
        return self._get(f"/calls/{call_id}/recording")
