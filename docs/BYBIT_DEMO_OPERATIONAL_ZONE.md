# Bybit Demo protected operational zone

The Bybit Demo path is moving from repository-qualified code to an operable product. The operational security boundary is a **single protected fixed-egress execution zone**, not a collection of unrelated GitHub-hosted jobs.

## Zone identity

Operational workflows that touch the Demo database or authenticate to Bybit run with:

```text
runs-on: [self-hosted, bybit-demo]
environment: bybit-demo
```

The `bybit-demo` runner label is a deployment requirement, not a claim that a runner already exists. The runner must be provisioned before any manual operational workflow can execute.

The GitHub `bybit-demo` environment should own Demo secrets and require appropriate reviewer approval. Pull-request qualification remains on GitHub-hosted runners with fake/local dependencies and receives no operational secrets.

## Network contract

The self-hosted runner or deployed supervisor service must have a stable outbound public IP. Both Demo API keys used by the product must be bound to concrete allowed IP addresses that include the operational egress:

```text
BYBIT_DEMO_READONLY_API_KEY
BYBIT_DEMO_TRADING_API_KEY
```

Wildcard/unbound access is not accepted by production-readiness gates. The read-only connected-preflight client validates returned bindings; the separate trading-credential preflight independently requires concrete IP binding and exact least privilege.

Moving execution to an unrelated egress invalidates the credential/readiness evidence.

## Database contract

`BYBIT_DEMO_DATABASE_DSN` belongs to the protected Demo environment. Operational PostgreSQL bootstrap/verify, connected state reconciliation, v121 control, v122 session risk, persistent management, and v123 lease recovery use the same authoritative database.

The durable schema contract is:

```text
v119 runtime lease + active excursion checkpoint
v120 immutable approval/provenance/terminal evidence
v121 append-only HALT/ARM control plane
v122 restart-safe session-risk checkpoint + append-only terminal outcome journal
v123 append-only controlled orphan-lease recovery audit
```

`VERIFIED_READY` now requires all five layers. v122 prevents process restart from silently resetting opening equity, high-water, terminal all-in PnL, execution cost or consecutive-loss history. v123 prevents an orphan lease from becoming an operational dead end without introducing TTL or automatic takeover.

The intended network posture is private/restricted database access from the Demo execution zone. Sanitized artifacts do not expose database identity.

## Operational components

The protected zone now has these bounded gates/services:

```text
bybit-demo-postgres-bootstrap
bybit-demo-connected-preflight
bybit-demo-control-plane
bybit-demo-trading-credential-preflight
bybit-demo-activation-readiness
bybit-demo-session-start
bybit-demo-persistent-supervisor
bybit-demo-runtime-lease-recovery
```

They have deliberately different authority:

- PostgreSQL bootstrap may mutate only explicit v119-v123 DDL after the exact confirmation phrase.
- Connected preflight performs authenticated Bybit GETs and PostgreSQL reads only.
- Control plane can append v121 ARM/HALT audit events but has no trading credential.
- Trading credential preflight authenticates the Demo trading key through GET-only API-key inspection and has no order mutation method.
- Activation readiness combines sanitized evidence while the control plane is HALTED; it does not ARM or trade.
- Session-start exposes only `status` and one-time `initialize`; it creates the v122 singleton while flat/HALTED and has no reset/order-write surface.
- Persistent supervisor manages only an already-open canonical Demo trade. It is IDLE without a checkpoint and has no selector/approval/new-entry path.
- Runtime-lease recovery can delete only an exact orphaned v119 lease after explicit HALT, exact owner fingerprint, external process-stop evidence, operator identity and immutable v123 audit.

There is no scheduled autonomous entry job.

## New-entry authority

ARM is accepted operationally only when the same invocation obtains an exact clean fixed-egress connected preflight:

```text
READY_FOR_MANUAL_OPERATOR_APPROVAL
read_only_api_key_verified = true
api_key_ip_binding_present = true
no preflight reasons
no mainnet/order-write capability
```

The operational control plane rechecks this contract before the durable v121 ARM transaction. The operator-approved entry runtime then adds fresh approval plus immutable pre-submit authorization before the single allowed Demo entry mutation.

Persistent management is not permission to create new exposure. A missing checkpoint remains IDLE.

## Session-risk anti-reset and emergency reduction

v122 is not auto-created by worker startup. A missing session-risk ledger blocks operational startup.

