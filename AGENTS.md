# Working agreement for AI agents

This file is the entry point for any AI coding agent working in this repository —
Claude Code, Codex/ChatGPT, Cursor or otherwise. Read it before the first edit.

It does not restate the project rules. It points at the files that already hold
them and records the conventions that agents keep breaking when they start from
an empty context.

## The one rule that outranks everything

A green pipeline is evidence that covered software contracts hold. It is never
permission to route orders and never a profitability claim. `README.md` and
`docs/RELEASE_PROCESS.md` state this; nothing an agent does may soften it.

Strategy promotion, Demo qualification and live routing are separate states that
cannot substitute for one another.

## Where the truth lives

Do not describe system state in prose. Current status is generated, not written:

| Question | Authoritative source |
| --- | --- |
| Current system status | `CURRENT_SYSTEM_STATUS.json` (generated) |
| Status block in `README.md` | generated between `ASTRA_STATUS:BEGIN/END` — never hand-edit |
| Engineering qualification evidence | `qualification/engineering/*.json` |
| Strategy evidence | `qualification/strategy/*.json` |
| Release/authority boundary | `RELEASE_IDENTITY_V109.json`, `LIVE_EXECUTION_STATUS_V109.json` |
| Release and PR procedure | `docs/RELEASE_PROCESS.md` |
| Operator procedure | `docs/OPERATOR_RUNBOOK_V99.md`, `OPERATOR_RUNBOOK_V109.md` |

Regenerate and validate with:

```bash
make status          # tools/system_status.py --check && --show
```

`--check` fails when committed status artifacts drift from their sources. If it
fails, fix the source, do not edit the generated artifact.

## Versioned artifacts are immutable

Files matching `ENGINEERING_REPORT_V*.md`, `INTEGRATION_V*.md`,
`OPERATOR_RUNBOOK_V*.md`, `QUALIFICATION_SUMMARY_V*.md`, `RELEASE_NOTES_V*.md`,
`LIVE_EXECUTION_STATUS_V*.json` and `RELEASE_IDENTITY_V*.json` are historical
release evidence.

**Never delete, rename, rewrite or "tidy" them.** Each version's set is asserted
by name in `tools/architecture_audit_v<N>.py`, and
`.github/workflows/compatibility-release-audits.yml` runs the historical
regression across `tests/test_tools_v100.py` … `test_tools_v108.py`. Removing one
file turns the audit into `missing:<file>` and reddens CI.

They look like clutter in the repository root. They are not.

Introducing a new version `V<N>` means adding a complete set — the documents, the
`tools/{architecture_audit,static_audit,platform,stress}_v<N>.py` scripts, the
matching `tests/test_tools_v<N>.py`, and a `schema<N>-*.yml` workflow. Do not
start a new version to record ordinary work.

## Do not start a new file to report your work

The versioned ladder exists because of release history, not because each task
gets its own report. When you finish a task, update the existing document that
covers it. Adding `*_V110.md` to describe a bug fix is wrong.

## Branch and PR conventions

From `docs/RELEASE_PROCESS.md`:

1. Branch from the current canonical baseline unless a stacked dependency
   requires another base. `main` is the canonical integration branch.
2. Keep a PR bounded to one purpose. Never mix strategy research with
   execution-authority changes.
3. Update tests and authoritative documentation in the same change when
   behavior, contracts or readiness claims change.
4. Record the exact head SHA, and distinguish mocked / local / connected / Demo /
   production evidence.
5. Treat deterministic failures and newly exposed database failures as blockers.
   Do not rerun until green and do not hide them behind environment skips.

Existing branch naming in use: `agent/<topic>` for agent work, `p0-<n>/<topic>`
for readiness items, `phase0/<topic>`, `research/<topic>`.

## Verification

```bash
make install         # editable install with dev extras
make test            # suite without PostgreSQL (27 integration modules skip)
make lint            # ruff over the paths CI keeps clean
make typecheck       # mypy over app
make security        # bandit over app
```

PostgreSQL-backed work needs a database. `make test` silently skips those
modules, so a green `make test` does not prove integration behavior:

```bash
make db-up           # docker compose postgres, port 5433 by default
make test-pg-fresh   # reset schema, migrate, run the full suite
```

`make db-reset` destroys durable state and refuses a non-local DSN. Never point
it at anything but a local database.

If port 5433 is taken by another project's container, override
`POSTGRES_PORT`/`DSN` for your own run rather than stopping their container.

`make lint-all` surveys the whole tree including a known legacy backlog and is
expected to report findings. Do not gate on it and do not mass-fix it as a side
effect of an unrelated change.

## Handing off between agents

State lives in git, not in any agent's memory or in a local tool's private
directory. A handoff is: a pushed branch, a PR describing purpose and exact head
SHA, and the generated status surface. Anything an agent knows that is not
committed is lost at the session boundary — write it into the PR or the relevant
document under `docs/`.
