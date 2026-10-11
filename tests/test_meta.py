"""Meta asset discovery and authorization."""

from __future__ import annotations

import httpx

from eunaria.agents import get as get_agent
from eunaria.integrations.meta import MetaClient
from eunaria.tools import ToolContext, execute


def _meta():
    def handler(request: httpx.Request):
        path = request.url.path.split("/", 2)[-1]
        if path == "me/accounts":
            return httpx.Response(200, json={"data": [
                {"id": "p1", "name": "Eunaria", "instagram_business_account": {"id": "ig1"}},
                {"id": "p2", "name": "No IG"}]})
        if path == "me/adaccounts":
            return httpx.Response(200, json={"data": [{"id": "act_9", "name": "Ads"}]})
        if path == "ig1/media":
            return httpx.Response(200, json={"data": [{"id": "m1", "media_product_type": "REELS"}]})
        if path == "m1/insights":
            return httpx.Response(200, json={"data": [{"name": "reach", "values": [{"value": 120}]}]})
        return httpx.Response(200, json={"error": {"type": "OAuthException", "code": 100, "message": path}})
    return MetaClient("tok", transport=httpx.MockTransport(handler))


def test_assets_are_discovered_from_the_token(make_app):
    app = make_app()
    app.meta = _meta()
    task = app.tasks.create(agent_id="victor", title="t")
    ctx = ToolContext(app=app, task=task, agent=get_agent("victor"))
    out, err = execute(ctx, "toolu_m1", "meta_instagram_media_insights", {})
    assert not err and '"reach": 120' in out
    out, err = execute(ctx, "toolu_m2", "meta_instagram_media_insights", {"ig_user_id": "someone-else"})
    assert err and "not in the authorized list" in out
