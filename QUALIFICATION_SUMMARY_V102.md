# Schema 102 qualification summary

## Deterministic local qualification

- focused Schema 102 tests: **53 passed**;
- branch-aware runtime coverage: **85%**;
- full available Schema 101–102 regression: **112 passed**;
- architecture audit: **PASS**;
- static security audit: **PASS**;
- stress: **1,000 campaigns / 8 workers / 0 failures**.

The test suite covers lease expiry and fencing, due-window enforcement, missed slots, total and consecutive failure budgets, residual-exposure blocking, upstream quarantine, restart replay, evidence retention, artifact and manifest tamper detection, stale/future evidence, terminal-state immutability and concurrent lease acquisition.

## External qualification boundary

Local deterministic campaigns are not evidence of a real multi-day external broker soak. The following remain unverified:

```text
external_30_day_soak_verified = false
external_alpaca_credentials_configured = false
external_order_routing_allowed = false
live_trading_allowed = false
```
