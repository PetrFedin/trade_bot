# Operator runbook — Schema 107

## Before enabling a worker

- verify the release identity and predecessor identity;
- apply the V107 PostgreSQL migration under a dedicated owner;
- grant the executor only GET and PATCH on the exact Deployment;
- ensure PATCH admission policies reject image, environment, command or other spec mutations;
- confirm external order routing and live trading annotations are `false`;
- load separate RELEASE, RISK, controller and executor keys from the approved secret backend;
- keep production mutation authorization disabled until an external qualification window is approved.

## Normal execution

1. Inspect the signed command and its expiration.
2. Confirm the durable replay/outbox transaction succeeded.
3. Confirm preflight evidence matches the intended deployment UID and release digests.
4. Observe the durable fence and mutation marker transaction.
5. Expect no more than one PATCH request.
6. Require a signed `APPLIED` or `ALREADY_APPLIED` receipt only after exact full readiness.

## UNCERTAIN state

- do not replay or reconstruct the PATCH;
- claim recovery through PostgreSQL;
- GET the Deployment;
- reconcile exact markers, target replicas, readiness and release identity;
- emit `RECONCILED` only when every gate passes;
- otherwise retain `UNCERTAIN` and escalate.

## Emergency stop

Disable the executor service account or remove PATCH permission. Do not delete execution rows or replay reservations. Preserve PostgreSQL events, Kubernetes audit logs and signed receipts.
