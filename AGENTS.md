# AGENTS.md — Project Agent Instructions

This file is the always-on entry point for the Antigravity agent in this workspace.

## Mandatory: Read the full protocol

Before any action, read **@docs/AGENT_PROTOCOL.md** completely.

All rules R-01 through R-10 are binding and non-negotiable.

## Critical summary (for quick reference)

- **R-01 — One phase at a time.** Do not start Phase N+1 until Phase N is approved.

- **R-02 — Approval before start.** Present a phase plan (target files, architectural decisions, acceptance criteria) and wait for explicit confirmation.

- **R-03 — Approval before completion.** Paste raw evidence of every acceptance criterion, then wait for approval. Never claim "done" without raw output.

- **R-04 — No moving goalposts.** Never rewrite acceptance criteria after implementation. If they are wrong, stop and ask.

- **R-05 — No unapproved dependencies.** Do not add packages, databases, services, or CLI tools without explicit approval.

- **R-06 — No automatic external API calls.** Do not execute scripts that hit external APIs (Gemini, Telegram, RSS) without explicit approval.

- **R-07 — Reference docs are sacred.** Never modify without approval: `docs/ARCHITECTURE.md`, `docs/DATABASE_SCHEMA.md`, `docs/EDITORIAL_POLICY.md`, `docs/PROJECT_OVERVIEW.md`, `docs/AGENT_PROTOCOL.md`, `docs/ROADMAP.md`.

- **R-08 — No phase status changes.** Do not change phase status in `docs/ROADMAP.md` without approval.

- **R-09 — No test manipulation.** Do not modify, skip, or delete tests to hide failures.

- **R-10 — Stop and ask when in doubt.** Never guess and proceed.

## Fixed architecture & performance invariants

Established by `623afab` and `465755c`. They are permanent rules, not tunables: do not revert, "simplify" or re-tune them without an explicitly approved task that names the invariant it changes.

- **Three-layer Gemini model chain (`623afab`).** Every AI request walks `gemini-3.5-flash-lite` (primary) -> `gemini-3.1-flash-lite` (fallback) -> `gemini-3.8-flash` (tertiary, `GEMINI_TERTIARY_MODEL`, default `gemini-3.8-flash`). Only a retryable error (`429`/`503`) advances to the next model, duplicate names are dropped from the chain, and `GEMINI_ENABLE_FALLBACK=false` keeps only the primary. The tertiary layer exists so a run survives the simultaneous unavailability of both Lite models instead of aborting the batch.
- **Event-driven slot refill in `_ConcurrentAnalysis` (`465755c`).** A finished attempt dispatches the next one from its completion callback (`_on_attempt_done`); the old serial `next_attempt()` loop must not come back, because then one slow article idles every free slot (head-of-line blocking). Results are still delivered to the consumer in queue order - event matching depends on that order - so only fetching and analysis are parallel, never the final hand-off. The caps stay fixed: `ai_processing_concurrency=4` and the rotator's `max_concurrent=5` exist to protect the Aiven free-tier database; raising them is not an optimization.
- **No scraping of Google News redirect URLs (`465755c`).** `news.google.com/rss/articles/...` and `/articles/...` are consent/interstitial stubs, not articles. `is_google_news_wrapper_url()` in `app/collectors/rss_collector.py` short-circuits both `fetch_article_full_text()` and the `fetch_feed` loop, so those items use the RSS `title`/`summary` only: no HTTP GET, no `trafilatura` call, no body stored.

## Evidence requirements

| Work type | Required evidence |

|---|---|

| Code | full `pytest -v` output + full `pytest --cov=app --cov-report=term-missing` + `ruff check` |

| Migration | real `alembic upgrade head` on PostgreSQL + `\\dt` output + empty `alembic revision --autogenerate` |

| Config | `python -c "from app.config import settings; print(settings)"` without `.env` + `pip check` |

| Requirements | `pip install -r requirements.txt` + `pip check` + `pip freeze` |

**Never** summarize test results without pasting raw output.

## Git workflow

- Commit after each approved unit of work with a descriptive message.

- Do not `git push` without explicit approval.

- Do not `git reset --hard` or `git checkout` over uncommitted work without asking.

