# ASTRA 7.31.0 — Schema 101 external sandbox qualification

Schema 101 adds a fail-closed qualification boundary for an external paper broker sandbox. It does **not** enable live trading.

## Control flow

```text
sealed plan
  -> read-only account and open-order probe
  -> authenticated paper stream evidence
  -> signed, scoped, one-time operator approval
  -> single-attempt submit / replace / cancel
  -> read-only resolution of ambiguous mutations
  -> bounded cleanup proof
  -> verified or blocked / recovering / quarantined
```

Safety controls include generation fencing, HMAC approval sealing, replay protection, sticky kill switch, allowlisted symbols, quantity/notional caps, hash-chain/fsync evidence and fail-closed recovery.

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python -m tools.architecture_audit_v101 .
python -m tools.static_audit_v101 .
python -m tools.stress_v101 --iterations 1000 --workers 8
```

External probes require paper credentials in environment variables and the optional market-data dependency. Paper mutations remain disabled until a separately signed approval is accepted by the qualification service.

```text
external_order_routing_allowed = false
live_trading_allowed = false
```
