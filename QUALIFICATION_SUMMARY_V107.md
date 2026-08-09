# Qualification summary — Schema 107 production rollout actuator

## Qualified source boundary

Schema 107 is qualified as a source-level, single-attempt Kubernetes replica rollout actuator stacked on Schema 106. It does not authorize a production mutation by itself.

## Final qualification

- focused Schema 107 suite: **139 passed**;
- complete stacked suite: **718 passed**;
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
- migration column-contract tests: **PASS**;
- migration apply and idempotent re-apply against PostgreSQL 16: **PASS**;
- stress: **1,000 commands / 8 workers / 0 failures**;
- durable-style replay ledger size: **1,000**;
- unique command digests: **1,000 / 1,000**.

The permanent GitHub workflow executes the complete stacked repository and starts an isolated PostgreSQL service. It applies the canonical migration twice with `ON_ERROR_STOP=1`, then queries `information_schema` to verify the execution/fence column contract used by the repository.

## Verified safety invariants

- exactly two independent RELEASE and RISK approval attestations are required;
- approvers, approval keys, approval IDs and approval nonces must be distinct;
- the controller signs the complete command and the executor signs the receipt;
- the intent binds the signed Schema 106 rollout-action digest, qualification evidence and qualification state;
- command validity and all signatures are rechecked immediately before the durable mutation marker;
- replay guard, execution row and outbox event are created in one PostgreSQL transaction;
- fencing tokens are durable, monotonic per deployment UID and recorded atomically with the mutation marker;
- the persisted execution schema contains one non-null `deployment_uid` column matching repository writes and fence indexes;
- every migration table is statically checked for duplicate top-level column declarations;
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
- production PostgreSQL credentials, role grants and transaction-isolation configuration;
- workload identity and secret/key distribution;
- Kubernetes audit sink and external observability pipeline;
- real production rollout or rollback;
- production mutation authorization;
- external order routing;
- live trading.
