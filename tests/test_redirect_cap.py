"""Redirect cap sampling — большие пачки 3xx должны сэмплироваться."""

from unittest.mock import AsyncMock

import pytest

from webarhive.analysis.redirects import analyze_redirects
from webarhive.cdx.client import CdxRow


def _row(ts: str) -> CdxRow:
    return CdxRow(
        urlkey="com,foo)/", timestamp=ts, original=f"http://foo.com/?v={ts}",
        mimetype="text/html", statuscode="301", digest=f"d{ts}", length="0",
    )


def _ts_seq(n: int) -> list[str]:
    """Сгенерировать n валидных YYYYMMDD timestamp'ов подряд."""
    out: list[str] = []
    for i in range(n):
        year = 2010 + i // 365
        day_of_year = (i % 365) + 1
        # переведём в месяц/день
        from datetime import datetime, timedelta
        d = datetime(year, 1, 1) + timedelta(days=day_of_year - 1)
        out.append(d.strftime("%Y%m%d%H%M%S"))
    return out


async def test_redirect_cap_samples_large_lists():
    """500 редиректов с cap=50 → ровно 50 фетчей."""
    rows = [_row(ts) for ts in _ts_seq(500)]

    fetcher = AsyncMock()
    fetcher.fetch = AsyncMock(side_effect=Exception("skip"))  # быстро упасть, не важно
    events: list[str] = []

    async def progress(msg: str) -> None:
        events.append(msg)

    result = await analyze_redirects(
        rows, source_domain="foo.com",
        fetcher=fetcher, cap=50, progress=progress,
    )

    # 50 (сэмпл) + 0 (отбраковки нет, у всех ts парсятся)
    assert fetcher.fetch.await_count == 50
    assert len(result) == 50
    # Прогресс упомянул сэмплинг
    assert any("сэмплируем 50" in e for e in events), events
    # И финальный отчёт
    assert any("50/50" in e for e in events), events


async def test_redirect_cap_no_op_when_below():
    """40 редиректов с cap=150 → все 40, без сэмплинга."""
    rows = [_row(ts) for ts in _ts_seq(40)]

    fetcher = AsyncMock()
    fetcher.fetch = AsyncMock(side_effect=Exception("skip"))
    events: list[str] = []

    async def progress(msg: str) -> None:
        events.append(msg)

    result = await analyze_redirects(
        rows, source_domain="foo.com",
        fetcher=fetcher, cap=150, progress=progress,
    )
    assert fetcher.fetch.await_count == 40
    assert len(result) == 40
    # Сэмплинга не было
    assert not any("сэмплируем" in e for e in events)
