"""ORM models (spec §13).

Tables:
- runs       — run metadata + frozen settings snapshot (spec §11)
- domains    — per-domain result inside a run
- epochs     — topic-epoch lane (period → category)
- redirects  — redirect classifications with snapshot URL
- drops      — heuristic drop signals (spec §8)
- llm_calls  — full audit of LLM invocations (spec §12.2)

Heavy raw page content is NOT stored. `input_text` in llm_calls is
gzipped + truncated to TEXT_LIMIT (spec §13 sizing).
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    FINISHED = "finished"
    ABORTED = "aborted"
    ERROR = "error"


class DomainStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    PARTIAL = "partial"      # hit LLM-call budget (spec §11, §19)
    ERROR = "error"
    NO_DATA = "no_data"      # not in archive

class Verdict(str, enum.Enum):
    CLEAN = "clean"
    NUANCED = "nuanced"
    DIRTY = "dirty"


class RedirectClass(str, enum.Enum):
    TECHNICAL = "technical"          # www↔root, http↔https — uninteresting
    SAME_SITE = "same_site"          # same root name, topic matches
    COMPANY_MOVE = "company_move"    # different name, brand evidence
    REVIEW = "review"                # «обратить внимание» (spec §7)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.RUNNING.value, index=True)

    total_domains: Mapped[int] = mapped_column(Integer, default=0)
    processed_domains: Mapped[int] = mapped_column(Integer, default=0)
    clean_count: Mapped[int] = mapped_column(Integer, default=0)
    nuanced_count: Mapped[int] = mapped_column(Integer, default=0)
    dirty_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)

    # Frozen snapshot of all relevant settings at run start (spec §11).
    # Stored as JSON: models per role, role flags, limits, throttle, input opts.
    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Operator-supplied note, if any.
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)

    domains: Mapped[list[Domain]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(16), default=DomainStatus.PENDING.value, index=True)
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    verdict_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict_key_flags: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)

    # History summary (cheap, from CDX directly)
    first_capture_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_capture_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    age_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_captures: Mapped[int] = mapped_column(Integer, default=0)
    total_versions: Mapped[int] = mapped_column(Integer, default=0)  # after digest collapse

    # Counters used for the canvas row icons (spec §15 экран 2)
    risky_flag_count: Mapped[int] = mapped_column(Integer, default=0)
    review_flag_count: Mapped[int] = mapped_column(Integer, default=0)

    # Live trace text (spec §12.1) — kept inline for in-card view.
    # Compressed in production via DB-side; for SQLite kept as Text.
    trace: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped[Run] = relationship(back_populates="domains")
    epochs: Mapped[list[Epoch]] = relationship(back_populates="domain_ref", cascade="all, delete-orphan")
    redirects: Mapped[list[Redirect]] = relationship(back_populates="domain_ref", cascade="all, delete-orphan")
    drops: Mapped[list[Drop]] = relationship(back_populates="domain_ref", cascade="all, delete-orphan")
    llm_calls: Mapped[list[LlmCall]] = relationship(back_populates="domain_ref", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_domain_run_domain", "run_id", "domain"),
    )


class Epoch(Base):
    __tablename__ = "epochs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id", ondelete="CASCADE"), index=True)
    period_from: Mapped[datetime] = mapped_column(DateTime)
    period_to: Mapped[datetime] = mapped_column(DateTime)
    category: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Representative snapshot (for opening one capture from the epoch)
    sample_snapshot_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    versions_in_epoch: Mapped[int] = mapped_column(Integer, default=1)

    domain_ref: Mapped[Domain] = relationship(back_populates="epochs")


class Redirect(Base):
    __tablename__ = "redirects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id", ondelete="CASCADE"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime)
    from_url: Mapped[str] = mapped_column(Text)
    to_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    classification: Mapped[str] = mapped_column(String(20), index=True)  # RedirectClass
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # without id_ — for human

    domain_ref: Mapped[Domain] = relationship(back_populates="redirects")


class Drop(Base):
    __tablename__ = "drops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id", ondelete="CASCADE"), index=True)
    gap_from: Mapped[datetime] = mapped_column(DateTime)
    gap_to: Mapped[datetime] = mapped_column(DateTime)
    gap_days: Mapped[int] = mapped_column(Integer)
    category_before: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category_after: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_drop: Mapped[bool] = mapped_column(default=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="heuristic")  # heuristic|llm

    domain_ref: Mapped[Domain] = relationship(back_populates="drops")


class LlmCall(Base):
    """Per-call audit row (spec §12.2). Doctrine: doctorate-grade proof
    of why a domain ended up where it did. Comparable across models."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    domain_id: Mapped[int | None] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    role: Mapped[str] = mapped_column(String(20), index=True)  # classification|verdict|smart_drop|redirect
    model: Mapped[str] = mapped_column(String(128), index=True)
    snapshot_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Input text (the actual text the model saw). Stored gzipped, ≤TEXT_LIMIT
    # of original characters. Spec §13: keep the audit but don't pile terabytes.
    input_text_gz: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    domain_ref: Mapped[Domain | None] = relationship(back_populates="llm_calls")

    __table_args__ = (
        Index("ix_llm_role_model", "role", "model"),
    )


# Helper to enforce unique domain-per-run when caller wants strict mode.
# Spec §2.2: "re-check": if domain seen in previous runs, check again — no
# blocking, no merging. Dedup is intra-run only.
UniqueConstraint("run_id", "domain", name="uq_domain_per_run")
