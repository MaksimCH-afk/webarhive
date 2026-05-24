# webarhive

Server-side domain checker over Internet Archive (Wayback Machine).

Takes a list of domains → returns per-domain picture: age in archive,
topic-epoch timeline, status-code timeline, redirect targets, suspected
drops, optional clean/nuanced/dirty verdict.

See `/root/.claude/uploads/.../domain_checker_spec.md` for the full
spec. Section anchors are referenced in docstrings as `(spec §N)`.

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env
# fill OPENROUTER_API_KEY in .env
pytest -q
```

## Architecture

Layered by responsibility:

| Package | Spec | Purpose |
|---------|------|---------|
| `config/` | §10–11 | Settings (env-driven), category enum |
| `domains/` | §2 | Loading + strict normalization |
| `cdx/` | §3, §14 | CDX Server API client with shared IA throttle |
| `analysis/` | §4–8 | History, topic epochs, redirects, drops |
| `fetcher/` | §3.2 | Snapshot fetch (`id_` for machine, plain for human) |
| `llm/` | §6, §9 | OpenRouter client with per-role models |
| `orchestrator/` | §2.1–2.2 | Run lifecycle, parallel queue, resumability |
| `db/` | §13 | SQLAlchemy models, Alembic migrations |
| `logging_/` | §12 | Trace + LLM audit (separate sinks) |
| `web/` | §15–17 | FastAPI dashboard, light theme, dense tables |

## Status

Bootstrap stage. See git log for what's wired up.
