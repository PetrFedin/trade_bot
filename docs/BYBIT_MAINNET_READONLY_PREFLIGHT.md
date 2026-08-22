# Bybit mainnet read-only operational preflight

## Purpose

This layer sits on top of the qualified real-account read-only connection boundary. It determines
whether the deployment is healthy enough to perform authenticated **reads** against the real Bybit
account. It does not enable real-money order routing.

The sequence is deliberately fail-closed:

1. resolve only the audited Bybit regional host selected by `BYBIT_MAINNET_READONLY_SITE`;
2. call public `GET /v5/market/time` without API credentials;
3. measure local send/receive time, round-trip time, midpoint clock offset and network uncertainty;
4. refuse authenticated account reads when the clock preflight is unsafe;
5. only then run the qualified read-only API-key/account probe;
6. aggregate a sanitized operational health snapshot.

## Why clock preflight is mandatory

Bybit authenticated V5 requests use a millisecond timestamp and receive window. Bybit documents the
validity rule as:

```text
server_time - recv_window <= timestamp < server_time + 1000
```

Bybit also recommends keeping the local device clock NTP-synchronized and exposes
`GET /v5/market/time` for server-time comparison.

ASTRA intentionally uses a stricter internal readiness boundary than the exchange's full acceptance
window. For the current read-only mainnet preflight:

- worst-case absolute clock skew = `abs(midpoint_offset) + ceil(RTT / 2)`;
- maximum accepted worst-case skew = **500 ms**;
- maximum accepted request RTT = **1000 ms**;
- these thresholds are code constants and cannot be relaxed through deployment environment values.

The symmetric 500 ms bound is intentionally conservative. It leaves substantial margin relative to
Bybit's asymmetric authenticated-request window instead of trying to consume the full exchange
allowance.

## Operational command

After configuring the qualified read-only credentials and site profile, run:

```text
python -m app.runtime.bybit_mainnet_readonly_operational_probe
```

The command performs the public clock preflight first. If it is safe, it then verifies the read-only
API key and reads account, wallet and USDT linear position state.

The emitted JSON is sanitized and contains:

- selected audited Bybit API host;
- API-key SHA-256 fingerprint, never the raw key or secret;
- clock readiness, estimated offset, RTT and uncertainty;
- read-only and IP-binding verification state;
- total equity, wallet balance, available balance and margin balance;
- initial- and maintenance-margin ratios when the denominator is available;
- open-position count;
- gross broker position value when every open position has a broker value;
- aggregate open-position unrealised PnL when every open position has broker UPL.

Missing broker position economics remain `null`; ASTRA does not replace missing real-account fields
with invented zeroes.

## Environment separation

This preflight uses the qualified real-account read-only plane only:

```text
BYBIT_MAINNET_READONLY_API_KEY=...
BYBIT_MAINNET_READONLY_API_SECRET=...
BYBIT_MAINNET_READONLY_SITE=...
```

It does not alter the canonical trading runtime, which remains Bybit Demo Trading with
`MAINNET_ENABLED=false` and `live_mainnet_order_routing_allowed=false`.

Real-account health is also intentionally **not** reconciled against the demo runtime's expected
position. They are different environments; treating a real mainnet position as a demo exposure
mismatch would create a false operational signal.

## Failure semantics

The operational probe stops before authenticated account reads when any of the following is true:

- the selected Bybit host is outside the audited allowlist;
- server-time response is malformed or internally inconsistent;
- local wall clock moves backwards during the measurement;
- worst-case clock skew exceeds 500 ms;
- RTT exceeds 1000 ms;
- the subsequent qualified read-only credential/IP/account checks fail.

Transport failures use the existing bounded Bybit REST retry policy. The preflight itself has no
POST/order mutation surface and cannot grant live-mainnet order routing.
