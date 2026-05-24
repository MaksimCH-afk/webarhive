import httpx
import pytest

from webarhive.llm.client import OpenRouterClient, _try_parse_json


def test_strip_json_fences():
    text = "```json\n{\"category\": \"foo\"}\n```"
    assert _try_parse_json(text) == {"category": "foo"}


def test_extract_first_json_block_from_garbage():
    text = "blah blah {\"category\": \"foo\", \"confidence\": 0.5} trailing junk"
    assert _try_parse_json(text) == {"category": "foo", "confidence": 0.5}


def test_returns_none_for_non_json():
    assert _try_parse_json("just text, no json") is None
    assert _try_parse_json("") is None


async def test_chat_json_success_path():
    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "choices": [
                {"message": {"content": '{"category":"гемблинг_казино","confidence":0.9,"reason":"slots"}'}}
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_cost": 0.0001},
        }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        client = OpenRouterClient(api_key="test-key", client=inner)
        resp = await client.chat_json(model="m", system_prompt="s", user_prompt="u")
        assert resp.parsed is not None
        assert resp.parsed["category"] == "гемблинг_казино"
        assert resp.prompt_tokens == 12
        assert resp.cost_usd == pytest.approx(0.0001)


async def test_chat_json_handles_429_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"category":"не_определено","confidence":0.0}'}}],
                "usage": {},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        client = OpenRouterClient(api_key="key", client=inner, backoff_base=0.01, max_retries=3)
        resp = await client.chat_json(model="m", system_prompt="s", user_prompt="u")
        assert calls["n"] == 2
        assert resp.parsed["category"] == "не_определено"


async def test_chat_json_returns_error_on_fatal_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as inner:
        client = OpenRouterClient(api_key="key", client=inner, backoff_base=0.01, max_retries=2)
        resp = await client.chat_json(model="m", system_prompt="s", user_prompt="u")
        assert resp.error is not None
        assert resp.parsed is None
