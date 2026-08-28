# Bybit Demo controlled runtime-lease recovery

## Why this exists

The canonical v119 runtime lease intentionally has no TTL and no automatic stale takeover. A hard-killed process can therefore leave a lease row after the process is gone. That blocks both the persistent supervisor and any later operator-approved entry action, which is safer than allowing a second writer.

v123 adds an explicit operator recovery path without weakening that invariant.

## Non-negotiable rules

Recovery is **not** based on lease age, PID age, wall-clock timeout, heartbeat expiry, or a guessed dead process.

The recovery API requires all of the following:

1. v119-v123 schema and append-only triggers are present.
2. The latest durable v121 control event is exactly `HALT_NEW_ENTRIES`.
3. The operator first runs `inspect` and obtains the SHA-256 fingerprint of the current lease owner.
4. The operator independently proves the previous process is stopped using the service manager / container platform / host evidence outside this database.
5. Recovery receives that exact lease-owner fingerprint, operator identity, bounded reason, non-secret process-stop evidence reference, and the exact confirmation phrase.
6. PostgreSQL locks the v119 lease table before the v121 control table, matching the existing ARM lock order and preventing a recovery/ARM deadlock.
7. In one transaction PostgreSQL inserts immutable v123 recovery audit and deletes only the exact raw owner token that produced the confirmed fingerprint.
8. The raw owner token never appears in the CLI, artifact, or v123 audit table.
9. An active excursion checkpoint is never deleted by lease recovery.
10. Recovery does not ARM new entries. The control plane remains HALTED after success.

## Schema guarantees

The v123 journal stores:

- deterministic recovery id;
- exact SHA-256 fingerprint of the recovered lease owner;
- lease creation timestamp and process id for audit;
- operator id and reason;
- external process-stop evidence reference;
- exact v121 HALT event id;
- whether an active checkpoint existed;
- SHA-256 of its entry orderLinkId when present;
- immutable safety markers.

Two database triggers reject:

- UPDATE or DELETE;
- TRUNCATE.

The journal also forbids order-write support, automatic stale takeover, and mainnet routing by CHECK constraint.

## Inspection

Run:

```bash
python -m tools.recover_bybit_demo_runtime_lease \
  --mode inspect \
  --output artifacts/bybit-demo-runtime-lease-recovery.json
```

Required environment variable:

```text
BYBIT_DEMO_DATABASE_DSN
```

Typical statuses:

- `NO_LEASE_PRESENT` — there is nothing to recover.
- `BLOCKED` — schema is not ready or the latest control event is not an explicit HALT.
- `RECOVERY_REQUIRED` — a lease exists and explicit HALT is present. The artifact contains the exact `lease_owner_sha256` fingerprint that must be supplied to recovery.

When an active checkpoint exists, inspection also exposes only `active_checkpoint_entry_order_link_id_sha256`, never the raw runtime owner token. This digest is derived from the durable v119 checkpoint identity and is later copied from the immutable v123 audit into the recovery receipt.

The inspection artifact does not expose the raw owner token, DSN, database identity, API credentials, position quantity, price, equity, or PnL.

## Recovery

First stop the previous service/container and retain external evidence that it is actually dead. Then ensure v121 has a fresh explicit operator HALT. Only then run:

```bash
python -m tools.recover_bybit_demo_runtime_lease \
  --mode recover \
  --expected-owner-sha256 '<fingerprint from inspect>' \
  --operator-id '<operator>' \
  --reason '<bounded recovery reason>' \
  --process-stop-evidence '<non-secret incident/deployment/service-manager reference>' \
  --confirmation RECOVER_BYBIT_DEMO_RUNTIME_LEASE \
  --output artifacts/bybit-demo-runtime-lease-recovery.json
```

A wrong confirmation or changed fingerprint is fail-closed and leaves the lease untouched.

A successful receipt carries `active_checkpoint_present` and `active_checkpoint_entry_order_link_id_sha256`. The digest is the exact value persisted in the append-only v123 recovery audit at the moment of recovery. If no checkpoint existed, the boolean is false and the digest is null.

If the first caller loses the response after a committed recovery, retrying with the same old owner fingerprint returns `ALREADY_RECOVERED` from the immutable v123 journal instead of attempting another delete. The replay rehydrates the same checkpoint-presence flag and checkpoint identity digest from that existing audit record; it does not recompute them from current runtime state.

## GitHub Actions

Manual workflow:

```text
bybit-demo-runtime-lease-recovery
```

The operational job runs only on the protected self-hosted `bybit-demo` environment. It needs only `BYBIT_DEMO_DATABASE_DSN`; no Bybit API key or mainnet credential is provided.

`inspect` is read-only. `recover` requires the exact fingerprint and confirmation inputs. There is no schedule and no automatic condition watch.

## After successful recovery

Do not immediately ARM a new entry.

1. Confirm the v121 decision is still HALTED.
2. Re-run connected Demo read-only preflight.
3. If an active checkpoint exists, require a matching real Demo position/execution and resume the persistent supervisor.
4. If there is no checkpoint/position, remain HALTED until a separate deliberate new-entry workflow performs fresh connected preflight, short-lived ARM, fresh operator approval, and immutable pre-submit authorization.

## Relation to operational release proof

Lease recovery and release-drill proof are intentionally different contracts. Recovery may legitimately free an orphaned lease with no active checkpoint. The higher-level `BYBIT_DEMO_OPERATIONAL_RELEASE_EVIDENCE_V1` gate is stricter when claiming `RECOVERY_DRILL_PROVEN`: it requires the recovery receipt to contain an active checkpoint digest equal to `SHA256(entry_order_link_id)` from the exact operator-approved entry artifact in the same evidence chain. This prevents a valid recovery of another orphaned runtime from being reused as proof for the approved trade under review.

## What this does not prove

A green code/PG qualification proves transaction ordering, exact identity matching, audit immutability and regression behavior. It does not prove that a production process is dead and must never be used as a substitute for external process-stop verification.

Mainnet remains read-only and is not part of this workflow.
