# CLAUDE.md

The working agreement for this repository is **[AGENTS.md](AGENTS.md)**. Read it
first; it applies to Claude Code exactly as it does to Codex and Cursor.

Kept as a pointer on purpose — a second copy of the rules would drift from the
first.

## Claude Code specifics

- Prefer `make` targets over ad-hoc commands so local runs match CI.
- `make test` skips the PostgreSQL-backed modules. Do not report integration
  work as verified on its result alone — use `make db-up && make test-pg-fresh`.
- Never hand-edit the block between `ASTRA_STATUS:BEGIN` and `ASTRA_STATUS:END`
  in `README.md`, or any `CURRENT_SYSTEM_STATUS.json` field. Run `make status`.
- The `V*` files in the repository root are load-bearing release evidence, not
  clutter. `AGENTS.md` explains why they must not be removed.
