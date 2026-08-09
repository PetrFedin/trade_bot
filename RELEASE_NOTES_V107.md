# Release notes — 7.37.0

- Adds a signed single-attempt Kubernetes rollout actuator.
- Adds independent RELEASE/RISK attestations and controller/executor signatures.
- Adds durable replay, outbox, monotonic deployment fencing and recovery claims.
- Adds JSON Patch optimistic-lock tests and exact rollout completion gates.
- Adds GET-only reconciliation for ambiguous or interrupted mutations.
- Adds a strict Schema 106 successor verifier.
- Corrects the previously detected condition where the Schema 107 remote branch still pointed to the Schema 106 commit.

This release does not enable production mutation authorization, external order routing or live trading.

## Final qualification hardening

- HTTP 408 and unknown non-2xx PATCH results are treated as ambiguous and can only enter GET-only reconciliation.
- Recovery restores the durable mutation-attempt count instead of assuming a PATCH occurred.
- PostgreSQL stores `deployment_uid` with each execution and scopes fencing uniqueness to `(deployment_uid, fencing_token)`.
- Fixes invalid DDL that declared `astra_rollout_fence_v107.deployment_uid` twice and omitted `astra_rollout_execution_v107.deployment_uid` required by repository inserts and indexes.
- Adds structural migration-contract tests that reject duplicate top-level table columns and schema/repository drift.
- CI now applies the canonical migration twice to an isolated PostgreSQL 16 service with `ON_ERROR_STOP=1` and verifies the resulting `information_schema` contract.
- Stored command enum corruption is normalized to a fail-closed repository error.
- Recovery takeover requires stale execution state unless the command is already explicitly `UNCERTAIN`.
- CI runs both focused Schema 107 tests and the complete stacked regression suite.
