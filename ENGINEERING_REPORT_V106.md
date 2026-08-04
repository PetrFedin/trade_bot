# Engineering Report V106

Schema 106 introduces a deployment-qualification boundary rather than a deployment actuator. It proves that a signed release can be observed, evaluated, promoted or rolled back through explicit evidence and dual-control decisions without giving the qualification process permission to mutate Kubernetes or route broker orders.

Core controls:

1. HMAC-signed deployment manifest with replay protection and release identity pinning.
2. Read-only Kubernetes adapter with strict origin allowlist, TLS verification, redirect blocking and response limits.
3. Preflight gates for image/config identity, readiness, heartbeats, certificates, network policy, PDB, topology and dependencies.
4. Canary observation state machine with monotonic samples, failure budgets and immediate quarantine on broker-mutation or live-routing evidence.
5. Signed rollout outbox actions with two approvers, generation/fencing fields, idempotency key and one mutation attempt maximum.
6. Certificate-renewal and disaster-recovery drills with explicit state transitions and hash-chain evidence.
7. PostgreSQL transaction boundary and append-only migration structures.
