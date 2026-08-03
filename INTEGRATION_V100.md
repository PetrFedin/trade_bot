# Schema 100 integration

1. Apply `migrations/v100/001_alpaca_paper_sandbox.sql` using a migration role.
2. Store Alpaca paper credentials in the process secret store and expose only the two `ASTRA_ALPACA_PAPER_*` environment variables to the adapter process.
3. Run `astra-platform-v100 credentials-status`; confirm that only a fingerprint is printed.
4. Start the authenticated paper trade-update stream and retain qualification evidence.
5. Keep `paper_order_writes_enabled=false` until an operator-approved sandbox test window exists.
6. Use Schema 99 read-only recovery for ambiguous submit, replace or cancel outcomes.

No secret value may be written to PostgreSQL, logs, release artifacts or qualification reports.