The one-time session initializer requires:

```text
v119-v123 VERIFIED_READY
exchange flat
no pending orders
no runtime lease
no active excursion checkpoint
v121 HALTED
fresh fixed-egress read-only account evidence
explicit operator action
exact Git revision
```

Opening equity comes from authenticated Demo wallet observation and is not emitted exactly in sanitized artifacts.

After initialization, every persistent-management cycle re-observes real Demo wallet equity and persists any higher v122 high-water by CAS before managing the active trade. If durable session policy returns `flatten_required`, normal ratchet/max-hold management is pre-empted by an exact reduce-only close of the current residual position. Ambiguous close mutations are never blindly retried, and terminal evidence/accounting still must complete before checkpoint ACK.

## Controlled orphan-lease recovery

v119 still has no TTL and no automatic stale takeover.

A hard kill may leave a lease row. The only supported recovery is v123:

```text
1. Stop/prove dead the prior service or container externally.
2. Append explicit v121 HALT_NEW_ENTRIES.
3. Run lease-recovery inspect and record lease_owner_sha256.
4. Run recover with the exact fingerprint, operator id, reason,
   process-stop evidence reference, and RECOVER_BYBIT_DEMO_RUNTIME_LEASE.
5. PostgreSQL atomically inserts immutable v123 audit and deletes only that lease.
6. Active excursion checkpoint remains untouched.
7. Control plane remains HALTED.
8. Re-run connected preflight before resuming management or considering any later ARM.
```

No age threshold, PID timeout or heartbeat expiry can authorize recovery.

## Secret separation

The intended environment variables remain separated by function:

```text
BYBIT_DEMO_DATABASE_DSN
BYBIT_DEMO_READONLY_API_KEY
BYBIT_DEMO_READONLY_API_SECRET
BYBIT_DEMO_TRADING_API_KEY
BYBIT_DEMO_TRADING_API_SECRET
BYBIT_DEMO_READONLY_API_KEY_SHA256
BYBIT_MAINNET_READONLY_API_KEY_SHA256
```

The trading-credential workflow receives only the Demo trading key/secret plus read-only namespace fingerprints. It does not receive a raw mainnet key or secret. Connected-preflight/control/session-start initialization receive the Demo read-only credential and never receive the Demo trading credential. Lease recovery and PostgreSQL bootstrap receive only the DSN.

The persistent supervisor receives the Demo trading credential only for already-open Demo protection/reduce-only management, plus a separate read-only Demo credential for wallet/accounting reads. It never receives or constructs a mainnet order-routing client.

## Qualification versus operational evidence

A green pull request proves code behavior against isolated/fake dependencies. It does not prove a self-hosted runner, allow-listed egress, protected GitHub environment, configured secrets, production PostgreSQL readiness, real credential authentication, initialized v122 risk session, or a completed real Demo lifecycle.

Those facts require actual manual operational evidence.

## Required activation sequence

```text
1. Provision hardened self-hosted runner/service host in the bybit-demo zone.
2. Give it stable outbound IP and restricted access to Demo PostgreSQL.
3. Protect the GitHub environment and configure separated secrets/variables.
4. Bind Demo read-only and Demo trading keys to the zone egress IP.
5. Run PostgreSQL bootstrap verify/apply/verify -> v119-v123 VERIFIED_READY.
6. Run activation readiness while HALTED and require real connected evidence PASS.
7. Run bybit-demo-session-start status.
8. If NOT_INITIALIZED, explicitly initialize once while flat/HALTED from real wallet equity.
9. Re-run status and require INITIALIZED; every restart loads/resumes v122 only.
10. Deploy persistent supervisor in once-mode smoke, then service-manager loop mode.
11. Select/revalidate the exact candidate and create a fresh bounded approval.
12. ARM only for a short explicit operator window; ARM reruns fixed-egress connected preflight.
13. Execute one protected Demo entry with immutable authorization.
14. Supervisor proves fill/protection/risk/high-water/restart/terminal accounting lifecycle.
15. If a hard-killed runtime leaves a lease, use controlled v123 recovery; never manual DELETE.
16. Qualify real terminal close -> fees/funding/PnL -> v122 outcome persistence -> exact checkpoint ACK.
17. Mainnet stays read-only until an independent future governance decision; no Demo milestone enables mainnet writes.
```

This is the current path from demo/MVP infrastructure to an actual controlled product rather than a collection of passing tests.
