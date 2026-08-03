# Integration — Schema 101

1. Apply `migrations/v101/001_external_sandbox_qualification.sql` using the migration role.
2. Store `ASTRA_ALPACA_PAPER_KEY_ID`, `ASTRA_ALPACA_PAPER_SECRET_KEY` and `ASTRA_SANDBOX_APPROVAL_HMAC_KEY` outside source control.
3. Run the read-only external account/stream probe with writes disabled.
4. Produce a sealed plan and a separately sealed operator approval.
5. Enable paper writes only for the bounded qualification process.
6. Run the qualification and verify the journal and kill-switch status.
7. Treat `BLOCKED`, `RECOVERING` or `QUARANTINED` as a hard stop.
8. Do not infer live readiness from a successful paper qualification.
