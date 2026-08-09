# Qualification summary — Schema 109

## Scope

This release qualifies the source, database contract and CI behavior for remote signer attestation. It does not qualify any production KMS/HSM, certificate chain, hardware firmware, audit sink or live trading path.

## Required deterministic gates

- PostgreSQL 16 migration applies twice without error.
- Schema 109 focused tests pass against real PostgreSQL.
- Full stacked regression passes.
- Branch-aware coverage over `app/runtime/*_v109.py` is at least 90%.
- Ruff, architecture and static security audits pass.
- Schema 108 predecessor release verification passes in successor mode.
- Schema 109 release identity verifies.
- 1,000-iteration / 8-worker claim, replay and policy-equivocation stress passes with zero failures.
- Canonical and packaged migrations are byte-identical.
- Request table has exactly one policy foreign-key contract and audit events have the append-only trigger.
- Production/live and automatic POST retry flags remain false.

## External blockers

See `LIVE_EXECUTION_STATUS_V109.json`. Until those controls are evidenced, the qualified state is `SOURCE_AND_CI_QUALIFICATION_ONLY`.
