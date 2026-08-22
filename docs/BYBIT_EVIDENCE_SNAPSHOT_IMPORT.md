# Bybit evidence snapshot import

The full-history research pipeline and the 10-minute live opportunity loop intentionally run on
different cadences.

After a qualified `tools.research_bybit_dynamic_top10` run, import its evidence matrix once into the
append-only v111 PostgreSQL registry:

```bash
export BYBIT_OPPORTUNITY_DATABASE_DSN='postgresql://...'
python -m tools.store_bybit_strategy_evidence \
  --input artifacts/bybit_dynamic_top10.json \
  --migrate-postgres
```

The command accepts the full research artifact and uses its timezone-aware `observed_at`. It can also
accept a direct `BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX` payload, but then `--observed-at` is required.

The canonical SHA-256 of the evidence report becomes `evidence_snapshot_id`. Re-importing identical
content is idempotent; content that differs receives a different immutable evidence id.

The 10-minute `tools.refresh_bybit_live_evidence_registry` command then consumes the most recent
stored evidence snapshot. It does **not** rerun the full historical analysis every cycle.

This import path contains no Bybit credentials and no order methods. Database checks retain:

```text
strategy_promotion_allowed=false
demo_activation_allowed=false
live_activation_allowed=false
bybit_live_order_routing_allowed=false
```
