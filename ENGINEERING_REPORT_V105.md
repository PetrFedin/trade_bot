# Schema 105 engineering report

## Objective

Move from one hardened worker process to a controlled worker fleet without widening the trading boundary.

## Implemented controls

1. **Enrollment and identity lifecycle** — HMAC-signed, time-bounded enrollment; token and nonce replay guards; Kubernetes-style deployment attestation; certificate rotation under dual control; key retirement and revocation.
2. **Fleet state** — generation-fenced identities, monotonic heartbeat sequence, stale-worker rejection, explicit `ACTIVE`, `DRAINING`, `STOPPED`, `QUARANTINED`, and `REVOKED` states.
3. **Graceful shutdown** — new work is rejected after drain starts; completion requires no active claim and flushed evidence; deadline expiry quarantines the worker.
4. **Controlled autoscaling** — replica bounds, bounded steps, cooldowns, stabilization samples, dependency gates, incident budgets and containment gates.
5. **Fleet containment** — fleet, zone, deployment and worker scopes; monotonic epoch; idempotent activation; dual-control release with cleanup digest; no release while impacted work or recovery remains.
6. **Evidence storage** — HTTPS-only allowlist, TLS verification, redirect rejection, short-lived credential lease, per-part checksum, final metadata verification, and read-only recovery of ambiguous mutations.
7. **Durability** — PostgreSQL replay guards, worker generations, task fencing, append-only event/containment/evidence records and `SECURITY DEFINER` functions with fixed search path.

## Non-goals

Schema 105 does not enable order submission, order replacement, cancellation, live endpoints or production trading. It does not claim external Kubernetes, S3 or PostgreSQL qualification without credentials and deployed infrastructure.
