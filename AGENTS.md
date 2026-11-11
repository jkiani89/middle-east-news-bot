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

