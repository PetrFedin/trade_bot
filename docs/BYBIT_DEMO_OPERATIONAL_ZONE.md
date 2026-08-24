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

Wildcard/unbound access is not accepted by the production-readiness gates.

The read-only connected-preflight client validates every returned binding as a real IPv4/IPv6 address, rejects malformed and duplicate bindings, and converts an absent/wildcard-only binding into a blocking preflight result.

The separate trading-credential preflight independently requires a concrete IP binding and exact least privilege.

The future Demo worker must run in this same zone or in an equivalently controlled zone with an IP already present in the key bindings. Moving execution to an unrelated egress invalidates the credential/readiness evidence.

## Database contract

`BYBIT_DEMO_DATABASE_DSN` belongs to the protected Demo environment. Operational PostgreSQL bootstrap/verify, connected state reconciliation and v121 control operations run from the same zone.

The intended network posture is private/restricted database access from the Demo execution zone. The repository does not encode a public database endpoint and sanitized artifacts do not expose database identity.

## Workflows inside the zone

The protected zone hosts these manual operational gates:

```text
bybit-demo-postgres-bootstrap
bybit-demo-connected-preflight
bybit-demo-control-plane
bybit-demo-trading-credential-preflight
```

They have different authority:

- PostgreSQL bootstrap may mutate only the explicit v119-v121 schema after its exact confirmation phrase.
- Connected preflight performs authenticated Bybit GETs and PostgreSQL reads only.
- Control plane can append v121 ARM/HALT audit events but has no trading credential.
- Trading credential preflight authenticates the future trading credential through GET `/v5/user/query-api` only and has no order mutation method.

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

The operational `FixedEgressPostgresBybitDemoControlPlane` rechecks this contract before delegating to the durable v121 ARM transaction. This is defense in depth on top of the connected-preflight wrapper.

HALT remains available without a Bybit trading credential. Existing-trade protection/reduce-only recovery remains separate from permission to create new exposure.

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

The trading-credential workflow receives only the Demo trading key/secret plus fingerprints of the read-only namespaces. It does not receive a raw mainnet key or secret.

The connected-preflight/control workflows receive the Demo read-only credential and never receive the Demo trading credential.

The PostgreSQL bootstrap workflow receives the database DSN and no Bybit credential.

The future write-enabled Demo worker must receive the Demo trading credential only at the final protected execution boundary. It must not receive or construct a mainnet order-routing client.

## Qualification versus operational evidence

A green pull request proves code behavior against isolated/fake dependencies. It does not prove:

- a self-hosted `bybit-demo` runner exists;
- its egress IP is stable or allow-listed;
- the protected GitHub environment exists;
- secrets/variables are configured;
- the production PostgreSQL schema is ready;
- either Bybit Demo credential authenticates from that zone;
- a real Demo order lifecycle has completed.

Those facts require actual manual operational workflow evidence.

## Required activation sequence

```text
1. Provision hardened self-hosted runner labelled self-hosted + bybit-demo.
2. Give it stable outbound IP and restricted access to the Demo PostgreSQL service.
3. Create/protect GitHub environment bybit-demo and configure separated secrets/variables.
4. Bind the Demo read-only and Demo trading keys to the zone egress IP.
5. Run PostgreSQL bootstrap verify/apply/verify -> VERIFIED_READY.
6. Run connected fixed-egress read-only preflight -> READY_FOR_MANUAL_OPERATOR_APPROVAL.
7. Run GET-only trading credential preflight -> READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL.
8. Observe v121 status (default HALTED).
9. ARM only for a short explicit operator window; ARM reruns fixed-egress connected preflight.
10. Only after the preceding operational evidence exists may a separate operator-approved Demo execution worker be wired to the trading credential.
11. Qualify a real bounded Demo order lifecycle: submit -> fill reconciliation -> protection -> restart/recovery -> terminal close -> fees/funding/PnL evidence.
12. Mainnet stays read-only until an independent future governance decision; no Demo milestone implicitly enables mainnet writes.
```

This sequence is the current path from demo/MVP infrastructure to an actual controlled product rather than a collection of passing tests.
