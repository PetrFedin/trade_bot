# ASTRA 7.32.0 — Schema 102 sandbox soak orchestrator

Schema 102 adds a durable scheduler for repeated Schema 101 paper-sandbox qualifications.

```text
sealed campaign plan
  -> fenced lease
  -> due run claim
  -> Schema 101 qualification
  -> sealed evidence archive
  -> verified / failed / blocked / quarantined
  -> next slot or completed paper-soak eligibility
```

Key controls:

- one active run per campaign;
- monotonic fencing tokens;
- total and consecutive failure budgets;
- strict evidence freshness and identity;
- immediate block for recovery-required or residual exposure;
- append-only fsync/hash-chain journal;
- chained evidence manifest and retention deadline;
- restart replay without automatic unblock.

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python -m tools.architecture_audit_v102 .
python -m tools.static_audit_v102 .
python -m tools.stress_v102 --iterations 1000 --workers 8
```

```text
external_order_routing_allowed = false
live_trading_allowed = false
```
