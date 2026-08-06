# Engineering report — Schema 107

## Decision

Implement the smallest production mutation surface that can safely consume Schema 106 qualification output: scaling one pre-bound Kubernetes Deployment. No broker mutation, image change, configuration change, arbitrary patch path or retry is permitted.

## Main controls

1. **Cryptographic command chain** — qualification evidence/state digests are bound into an execution intent; RELEASE and RISK independently attest that intent; a controller signs the complete command.
2. **Durable replay boundary** — command ID, nonce and idempotency key are reserved in PostgreSQL in the same transaction as command persistence and outbox creation.
3. **Two-layer fencing** — Kubernetes annotations reject stale visible markers; PostgreSQL atomically enforces a strictly increasing deployment fence before recording the single mutation attempt.
4. **Optimistic concurrency** — JSON Patch tests UID, resourceVersion, generation and current replicas before replacing replicas.
5. **Ambiguity discipline** — network failure, 5xx or malformed 2xx after the durable marker is `UNCERTAIN`; the executor never retries PATCH.
6. **Recovery discipline** — crashed `MUTATION_STARTED`, `VERIFYING` and `UNCERTAIN` commands are recovered with a TTL-bound claim and GET-only reconciliation.
7. **Exact completion** — success requires every signed target replica to be desired, ready and available, with exact command/fence markers and unchanged release identity.

## Defects found during implementation

- The initially planned unique fencing token did not prove monotonicity. A durable per-deployment fence register with conditional upsert was added.
- An already-applied command can require continued readiness observation even when the local row records zero PATCH attempts. Recovery eligibility was expanded to include `VERIFYING`/`UNCERTAIN` without falsifying the mutation count.
- A command could expire while the Kubernetes preflight GET was in flight. Full signature and validity verification is repeated immediately before the durable mutation marker.
- Unexpected client exceptions after the marker could escape without explicit classification. They now become `UNCERTAIN`; recovery remains GET-only.
- The existing Schema 107 branch was discovered to still reference the Schema 106 commit. This release is qualified against the actual remote ref before reporting publication.

## External boundary

Local tests and adapters do not prove a live production rollout. External Kubernetes, PostgreSQL permissions, workload identity, network policy, audit sink and operational approvals must be qualified separately.
