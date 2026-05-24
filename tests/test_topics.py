from dataclasses import dataclass

from webarhive.analysis.topics import classify_topics
from webarhive.cdx.client import CdxRow
from webarhive.fetcher.parser import ParsedPage
from webarhive.llm.client import LlmResponse


def _row(ts: str, digest: str = "X" * 32) -> CdxRow:
    return CdxRow(
        urlkey="com,foo)/", timestamp=ts, original=f"http://foo.com/?t={ts}",
        mimetype="text/html", statuscode="200", digest=digest, length="100",
    )


@dataclass
class FakeFetcher:
    """Returns a canned ParsedPage per (timestamp). We bypass the
    real parser by patching `parse_html` indirectly: classify_topics
    calls fetcher.fetch then parses. To keep this test focused on the
    pipeline logic, we monkey-patch the module's fetch helpers."""
    pages: dict[str, tuple[str, str, str, str]]  # ts -> (title, desc, h1, body)

    async def fetch(self, timestamp: str, url: str):
        # Doesn't get called — we patch the helpers.
        raise AssertionError("should not be called in this test")


class StubLlm:
    def __init__(self, mapping):
        self.mapping = mapping  # title-fragment -> (category, confidence, reason)
        self.calls = 0

    async def chat_json(self, *, model, system_prompt, user_prompt, **kw):
        self.calls += 1
        for needle, (cat, conf, reason) in self.mapping.items():
            if needle in user_prompt:
                return LlmResponse(
                    raw_text=f'{{"category":"{cat}","confidence":{conf},"reason":"{reason}"}}',
                    parsed={"category": cat, "confidence": conf, "reason": reason},
                    prompt_tokens=10, completion_tokens=5, cost_usd=0.0001,
                    latency_ms=50, model=model,
                )
        return LlmResponse("{}", {"category": "не_определено", "confidence": 0.0},
                           1, 1, 0.0, 1, model)


async def test_classify_topics_merges_consecutive_into_epochs(monkeypatch):
    rows = [_row("20100101000000", "A" * 32),
            _row("20120101000000", "B" * 32),
            _row("20180101000000", "C" * 32)]

    pages_light = {
        rows[0].timestamp: ParsedPage("Pizza shop online", "best pizza", "Order pizza", ""),
        rows[1].timestamp: ParsedPage("Pizza shop online", "best pizza", "Order pizza", ""),
        # Third version: totally different — should trigger shift.
        rows[2].timestamp: ParsedPage("Casino slots roulette bonus", "win big", "Top casino", ""),
    }
    pages_heavy = {
        rows[0].timestamp: ParsedPage("Pizza shop online", "best pizza", "Order pizza",
                                       "We deliver pizza fast"),
        rows[2].timestamp: ParsedPage("Casino slots roulette bonus", "win big", "Top casino",
                                       "Spin and win at our casino"),
    }

    from webarhive.analysis import topics as topics_mod

    async def fake_light(_fetcher, row):
        return pages_light[row.timestamp]

    async def fake_heavy(_fetcher, row, *, text_limit):
        return pages_heavy.get(row.timestamp)

    monkeypatch.setattr(topics_mod, "_fetch_light", fake_light)
    monkeypatch.setattr(topics_mod, "_fetch_heavy", fake_heavy)

    llm = StubLlm({
        "Pizza shop online": ("коммерция_магазин", 0.9, "pizza ecom"),
        "Casino slots": ("гемблинг_казино", 0.95, "casino"),
    })

    result = await classify_topics(
        rows,
        fetcher=FakeFetcher({}),
        llm=llm,
        model="dummy",
        text_limit=200,
        title_shift_threshold=2,
        max_llm_calls=10,
    )

    # Two LLM calls only (initial + the one shift point), not three.
    assert llm.calls == 2
    cats = [e.category for e in result.epochs]
    assert cats == ["коммерция_магазин", "гемблинг_казино"]
    # Two pizza versions merged into one epoch.
    assert result.epochs[0].versions_in_epoch == 2
    assert result.epochs[1].versions_in_epoch == 1
    assert "гемблинг_казино" in result.risky_categories
    assert not result.partial


async def test_classify_topics_budget_marks_partial(monkeypatch):
    rows = [_row(f"201{i}0101000000", chr(65 + i) * 32) for i in range(5)]

    from webarhive.analysis import topics as topics_mod

    async def fake_light(_fetcher, row):
        # Every version is distinct → all shift → all want LLM calls
        return ParsedPage(f"title_{row.timestamp}", "", "", "")

    async def fake_heavy(_fetcher, row, *, text_limit):
        return ParsedPage(f"title_{row.timestamp}", "", "", f"body for {row.timestamp}")

    monkeypatch.setattr(topics_mod, "_fetch_light", fake_light)
    monkeypatch.setattr(topics_mod, "_fetch_heavy", fake_heavy)

    llm = StubLlm({})  # always returns не_определено

    result = await classify_topics(
        rows,
        fetcher=FakeFetcher({}),
        llm=llm,
        model="dummy",
        text_limit=100,
        title_shift_threshold=0,  # everything is a shift
        max_llm_calls=2,
    )
    assert llm.calls == 2
    assert result.partial is True


async def test_empty_input_returns_empty_result():
    result = await classify_topics(
        [],
        fetcher=FakeFetcher({}),
        llm=StubLlm({}),
        model="m",
        text_limit=100,
        title_shift_threshold=2,
        max_llm_calls=10,
    )
    assert result.epochs == [] and result.versions == []
