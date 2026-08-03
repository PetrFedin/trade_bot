# ASTRA 7.30.0 — Schema 100 Alpaca paper sandbox

Schema 100 adds a concrete, self-contained Alpaca **paper-only** adapter boundary on top of the Schema 99 round-trip and deployment controls.

```text
secret-store credentials
  -> paper endpoint pinning
  -> independent read/write rate limits
  -> bounded retry for reads only
  -> single-attempt submit/replace/cancel
  -> authenticated trade_updates stream
  -> duplicate suppression and generation fencing
  -> regression quarantine and qualification evidence
```

The runnable repository slice includes the adapter, tests, PostgreSQL migration, operator CLI, static/architecture audits and GitHub Actions.

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python -m tools.architecture_audit_v100 .
python -m tools.static_audit_v100 .
python -m tools.stress_v100 --iterations 1000 --workers 8
```

Credentials are read only from `ASTRA_ALPACA_PAPER_KEY_ID` and `ASTRA_ALPACA_PAPER_SECRET_KEY`; logs and evidence contain only a short fingerprint.

```text
alpaca_paper_credentials_configured = false
external_order_routing_allowed = false
live_trading_allowed = false
```
