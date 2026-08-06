# Qualification summary — Schema 107 production rollout actuator

## Qualified source boundary

Schema 107 is qualified locally as a source-level, single-attempt Kubernetes replica rollout actuator stacked on Schema 106. It does not authorize a production mutation by itself.

## Final local qualification

- focused and available full local suite: **136 passed**;
- branch-aware runtime coverage: **93.900804%**;
- runtime statements measured: **1,184**;
- runtime branches measured: **308**;
- Kubernetes adapter coverage: **90.974729%**;
- PostgreSQL repository coverage: **88.546256%**;
- qualification bridge coverage: **92.452830%**;
- execution/signature/state-machine coverage: **98.105548%**;
- rollout service coverage: **88.775510%**;
- architecture audit: **PASS**;
- static security audit: **PASS**;
- compileall: **PASS**;
- canonical/package migration byte comparison: **PASS**;
- stress: **1,000 commands / 8 workers / 0 failures**;
- durable-style replay ledger size: **1,000**;
- unique command digests: **1,000 / 1,000**.

The GitHub workflow also runs `pytest -q` against the complete stacked repository after publication. The inherited Schema 99–106 suite is not present in this local overlay and must be confirmed by that server-side run.

## Verified safety invariants

- exactly two independent RELEASE and RISK approval attestations are required;
- approvers, approval keys, approval IDs and approval nonces must be distinct;
- the controller signs the complete command and the executor signs the receipt;
- the intent binds the signed Schema 106 rollout-action digest, qualification evidence and qualification state;
- command validity and all signatures are rechecked immediately before the durable mutation marker;
- replay guard, execution row and outbox event are created in one PostgreSQL transaction;
- fencing tokens are durable, monotonic per deployment UID and recorded atomically with the mutation marker;
- the Kubernetes JSON Patch tests deployment UID, resourceVersion, generation and current replicas;
- a command can attempt PATCH at most once in process and at most once in durable state;
- HTTP 408, 5xx, transport failures and malformed successful responses are ambiguous, never retried;
- recovery performs GET-only reconciliation and restores the durable mutation-attempt count;
- recovery takeover requires an explicitly UNCERTAIN command or a stale active execution;
- rollout success requires desired, ready and available replicas all equal the signed target;
- partial or conflicting execution annotations fail closed;
- external order routing and live trading remain disabled.

## Not externally verified

- production Kubernetes API and admission chain;
- production PostgreSQL repository and transaction isolation;
- workload identity and secret/key distribution;
- Kubernetes audit sink and external observability pipeline;
- real production rollout or rollback;
- production mutation authorization;
- external order routing;
- live trading.
