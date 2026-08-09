# ASTRA 7.33.0 / Schema 103 Engineering Report

Schema 103 introduces a production-oriented control-plane boundary for repeated read-only paper-sandbox qualification campaigns.

## Scope

- PostgreSQL-compatible due-campaign selection with `FOR UPDATE SKIP LOCKED`;
- generation and monotonically increasing fencing-token checks;
- expiring lease ownership and deployment heartbeat renewal;
- read-only probe plans restricted to `GET` and `HEAD` on allowlisted paper hosts;
- explicit rejection of all broker mutation evidence;
- resumable, contiguous, chunk-digested evidence upload;
- final-size and SHA-256 verification before evidence acceptance;
- retention deadlines, legal hold and append-only deletion tombstones;
- incident deduplication, severity escalation, acknowledgement and operator resolution;
- fail-closed recovery after stale heartbeat, expired lease, identity mismatch or evidence corruption;
- append-only hash-chain control-plane events;
- operator-confirmed cleanup before a blocked campaign can be released;
- production PostgreSQL adapter boundary without embedding a database driver or credentials.

## State machine

```text
CREATED -> READY -> LEASED -> PROBING -> UPLOADING -> READY/RETIRED
                       |          |          |
                       +----------+----------+-> BLOCKED
                                              -> QUARANTINED
```

`BLOCKED` requires explicit cleanup confirmation, resolution of critical incidents and an operator release. `QUARANTINED` has no automatic recovery path.

## Non-goals

Schema 103 does not add forecasting, signal generation, position sizing, pricing, live order routing or broker write operations.
