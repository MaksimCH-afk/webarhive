"""Per-domain trace (spec §12.1).

Human-readable log of a single domain's processing journey. Kept in
the domain row (text column), exposed in the card and downloadable as
.txt. NOT the LLM audit — that's a separate sink in llm_calls table.

Goal per spec: lets you reconstruct what happened on this domain
post-mortem without re-running it. Crucially, we flush to the DB
ON EVERY WRITE — so the operator can watch progress live, and if a
worker hangs we still see *where* it hung instead of an empty box.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Type for the async flush callback the orchestrator hands us.
FlushFn = Callable[[str], Awaitable[None]]


class DomainTracer:
    """Per-domain trace buffer with optional live flush to DB.

    Each `info/warn/step/...` call appends a line AND schedules a
    background flush. We use create_task so the line shows up in the
    DB without blocking the worker that's writing it.
    """

    def __init__(self, domain: str, flush_fn: FlushFn | None = None) -> None:
        self.domain = domain
        self._buf = io.StringIO()
        self._started = datetime.utcnow()
        self._flush_fn = flush_fn
        # Track in-flight flushes so we can await them on shutdown.
        self._pending: set[asyncio.Task] = set()
        self._write("START", f"начало обработки {domain}")

    # ----- write helpers -----

    def _schedule_flush(self) -> None:
        if self._flush_fn is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._flush_fn(self.text()))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _write(self, level: str, msg: str) -> None:
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level:>5}] {msg}\n"
        self._buf.write(line)
        # Mirror INFO+ to stdlib logging for the global server log.
        if level in ("INFO", "WARN", "ERROR"):
            logger.log(
                {"INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR}.get(
                    level, logging.INFO
                ),
                "[%s] %s",
                self.domain,
                msg,
            )
        # Live-flush to DB so the card page can read this line right away.
        self._schedule_flush()

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

    # ----- structured shortcuts -----

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

    async def drain(self) -> None:
        """Wait for any in-flight flushes to complete. Call before the
        worker exits to make sure the final trace lands in DB."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
