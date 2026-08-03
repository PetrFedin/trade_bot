# Operator runbook — Schema 101

## Before qualification

- Confirm the endpoint is `paper-api.alpaca.markets` and the stream is the paper stream.
- Confirm the account ID, currency and status against the signed plan.
- Confirm the open-order baseline is empty.
- Confirm the selected symbol, quantity and maximum notional are intentionally small.
- Confirm paper writes are disabled until the signed approval is ready.

## Approval

- A second operator prepares an approval with a unique nonce and short expiry.
- Seal it with `astra-platform-v101 seal-approval`.
- Never paste the HMAC key or broker credentials into logs, tickets or chat.

## During qualification

- Do not repeat a timed-out mutation manually.
- Let the service perform a read-only lookup by `client_order_id`.
- Any fill, unresolved open order or identity mismatch requires a hard stop.

## After qualification

- Verify the append-only journal.
- Confirm the order is absent from open orders and terminal or absent by lookup.
- Confirm the kill switch is not engaged.
- Preserve evidence but do not store credentials.
