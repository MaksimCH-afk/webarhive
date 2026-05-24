"""Test the CDX client serialises both collapse=urlkey and collapse=digest
into the outgoing request (spec §3.1)."""

import httpx

from webarhive.cdx.client import CdxClient
from webarhive.cdx.throttle import IAThrottle


async def test_cdx_query_includes_both_collapse_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # Save the raw query string so we can verify the repeated param.
        captured["query"] = str(request.url.query.decode())
        captured["params"] = list(request.url.params.multi_items())
        # Return just the header (empty result).
        return httpx.Response(200, json=[
            ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
        ])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        client = CdxClient(throttle=IAThrottle(100), client=inner, backoff_base=0.01, max_retries=2)
        rows = await client.fetch_all("foo.com")
    assert rows == []
    collapses = [v for k, v in captured["params"] if k == "collapse"]
    assert collapses == ["urlkey", "digest"], (
        f"both collapse params required per spec §3.1, got {collapses}"
    )


async def test_cdx_match_type_propagates():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params.multi_items())
        return httpx.Response(200, json=[
            ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
        ])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        client = CdxClient(throttle=IAThrottle(100), client=inner, backoff_base=0.01, max_retries=2)
        await client.fetch_all("blog.foo.com", match_type="host")
    assert captured["params"]["matchType"] == "host"
