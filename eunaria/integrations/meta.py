"""Meta Graph API adapter (read-only in this phase).

Auth: a System User access token from Meta Business Manager (META_ACCESS_TOKEN) with, at minimum,
pages_read_engagement, read_insights, instagram_basic, instagram_manage_insights, ads_read.
Only metrics the API actually returns are reported; unavailable metrics are listed as such.
Status: implemented against the Graph API documentation; NOT yet verified with a live token.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

# Reels/video metrics requested per media item; the API rejects unsupported ones per media type,
# so we request per type and record what came back.
REEL_METRICS = ("reach", "views", "likes", "comments", "shares", "saved", "total_interactions",
                "ig_reels_avg_watch_time", "ig_reels_video_view_total_time")
FEED_METRICS = ("reach", "views", "likes", "comments", "shares", "saved", "total_interactions")


class MetaError(RuntimeError):
    pass


class MetaClient:
    def __init__(self, token: str, version: str = "v23.0", transport: httpx.BaseTransport | None = None):
        self.token = token
        self.http = httpx.Client(base_url=f"https://graph.facebook.com/{version}", timeout=30, transport=transport)

    def _get(self, path: str, params: dict | None = None) -> dict:
        params = {**(params or {}), "access_token": self.token}
        for attempt in range(3):
            r = self.http.get(path, params=params)
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            data = r.json()
            if "error" in data:
                err = data["error"]
                raise MetaError(f"Meta {path}: {err.get('type')} {err.get('code')} — {err.get('message')}")
            return data
        raise MetaError(f"Meta {path} failed after retries")

    def debug_permissions(self) -> list[str]:
        data = self._get("/me/permissions")
        return [p["permission"] for p in data.get("data", []) if p.get("status") == "granted"]

    def list_pages(self) -> list[dict]:
        return self._get("/me/accounts", {"fields": "id,name,instagram_business_account"}).get("data", [])

    def instagram_media(self, ig_user_id: str, limit: int = 25) -> list[dict]:
        fields = "id,caption,media_type,media_product_type,permalink,timestamp"
        return self._get(f"/{ig_user_id}/media", {"fields": fields, "limit": limit}).get("data", [])

    def media_insights(self, media: dict) -> dict[str, Any]:
        metrics = REEL_METRICS if media.get("media_product_type") == "REELS" else FEED_METRICS
        out: dict[str, Any] = {}
        try:
            data = self._get(f"/{media['id']}/insights", {"metric": ",".join(metrics)}).get("data", [])
            for m in data:
                values = m.get("values") or [{}]
                out[m["name"]] = values[0].get("value", m.get("total_value", {}).get("value"))
        except MetaError as exc:
            out["_error"] = str(exc)
        return out

    def page_posts(self, page_id: str, limit: int = 25) -> list[dict]:
        return self._get(f"/{page_id}/posts", {"fields": "id,message,created_time,permalink_url", "limit": limit}).get("data", [])

    def ad_insights(self, ad_account_id: str, date_preset: str = "last_7d", level: str = "ad") -> list[dict]:
        fields = ("ad_name,campaign_name,impressions,reach,spend,clicks,ctr,cpc,actions,"
                  "video_p25_watched_actions,video_p50_watched_actions,video_p75_watched_actions,"
                  "video_p100_watched_actions,video_avg_time_watched_actions")
        acct = ad_account_id if ad_account_id.startswith("act_") else f"act_{ad_account_id}"
        return self._get(f"/{acct}/insights", {"fields": fields, "date_preset": date_preset,
                                              "level": level, "limit": 100}).get("data", [])
