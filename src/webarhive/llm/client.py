"""LLM chat client (spec §3.3, §11).

- Model is ALWAYS a parameter — never hardcoded. Different roles can use
  different models (spec §9, §11).
- API key from env only (never logged or returned).
- Strict JSON expected; we parse and validate at the call site, raw
  response always saved for audit.
- Поддерживает два провайдера через единый chat-completions интерфейс:
  «openrouter» (по умолчанию, любой подключённый там провайдер) и
  «openai» (напрямую к api.openai.com). Класс называется OpenRouterClient
  по историческим причинам — он давно работает с любым endpoint в
  OpenAI-совместимом формате.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Поддерживаемые провайдеры → URL и поведение заголовков.
PROVIDER_ENDPOINTS = {
    "openrouter": OPENROUTER_URL,
    "openai": OPENAI_URL,
}


@dataclass(frozen=True, slots=True)
class LlmResponse:
    """Raw + parsed view of an LLM call.

    `parsed` may be None when the model returned non-JSON; the call site
    falls back to неопределённо and we still keep `raw_text` for audit.
    """
    raw_text: str
    parsed: dict[str, Any] | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None
    latency_ms: int
    model: str
    error: str | None = None


def _strip_json_fences(text: str) -> str:
    """Models sometimes wrap JSON in ```json ... ``` fences."""
    s = text.strip()
    if s.startswith("```"):
        # drop first line and trailing fence
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _try_parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = _strip_json_fences(text)
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        # Try to locate the first {...} block.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(cleaned[start : end + 1])
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
        return None


class OpenRouterClient:
    def __init__(
        self,
        *,
        api_key: str,
        app_domain: str = "checker.local",
        client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        per_call_timeout: float = 45.0,
        provider: str = "openrouter",
    ) -> None:
        if not api_key:
            raise ValueError("LLM API key is required")
        if provider not in PROVIDER_ENDPOINTS:
            raise ValueError(
                f"Unknown provider {provider!r}; expected one of {list(PROVIDER_ENDPOINTS)}"
            )
        self._api_key = api_key
        self._app_domain = app_domain
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._provider = provider
        self._endpoint = PROVIDER_ENDPOINTS[provider]
        # Wall-clock cap на ОДИН вызов chat_json (включая retry). Раньше
        # зависший вызов мог съесть до 3×60с tenacity-backoff. Теперь
        # выходим с error по истечении этого таймаута и пайплайн идёт
        # дальше — один сбойный вызов больше не блокирует фазу.
        self._per_call_timeout = per_call_timeout

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OpenRouterClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def chat_json(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 400,
    ) -> LlmResponse:
        """Call a chat-completions endpoint and try to parse JSON from
        the assistant's reply. Returns LlmResponse regardless of parse
        outcome; caller validates `parsed` against its enum.
        """
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            # OpenAI и большинство OpenRouter-провайдеров honour this.
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        # OpenRouter использует HTTP-Referer/X-Title для своей атрибуции.
        # OpenAI эти заголовки игнорирует, но и не ругается на них.
        if self._provider == "openrouter":
            headers["HTTP-Referer"] = f"https://{self._app_domain}"
            headers["X-Title"] = "webarhive-checker"

        start = time.monotonic()
        retryer = AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=self._backoff_base, min=self._backoff_base, max=30),
            retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError)),
            reraise=True,
        )

        async def _do_call() -> dict:
            async for attempt in retryer:
                with attempt:
                    resp = await self._client.post(self._endpoint, json=payload, headers=headers)
                    if resp.status_code == 429 or 500 <= resp.status_code < 600:
                        logger.warning("openrouter retryable status=%s", resp.status_code)
                        raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                    resp.raise_for_status()
                    return resp.json()
            raise RuntimeError("unreachable")  # pragma: no cover

        try:
            # Жёсткий wall-clock потолок на ВСЁ retry-окно, чтобы один
            # сбойный вызов не съел до 3×60с tenacity backoff.
            data = await asyncio.wait_for(_do_call(), timeout=self._per_call_timeout)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.exception("openrouter call failed")
            return LlmResponse(
                raw_text="",
                parsed=None,
                prompt_tokens=None,
                completion_tokens=None,
                cost_usd=None,
                latency_ms=elapsed_ms,
                model=model,
                error=f"{type(exc).__name__}: {exc}",
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        choices = data.get("choices") or []
        if not choices:
            return LlmResponse("", None, None, None, None, elapsed_ms, model, error="no choices")

        raw_text = (choices[0].get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        cost = usage.get("total_cost") or data.get("cost")

        return LlmResponse(
            raw_text=raw_text,
            parsed=_try_parse_json(raw_text),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cost_usd=float(cost) if cost is not None else None,
            latency_ms=elapsed_ms,
            model=model,
        )
