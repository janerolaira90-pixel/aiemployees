"""Meta (Facebook/Instagram/Ads) read tools for Victor. Only configured assets are accessible."""

from __future__ import annotations

from .base import ToolContext, ToolError, tool


def _meta(ctx: ToolContext):
    if ctx.app.meta is None:
        raise ToolError("Meta is not connected (META_ACCESS_TOKEN missing). Report this; never estimate metrics.")
    return ctx.app.meta


def _allowed(ctx: ToolContext, kind: str, asset_id: str | None) -> str:
    s = ctx.app.settings
    allowed = {"ig": s.meta_ig_user_ids, "page": s.meta_page_ids, "ads": s.meta_ad_account_ids}[kind]
    if not allowed:
        raise ToolError(f"No authorized {kind} assets configured (META_*_IDS).")
    if asset_id is None:
        return allowed[0]
    if asset_id not in allowed:
        raise ToolError(f"Asset {asset_id} is not in the authorized list: {list(allowed)}")
    return asset_id


@tool("meta_list_assets", "List authorized Meta assets and the token's granted permissions.", {})
def meta_list_assets(ctx: ToolContext, args: dict):
    meta, s = _meta(ctx), ctx.app.settings
    return {"granted_permissions": meta.debug_permissions(), "pages": list(s.meta_page_ids),
            "instagram_accounts": list(s.meta_ig_user_ids), "ad_accounts": list(s.meta_ad_account_ids)}


@tool("meta_instagram_media_insights",
      "Recent Instagram media with the insights the API returns for each (Reels include watch-time metrics "
      "where available). Missing metrics are reported as missing, never estimated.",
      {"ig_user_id": {"type": "string"}, "limit": {"type": "integer", "maximum": 50}})
def meta_instagram_media_insights(ctx: ToolContext, args: dict):
    meta = _meta(ctx)
    ig = _allowed(ctx, "ig", args.get("ig_user_id"))
    media = meta.instagram_media(ig, args.get("limit") or 20)
    return [{**m, "insights": meta.media_insights(m)} for m in media]


@tool("meta_page_post_insights", "Recent Facebook Page posts.", {"page_id": {"type": "string"},
                                                                 "limit": {"type": "integer", "maximum": 50}})
def meta_page_post_insights(ctx: ToolContext, args: dict):
    page = _allowed(ctx, "page", args.get("page_id"))
    return _meta(ctx).page_posts(page, args.get("limit") or 20)


@tool("meta_ad_insights", "Ad-level performance incl. video watch percentiles, for an authorized ad account.",
      {"ad_account_id": {"type": "string"},
       "date_preset": {"type": "string", "enum": ["last_7d", "last_14d", "last_30d", "last_90d", "this_month",
                                                  "last_month"]}})
def meta_ad_insights(ctx: ToolContext, args: dict):
    acct = _allowed(ctx, "ads", args.get("ad_account_id"))
    return _meta(ctx).ad_insights(acct, args.get("date_preset") or "last_7d")
