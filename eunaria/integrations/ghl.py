"""GoHighLevel (LeadConnector API v2) adapter.

Auth: a Private Integration token for the sub-account (GHL_PRIVATE_TOKEN) + GHL_LOCATION_ID.
Endpoints follow the documented v2 API (services.leadconnectorhq.com, Version: 2021-07-28).
Status: implemented against the documentation; NOT yet verified against a live account.
Note: the public API can list workflows and enroll contacts but cannot create/edit workflow logic.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

BASE = "https://services.leadconnectorhq.com"


class GHLError(RuntimeError):
    pass


class GHLClient:
    def __init__(self, token: str, location_id: str, version: str = "2021-07-28",
                 transport: httpx.BaseTransport | None = None):
        self.location_id = location_id
        self.http = httpx.Client(
            base_url=BASE,
            headers={"Authorization": f"Bearer {token}", "Version": version, "Accept": "application/json"},
            timeout=30,
            transport=transport,
        )

    def _req(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> dict:
        for attempt in range(4):
            r = self.http.request(method, path, params=params, json=json)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise GHLError(f"GHL {method} {path} failed ({r.status_code}) — {r.text[:300]}")
            return r.json() if r.content else {}
        raise GHLError(f"GHL {method} {path} failed after retries")

    # --- reads ----------------------------------------------------------------------------

    def search_contacts(self, query: str | None = None, limit: int = 20) -> list[dict]:
        body: dict[str, Any] = {"locationId": self.location_id, "pageLimit": min(limit, 100)}
        if query:
            body["query"] = query
        return self._req("POST", "/contacts/search", json=body).get("contacts", [])

    def get_contact(self, contact_id: str) -> dict:
        return self._req("GET", f"/contacts/{contact_id}").get("contact", {})

    def list_pipelines(self) -> list[dict]:
        return self._req("GET", "/opportunities/pipelines", params={"locationId": self.location_id}).get("pipelines", [])

    def search_opportunities(self, pipeline_id: str | None = None, status: str | None = None,
                             limit: int = 50) -> list[dict]:
        params: dict[str, Any] = {"location_id": self.location_id, "limit": min(limit, 100)}
        if pipeline_id:
            params["pipeline_id"] = pipeline_id
        if status:
            params["status"] = status
        return self._req("GET", "/opportunities/search", params=params).get("opportunities", [])

    def list_calendars(self) -> list[dict]:
        return self._req("GET", "/calendars/", params={"locationId": self.location_id}).get("calendars", [])

    def list_appointments(self, calendar_id: str, days_back: int = 1, days_ahead: int = 7) -> list[dict]:
        now = datetime.now(timezone.utc)
        params = {
            "locationId": self.location_id, "calendarId": calendar_id,
            "startTime": int((now - timedelta(days=days_back)).timestamp() * 1000),
            "endTime": int((now + timedelta(days=days_ahead)).timestamp() * 1000),
        }
        return self._req("GET", "/calendars/events", params=params).get("events", [])

    def list_workflows(self) -> list[dict]:
        return self._req("GET", "/workflows/", params={"locationId": self.location_id}).get("workflows", [])

    # --- writes (always behind owner approval) ----------------------------------------------

    def add_tags(self, contact_id: str, tags: list[str]) -> dict:
        return self._req("POST", f"/contacts/{contact_id}/tags", json={"tags": tags})

    def add_note(self, contact_id: str, body: str) -> dict:
        return self._req("POST", f"/contacts/{contact_id}/notes", json={"body": body})

    def create_task(self, contact_id: str, title: str, body: str, due_date: str) -> dict:
        return self._req("POST", f"/contacts/{contact_id}/tasks",
                         json={"title": title, "body": body, "dueDate": due_date, "completed": False})

    def update_opportunity_stage(self, opportunity_id: str, stage_id: str) -> dict:
        return self._req("PUT", f"/opportunities/{opportunity_id}", json={"pipelineStageId": stage_id})

    def create_contact(self, fields: dict) -> dict:
        return self._req("POST", "/contacts/", json={"locationId": self.location_id, **fields})
