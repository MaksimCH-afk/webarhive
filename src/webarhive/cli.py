"""CLI entry-points (spec §2 — engine always works with a queue;
single-domain is a queue of length 1).

Usage:
    webarhive run foo.com bar.com
    webarhive run --file domains.txt
    webarhive scan foo.com           # dry CDX-only summary (no LLM, no DB)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click

from webarhive.cdx.client import CdxClient
from webarhive.cdx.throttle import IAThrottle
from webarhive.config import get_settings


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
def main() -> None:
    """webarhive — domain checker over Wayback Machine."""


@main.command()
@click.argument("domain")
def scan(domain: str) -> None:
    """One-shot CDX scan: print status histogram + age. No LLM, no DB.

    Useful for quickly sanity-checking IA connectivity and a domain's
    archive footprint before launching a full run.
    """
    settings = get_settings()
    _setup_logging(settings.log_level)

    async def _go() -> None:
        from webarhive.analysis.history import summarize_history
        throttle = IAThrottle(rate=settings.ia_rate_limit)
        async with CdxClient(
            throttle=throttle,
            max_retries=settings.ia_max_retries,
            backoff_base=settings.ia_backoff,
        ) as cdx:
            rows = await cdx.fetch_all(
                domain,
                match_type="host" if settings.check_subdomains else "domain",
            )
        summary = summarize_history(rows)
        out = {
            "domain": domain,
            "total_captures": summary.total_captures,
            "first_capture": summary.first_capture_at.isoformat() if summary.first_capture_at else None,
            "last_capture": summary.last_capture_at.isoformat() if summary.last_capture_at else None,
            "age_days": summary.age_days,
            "buckets": {k: len(v) for k, v in summary.by_bucket.items()},
        }
        click.echo(json.dumps(out, indent=2, ensure_ascii=False))

    asyncio.run(_go())


@main.command()
@click.argument("domains", nargs=-1)
@click.option("--file", "file_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="Load domains from a .txt/.csv/.xlsx file (first column).")
@click.option("--note", default=None, help="Operator note for this run.")
def run(domains: tuple[str, ...], file_path: Path | None, note: str | None) -> None:
    """Launch a full pipeline run on the given domain list."""
    from webarhive.domains.loader import load_from_bytes, load_from_text
    from webarhive.orchestrator.runner import run_pipeline, start_run

    settings = get_settings()
    _setup_logging(settings.log_level)

    if file_path:
        report = load_from_bytes(
            file_path.name, file_path.read_bytes(),
            check_subdomains=settings.check_subdomains,
        )
    else:
        if not domains:
            click.echo("error: pass domains as args or use --file", err=True)
            sys.exit(2)
        report = load_from_text(
            "\n".join(domains),
            check_subdomains=settings.check_subdomains,
        )

    click.echo(
        f"загружено строк {report.raw_lines} → "
        f"валидных уникальных {len(report.valid_unique)} → "
        f"отброшено {report.dropped}"
    )
    if not report.valid_unique:
        sys.exit(1)

    snap = settings.snapshot()

    async def _go() -> None:
        run_id = await start_run(
            domains=report.valid_unique,
            settings_snapshot=snap,
            note=note,
        )
        click.echo(f"run_id={run_id}")
        await run_pipeline(
            run_id=run_id,
            settings_snapshot=snap,
            api_key=settings.openrouter_api_key,
        )
        click.echo(f"run {run_id} finished")

    asyncio.run(_go())


if __name__ == "__main__":
    main()
