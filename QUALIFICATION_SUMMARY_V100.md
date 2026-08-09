# ASTRA 7.30.0 / Schema 100 qualification summary

## Deterministic verification

- focused Schema 100 tests: **26 passed**;
- integrated ASTRA regression: **810 passed, 6 skipped**;
- Schema 100 self-contained paper runtime branch coverage: **98%**;
- architecture audit: **PASS**;
- static security audit: **PASS**;
- authenticated trade-update stress: **1,000 updates / 8 workers / 0 failures**.

## External evidence not present

The six skipped tests require external PostgreSQL DSN, TLS certificate evidence and HA/chaos infrastructure. No Alpaca credentials were available in the build environment, therefore account reads, authenticated external WebSocket traffic and submit/replace/cancel mutations were not claimed as verified.

```text
alpaca_paper_credentials_configured = false
alpaca_paper_account_read_verified = false
alpaca_trade_updates_authenticated_verified = false
alpaca_submit_replace_cancel_verified = false
external_order_routing_allowed = false
live_trading_allowed = false
```
