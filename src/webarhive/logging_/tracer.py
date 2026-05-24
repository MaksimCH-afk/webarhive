"""Per-domain trace (spec §12.1).

Human-readable log of a single domain's processing journey. Kept in
the domain row (text column), exposed in the card and downloadable as
.txt. NOT the LLM audit — that's a separate sink in llm_calls table.

Goal per spec: lets you reconstruct what happened on this domain
post-mortem without re-running it.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class DomainTracer:
    """Thread-safe-enough (single asyncio task per domain) trace buffer."""

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self._buf = io.StringIO()
        self._started = datetime.utcnow()
        self._write("START", f"начало обработки {domain}")

    def _write(self, level: str, msg: str) -> None:
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level:>5}] {msg}\n"
        self._buf.write(line)
        # Mirror INFO+ to stdlib logging for the global trace (spec §12 LOG_LEVEL).
        if level in ("INFO", "WARN", "ERROR"):
            logger.log(
                {"INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR}.get(
                    level, logging.INFO
                ),
                "[%s] %s",
                self.domain,
                msg,
            )

    def info(self, msg: str) -> None:
        self._write("INFO", msg)

    def warn(self, msg: str) -> None:
        self._write("WARN", msg)

    def error(self, msg: str) -> None:
        self._write("ERROR", msg)

    def debug(self, msg: str) -> None:
        self._write("DEBUG", msg)

    def step(self, stage: str, msg: str = "") -> None:
        suffix = f" — {msg}" if msg else ""
        self._write("INFO", f">>> {stage}{suffix}")

    def cdx_summary(self, *, total: int, after_collapse: int, buckets: dict[str, int]) -> None:
        b = ", ".join(f"{k}:{v}" for k, v in buckets.items())
        self.info(
            f"CDX: получено {total} захватов → после схлопывания {after_collapse} версий "
            f"({b})"
        )

    def topics_plan(self, *, total_versions: int, shift_points: int, llm_budget: int) -> None:
        self.info(
            f"тематика: всего версий {total_versions}, точек сдвига {shift_points}, "
            f"бюджет LLM {llm_budget}"
        )

    def llm_call(self, *, role: str, model: str, tokens_in: int | None, latency_ms: int) -> None:
        self.info(
            f"LLM[{role}] model={model} latency={latency_ms}ms "
            f"tokens_in={tokens_in if tokens_in is not None else '?'}"
        )

    def finish(
        self,
        *,
        age_days: int | None,
        epochs: int,
        flags: int,
        verdict: str | None,
        partial: bool,
    ) -> None:
        parts = [
            f"возраст {age_days}д" if age_days is not None else "возраст ?",
            f"эпох {epochs}",
            f"флагов {flags}",
            f"вердикт {verdict or 'нет'}",
        ]
        if partial:
            parts.append("ЧАСТИЧНАЯ")
        elapsed = (datetime.utcnow() - self._started).total_seconds()
        self._write("INFO", "<<< готово: " + " | ".join(parts) + f" ({elapsed:.1f}s)")

    def text(self) -> str:
        return self._buf.getvalue()
