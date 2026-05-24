"""Final verdict (spec §9.1).

Two paths:
- ENABLE_VERDICT=False (no model call): verdict column stays None.
  Per spec we do NOT compute a deterministic verdict — only flags
  (risky-category icons, review-tagged redirects) are shown, and the
  canvas falls back to flag-driven highlighting.
- ENABLE_VERDICT=True: build a structured snapshot of the domain
  picture, send to the LLM, expect strict JSON
  {verdict: clean|nuanced|dirty, reason, key_flags}.

Flag aggregation (always done, regardless of the LLM toggle):
- risky_flag_count = number of risky-category epochs
- review_flag_count = number of «обратить внимание» redirects
These drive the strip icons on the canvas row.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from webarhive.analysis.drops import DropSignal
from webarhive.analysis.redirects import RedirectInfo
from webarhive.analysis.topics import TopicEpoch
from webarhive.config.categories import is_risky
from webarhive.db.models import RedirectClass, Verdict
from webarhive.llm.client import LlmResponse, OpenRouterClient
from webarhive.llm.prompts import build_verdict_prompt


@dataclass
class VerdictResult:
    verdict: Verdict | None = None
    reason: str | None = None
    key_flags: list[str] = field(default_factory=list)
    risky_flag_count: int = 0
    review_flag_count: int = 0
    llm_response: LlmResponse | None = None  # for audit


def _aggregate_flags(
    *, epochs: Sequence[TopicEpoch], redirects: Sequence[RedirectInfo]
) -> tuple[int, int, list[str]]:
    risky = [e.category for e in epochs if is_risky(e.category)]
    reviews = [r for r in redirects if r.classification is RedirectClass.REVIEW]
    flags: list[str] = []
    flags.extend(f"risky:{c}" for c in dict.fromkeys(risky))  # preserve order, dedup
    if reviews:
        flags.append(f"review_redirects:{len(reviews)}")
    return len(risky), len(reviews), flags


def _build_report(
    *,
    domain: str,
    age_days: int | None,
    epochs: Sequence[TopicEpoch],
    redirects: Sequence[RedirectInfo],
    drops: Sequence[DropSignal],
    partial: bool,
) -> dict:
    return {
        "domain": domain,
        "age_days": age_days,
        "partial_check": partial,
        "epochs": [
            {
                "from": e.period_from.isoformat(),
                "to": e.period_to.isoformat(),
                "category": e.category,
                "confidence": e.confidence,
                "reason": e.reason,
                "versions": e.versions_in_epoch,
            }
            for e in epochs
        ],
        "redirects": [
            {
                "at": r.captured_at.isoformat(),
                "from": r.from_url,
                "to": r.to_url,
                "target_domain": r.target_domain,
                "class": r.classification.value,
                "reason": r.reason,
            }
            for r in redirects
        ],
        "drops": [
            {
                "from": d.gap_from.isoformat(),
                "to": d.gap_to.isoformat(),
                "gap_days": d.gap_days,
                "category_before": d.category_before,
                "category_after": d.category_after,
                "is_drop": d.is_drop,
                "confidence": d.confidence,
                "reason": d.reason,
            }
            for d in drops
        ],
    }


def _coerce_verdict(parsed: dict | None) -> tuple[Verdict | None, str | None, list[str]]:
    if not parsed:
        return None, None, []
    raw = parsed.get("verdict")
    mapping = {
        "чистый": Verdict.CLEAN, "clean": Verdict.CLEAN,
        "есть_нюансы": Verdict.NUANCED, "нюансы": Verdict.NUANCED, "nuanced": Verdict.NUANCED,
        "грязный": Verdict.DIRTY, "dirty": Verdict.DIRTY,
    }
    verdict = mapping.get(str(raw).strip().lower())
    reason = parsed.get("reason") if isinstance(parsed.get("reason"), str) else None
    raw_flags = parsed.get("key_flags") or []
    key_flags = [str(x) for x in raw_flags if isinstance(x, (str, int, float))]
    return verdict, reason, key_flags


async def make_verdict(
    *,
    enabled: bool,
    domain: str,
    age_days: int | None,
    epochs: Sequence[TopicEpoch],
    redirects: Sequence[RedirectInfo],
    drops: Sequence[DropSignal],
    partial: bool,
    llm: OpenRouterClient | None = None,
    model: str | None = None,
) -> VerdictResult:
    risky_count, review_count, baseline_flags = _aggregate_flags(epochs=epochs, redirects=redirects)
    result = VerdictResult(
        risky_flag_count=risky_count,
        review_flag_count=review_count,
        key_flags=list(baseline_flags),
    )

    if not enabled:
        # Spec §9.1: when off, no synthetic baseline verdict. Flags only.
        return result

    if llm is None or model is None:
        # Configured to use LLM but client wasn't provided — degrade gracefully.
        result.reason = "LLM-вердикт включён в настройках, но клиент не подключён"
        return result

    sys_p, usr_p = build_verdict_prompt(_build_report(
        domain=domain, age_days=age_days, epochs=epochs,
        redirects=redirects, drops=drops, partial=partial,
    ))
    response = await llm.chat_json(model=model, system_prompt=sys_p, user_prompt=usr_p, max_tokens=600)
    result.llm_response = response
    verdict, reason, key_flags = _coerce_verdict(response.parsed)
    if verdict is not None:
        result.verdict = verdict
    result.reason = reason or result.reason
    # Merge baseline flags with LLM-supplied ones (dedup preserving order).
    seen: set[str] = set()
    merged: list[str] = []
    for f in result.key_flags + key_flags:
        if f not in seen:
            seen.add(f)
            merged.append(f)
    result.key_flags = merged
    return result
