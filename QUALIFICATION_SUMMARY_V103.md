# Schema 103 qualification summary

## Deterministic local qualification

- focused Schema 103 tests: **87 passed**;
- branch-aware runtime coverage: **88%** across both Schema 103 runtime modules;
- `campaign_control_plane_v103.py`: **87%**;
- `postgres_control_plane_v103.py`: **98%**;
- architecture audit: **PASS**;
- static security audit: **PASS**;
- Python compileall: **PASS**;
- stress: **1,000 campaigns / 8 workers / 0 failures**;
- stress event-tail uniqueness: **1,000 / 1,000**.

The focused suite covers policy sealing, due activation, concurrent lease acquisition, generation/fencing rejection, heartbeat extension and regression, allowlisted read-only probes, mutation rejection, failed/error outcomes, resumable chunk upload, idempotent replay, conflicting replay quarantine, total SHA-256 verification, retention/legal hold, incident deduplication/escalation, operator cleanup confirmation, operator block release, DB transaction commit/rollback and `FOR UPDATE SKIP LOCKED` selection.

## Source identities

```text
campaign_control_plane_v103.py
635a08540c0bd9664b4faf92a273372e7e360a4598bc9ab7459f65742152d345

postgres_control_plane_v103.py
81a0b35746c2bf8ce97eb47d53b6767d7086ad3560417ca0b8517a2475e86bd3

001_production_campaign_control_plane.sql
872ec384784c1da91b768a2ecb77fd8e4cb17f5f536f0a3bd4eea774b1465171
```

## External qualification boundary

The deterministic implementation and DB-API adapter are not proof of a deployed external control plane. The following remain unverified:

```text
external_postgresql_cluster_verified = false
production_scheduler_deployed = false
production_distributed_lease_verified = false
external_alpaca_read_only_campaign_verified = false
external_order_routing_allowed = false
live_trading_allowed = false
```
