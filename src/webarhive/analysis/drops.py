"""Drop heuristic (spec §8).

Wayback has no explicit drop signal. We infer:
  - a long gap in archive activity (≥ DROP_MIN_GAP_DAYS, default ~365),
    AND
  - on resume, a sharp change in digest and/or topic category.

The more such breaks → the more re-drops. Per spec, this is presented
as an estimate, not a fact (the UI labels it "эвристика").

Layer separation:
  - `find_gaps()` is purely structural over captures
  - `score_drops()` combines gaps with the topic-epoch sequence to
    emit Drop records ready for the DB
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from webarhive.analysis.topics import TopicEpoch
from webarhive.llm.client import OpenRouterClient
from webarhive.llm.prompts import build_smart_drop_prompt

DROP_MIN_GAP_DAYS = 365  # spec mentions "year-two" silence


@dataclass(frozen=True, slots=True)
class Gap:
    before: datetime
    after: datetime
    days: int


def find_gaps(timestamps: Sequence[datetime], *, min_days: int = DROP_MIN_GAP_DAYS) -> list[Gap]:
    if len(timestamps) < 2:
        return []
    sorted_ts = sorted(timestamps)
    gaps: list[Gap] = []
    for prev, curr in zip(sorted_ts, sorted_ts[1:]):
        delta = (curr - prev).days
        if delta >= min_days:
            gaps.append(Gap(before=prev, after=curr, days=delta))
    return gaps


@dataclass(frozen=True, slots=True)
class DropSignal:
    gap_from: datetime
    gap_to: datetime
    gap_days: int
    category_before: str | None
    category_after: str | None
    is_drop: bool
    confidence: float | None
    reason: str
    source: str = "heuristic"


def _category_at(epochs: Sequence[TopicEpoch], at: datetime, *, side: str) -> str | None:
    """Find the epoch covering `at`. `side='before'` picks the last
    epoch ending ≤ at; `side='after'` picks the first starting ≥ at.
    Falls back to closest neighbour to avoid Nones when the gap sits
    inside a sparse region between epochs."""
    if not epochs:
        return None
    if side == "before":
        cand = [e for e in epochs if e.period_to <= at]
        if cand:
            return cand[-1].category
        # Fallback to first epoch with any captures before `at`
        for e in epochs:
            if e.period_from <= at:
                return e.category
        return None
    # after
    cand = [e for e in epochs if e.period_from >= at]
    if cand:
        return cand[0].category
    for e in reversed(epochs):
        if e.period_to >= at:
            return e.category
    return None


def score_drops(
    gaps: Sequence[Gap],
    epochs: Sequence[TopicEpoch],
) -> list[DropSignal]:
    """Apply the heuristic. Gap + category change → likely drop;
    gap without category change → likely just sparse archiving."""
    signals: list[DropSignal] = []
    for gap in gaps:
        cat_before = _category_at(epochs, gap.before, side="before")
        cat_after = _category_at(epochs, gap.after, side="after")

        if cat_before is None or cat_after is None:
            # Can't compare — present as a weak signal, not a drop.
            signals.append(DropSignal(
                gap_from=gap.before,
                gap_to=gap.after,
                gap_days=gap.days,
                category_before=cat_before,
                category_after=cat_after,
                is_drop=False,
                confidence=0.2,
                reason=f"длинный разрыв {gap.days} дн., тематику сравнить не вышло",
            ))
            continue

        if cat_before != cat_after:
            # Bigger gap → higher confidence, capped.
            conf = min(0.5 + (gap.days / 365.0) * 0.15, 0.9)
            signals.append(DropSignal(
                gap_from=gap.before,
                gap_to=gap.after,
                gap_days=gap.days,
                category_before=cat_before,
                category_after=cat_after,
                is_drop=True,
                confidence=conf,
                reason=(
                    f"разрыв {gap.days} дн. + смена тематики "
                    f"({cat_before} → {cat_after})"
                ),
            ))
        else:
            signals.append(DropSignal(
                gap_from=gap.before,
                gap_to=gap.after,
                gap_days=gap.days,
                category_before=cat_before,
                category_after=cat_after,
                is_drop=False,
                confidence=0.3,
                reason=f"разрыв {gap.days} дн., тематика сохранилась",
            ))
    return signals


async def smart_drop_assess(
    signals: Sequence[DropSignal],
    *,
    llm: OpenRouterClient,
    model: str,
) -> list[DropSignal]:
    """Spec §9.2 — only runs on already-suspicious gaps (is_drop=True
    OR category change unverifiable). Asks the model to confirm/deny
    based on the topics on both sides; replaces source="heuristic" with
    "llm" and updates confidence/reason. No-op on gaps that look
    clearly not-a-drop."""
    out: list[DropSignal] = []
    for s in signals:
        if not s.is_drop and (s.category_before == s.category_after):
            out.append(s)
            continue
        sys_p, usr_p = build_smart_drop_prompt(
            before={"category": s.category_before},
            after={"category": s.category_after},
            gap_days=s.gap_days,
        )
        resp = await llm.chat_json(model=model, system_prompt=sys_p, user_prompt=usr_p)
        parsed = resp.parsed or {}
        is_drop = bool(parsed.get("is_drop", s.is_drop))
        conf = parsed.get("confidence")
        try:
            conf_f = float(conf) if conf is not None else s.confidence
        except (TypeError, ValueError):
            conf_f = s.confidence
        reason = parsed.get("reason") if isinstance(parsed.get("reason"), str) else s.reason
        out.append(DropSignal(
            gap_from=s.gap_from, gap_to=s.gap_to, gap_days=s.gap_days,
            category_before=s.category_before, category_after=s.category_after,
            is_drop=is_drop, confidence=conf_f, reason=reason or s.reason,
            source="llm",
        ))
    return out
