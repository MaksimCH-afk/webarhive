"""WhoisJSON client: parsing, rate-limit floor, error mapping."""

from datetime import datetime

import httpx
import pytest

from webarhive.clients.whois import WhoisClient, _extract_registration_date


def test_extract_registration_date_common_keys():
    assert _extract_registration_date({"created": "2015-03-21"}) == datetime(2015, 3, 21)
    assert _extract_registration_date({"creation_date": "2018-11-02T10:00:00"}) == datetime(2018, 11, 2, 10)
    assert _extract_registration_date({}) is None


def test_extract_registration_date_rdap_events():
    payload = {"events": [{"eventAction": "registration", "eventDate": "2010-01-15"}]}
    assert _extract_registration_date(payload) == datetime(2010, 1, 15)


async def test_lookup_success_sets_remaining():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "").startswith("TOKEN=")
        return httpx.Response(
            200,
            headers={"Remaining-Requests": "987"},
            json={"created": "2012-06-01"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        c = WhoisClient(api_key="test", rate_limit=1000, client=inner, monthly_floor=10)
        r = await c.lookup("foo.com")
    assert r.status == "got"
    assert r.registration_date == datetime(2012, 6, 1)
    assert c.remaining == 987
    assert not c.exhausted


async def test_lookup_below_monthly_floor_marks_exhausted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Remaining-Requests": "5"},
            json={"created": "2020-01-01"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        c = WhoisClient(api_key="k", rate_limit=1000, client=inner, monthly_floor=10)
        r1 = await c.lookup("foo.com")
        # Next call should immediately return limit without hitting API.
        r2 = await c.lookup("bar.com")
    assert r1.status == "got"
    assert c.exhausted
    assert r2.status == "limit"


async def test_lookup_401_marks_fatal_config_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        c = WhoisClient(api_key="bad", rate_limit=1000, client=inner)
        r = await c.lookup("foo.com")
    assert r.status == "error"
    assert c.fatal_config_error


async def test_lookup_429_marks_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Remaining-Requests": "0"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        c = WhoisClient(api_key="k", rate_limit=1000, client=inner)
        r = await c.lookup("foo.com")
    assert r.status == "limit"
    assert c.exhausted


async def test_lookup_no_api_key_returns_not_configured():
    c = WhoisClient(api_key="", rate_limit=1000)
    r = await c.lookup("foo.com")
    assert r.status == "not_configured"
    await c.aclose()
