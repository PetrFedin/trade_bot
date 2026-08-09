# Schema 104 qualification summary

## Deterministic local qualification

- focused Schema 104 tests: **73 passed**;
- branch-aware runtime coverage: **97%**;
- worker execution plane coverage: **97%**;
- PostgreSQL worker repository coverage: **100%**;
- architecture audit: **PASS**;
- static security audit: **PASS**;
- compileall: **PASS**;
- stress: **1,000 claims / 8 workers / 0 failures**;
- unique event-tail digests: **1,000 / 1,000**.

The tests cover policy validation, HMAC claim and deployment attestation, replay prevention, generation/fencing checks, heartbeat freshness and regression, exact paper endpoint allowlisting, TLS/redirect transport flags, response-size limits, secret redaction, hash-chain journals, bounded evidence spool, HMAC upload checkpoints, multipart resume, part and total digest verification, DLQ deduplication/release, crash recovery, transaction rollback, and PostgreSQL `FOR UPDATE SKIP LOCKED` claiming.

## External qualification boundary

Local protocol fakes are not evidence of external infrastructure. The following remain unverified:

```text
external_object_storage_verified = false
external_alpaca_read_only_worker_verified = false
production_worker_deployment_verified = false
external_order_routing_allowed = false
live_trading_allowed = false
```
