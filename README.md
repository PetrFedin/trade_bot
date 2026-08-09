# ASTRA 7.29.0 — Schema 99 external paper round trip

Schema 99 adds a bounded paper-only broker verification loop and a generation-fenced deployment supervisor.

```text
admission evidence
  -> broker/account preflight
  -> submit one allowlisted paper limit order
  -> optional replace
  -> cancel
  -> reconcile absence from open orders
  -> append-only fsync/hash-chain evidence
```

Ambiguous mutations are never blindly repeated. A read-only lookup is used and unresolved outcomes enter `RECOVERING`. Any fill creates a residual-exposure block. Live routing stays disabled.

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python -m tools.platform_v99 verify-journal ./roundtrip.jsonl
```

```text
external_order_routing_allowed = false
live_trading_allowed = false
```
