# Bybit Demo protected operational zone

The Bybit Demo path is moving from repository-qualified code to an operable product. The operational security boundary is therefore a **single protected fixed-egress execution zone**, not a collection of unrelated GitHub-hosted jobs.

## Zone identity

Operational workflows that touch the Demo database or authenticate to Bybit run with:

```text
runs-on: [self-hosted, bybit-demo]
environment: bybit-demo
```

The `bybit-demo` runner label is a deployment requirement, not a claim that a runner already exists. The runner must be provisioned before any manual operational workflow can execute.

The GitHub `bybit-demo` environment should own Demo secrets and require appropriate reviewer approval. Pull-request qualification remains on GitHub-hosted runners with fake/local dependencies and receives no operational secrets.

## Network contract

The self-hosted runner must have a stable outbound public IP. Both Demo API keys used by the product must be bound to concrete allowed IP addresses that include the operational egress:

```text
BYBIT_DEMO_READONLY_API_KEY
BYBIT_DEMO_TRADING_API_KEY
```

Wildcard/unbound access is not accepted by the production-readiness gates. The read-only connected-preflight client validates returned bindings; the separate trading-credential preflight independently requires concrete IP binding and exact least privilege.

The future Demo worker must run in this same zone or in an equivalently controlled zone with an IP already present in the key bindings. Moving execution to an unrelated egress invalidates the credential/readiness evidence.

## Database contract

`BYBIT_DEMO_DATABASE_DSN` belongs to the protected Demo environment. Operational PostgreSQL bootstrap/verify, connected state reconciliation, control operations, risk persistence and session provenance run from the same zone.

The durable schema contract is now:

```text
v119 runtime lease + active excursion checkpoint
v120 immutable approval/provenance/terminal evidence
v121 append-only HALT/ARM control plane
v122 restart-safe session-risk checkpoint + append-only terminal outcome journal
v123 immutable session-start operator/Git/preflight provenance
```

`VERIFIED_READY` requires all five layers. v122 prevents process restart from silently resetting risk history. v123 prevents the origin of that risk session from disappearing when a GitHub artifact expires.

The bootstrap uses stable confirmation `APPLY_BYBIT_DEMO_DURABLE_SCHEMA` and advisory lock `119999`; future durable migrations extend the verified contract without changing the operator-facing confirmation string.

## Workflows inside the zone

The protected zone hosts these manual operational gates:

```text
bybit-demo-postgres-bootstrap
bybit-demo-connected-preflight
bybit-demo-control-plane
bybit-demo-trading-credential-preflight
bybit-demo-activation-readiness
bybit-demo-session-start
```

They have different authority:

- PostgreSQL bootstrap may mutate only the repository-defined durable schema after its exact confirmation phrase.
- Connected preflight performs authenticated Bybit GETs and PostgreSQL reads only.
- Control plane can append v121 ARM/HALT audit events but has no trading credential.
- Trading credential preflight authenticates the future trading credential through GET `/v5/user/query-api` only and has no order mutation method.
- Activation readiness combines sanitized evidence while the control plane is still HALTED; it does not ARM or trade.
- Session-start exposes only `status` and one-time `initialize`; it atomically creates v122 risk state + v123 provenance from authenticated Demo read-only evidence while flat/HALTED and has no reset or order-write surface.

No workflow above is scheduled. None autonomously creates a trade.

## ARM hardening

ARM is accepted operationally only when the same invocation obtains an exact clean fixed-egress connected preflight:

```text
READY_FOR_MANUAL_OPERATOR_APPROVAL
read_only_api_key_verified = true
api_key_ip_binding_present = true
no preflight reasons
no mainnet/order-write capability
```

The operational `FixedEgressPostgresBybitDemoControlPlane` rechecks this contract before delegating to the durable v121 ARM transaction. HALT remains available without a Bybit trading credential. Existing-trade protection/reduce-only recovery remains separate from permission to create new exposure.

## Session-risk and provenance boundary

v122 is intentionally not auto-created by worker startup. A missing session-risk ledger means **new exposure is blocked**. A v122 ledger without valid v123 provenance also means worker readiness is blocked.

The explicit `bybit-demo-session-start` initializer requires all of the following in one bounded invocation:

```text
v119-v123 VERIFIED_READY
exchange flat
no pending orders
no runtime lease
no active excursion checkpoint
v121 HALTED
fresh fixed-egress read-only account evidence
explicit operator identity + reason
exact Git revision
```

The initializer acquires a PostgreSQL transaction advisory lock plus table locks covering runtime lease, active checkpoint, v121 control, v122 singleton and v123 provenance. It performs a final direct positions/orders recheck immediately before atomic v122/v123 inserts.

Opening equity comes from the authenticated Demo wallet observation and is not emitted as an exact value in the sanitized artifact. v123 stores the operator/reason internally in immutable PostgreSQL audit but the sanitized artifact emits only hashes/booleans, not the reason text.

Once created, process/container/runner restarts must verify v123, load the existing v122 ledger and advance it only by CAS. There is no normal reset/clear/truncate/takeover path.

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

The trading-credential workflow receives only the Demo trading key/secret plus fingerprints of the read-only namespaces. It does not receive a raw mainnet key or secret. Connected-preflight/control/session-start initialization receive the Demo read-only credential and never receive the Demo trading credential. Session-start `status` and PostgreSQL bootstrap receive only the DSN.

The future write-enabled Demo worker must receive the Demo trading credential only at the final protected execution boundary. It must not receive or construct a mainnet order-routing client.

## Qualification versus operational evidence

A green pull request proves code behavior against isolated/fake dependencies. It does not prove a self-hosted runner, stable/allow-listed egress, protected GitHub environment, configured secrets, production PostgreSQL readiness, real credential authentication, initialized v122/v123 session, or a completed real Demo order lifecycle.

Those facts require actual manual operational workflow evidence.

## Required activation sequence

```text
1. Provision hardened self-hosted runner labelled self-hosted + bybit-demo.
2. Give it stable outbound IP and restricted access to Demo PostgreSQL.
3. Create/protect GitHub environment bybit-demo and configure separated secrets/variables.
4. Bind Demo read-only and Demo trading keys to the zone egress IP.
5. Run PostgreSQL bootstrap verify/apply/verify -> v119-v123 VERIFIED_READY.
6. Run activation readiness while HALTED and require real connected evidence PASS.
7. Run bybit-demo-session-start status.
8. If NOT_INITIALIZED, explicitly initialize v122 + v123 once while flat/HALTED.
9. Re-run status and require INITIALIZED with verified immutable provenance.
10. On every later restart verify v123 and load/resume v122 only.
11. Select/revalidate the exact candidate and create the exact short-lived approval.
12. ARM only for a short explicit operator window; ARM reruns fixed-egress connected preflight.
13. Only after the preceding evidence exists may a protected Demo execution worker receive the trading credential.
14. Qualify a real bounded Demo lifecycle: submit -> fill reconciliation -> protection -> restart/recovery -> terminal close -> fees/funding/PnL -> v122 outcome persistence.
15. Mainnet stays read-only until an independent future governance decision; no Demo milestone implicitly enables mainnet writes.
```

This sequence is the current path from demo/MVP infrastructure to an actual controlled product rather than a collection of passing tests.
