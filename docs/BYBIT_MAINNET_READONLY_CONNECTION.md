# Bybit mainnet read-only account connection

## Scope

This boundary connects ASTRA to a real Bybit account for account visibility and reconciliation only.
It does **not** promote the trading runtime to real-money order routing.

The canonical trading product remains pinned to Bybit Demo Trading:

- `ASTRA_ENV=demo`
- `BYBIT_REST_URL=https://api-demo.bybit.com`
- `TRADING_WRITES_ENABLED` controls demo mutations only
- `MAINNET_ENABLED=false`
- `live_mainnet_order_routing_allowed=false`

Mainnet observability is implemented separately in:

- `app/execution/bybit_mainnet_readonly.py`
- `app/runtime/bybit_mainnet_readonly_probe.py`

## Bybit API key requirements

Create a dedicated API key for the real account. Do not reuse the Demo Trading key.

Required controls:

1. The key must be **Read-Only** in Bybit. ASTRA calls `GET /v5/user/query-api` and refuses the key when Bybit reports `readOnly != 1`.
2. Bind the key to the production server's fixed egress IP address or addresses. The production probe refuses an empty Bybit `ips` list.
3. Store the key and secret only in the deployment secret manager or protected environment. Never commit them to Git.
4. Use the dedicated variables `BYBIT_MAINNET_READONLY_API_KEY` and `BYBIT_MAINNET_READONLY_API_SECRET`. The demo order-writing runtime does not consume these names.
5. Do not grant a read/write key merely because the desired permission categories are narrow. The hard safety condition is Bybit's `readOnly=1` result.

Official Bybit references:

- API key information: https://bybit-exchange.github.io/docs/v5/user/apikey-info
- V5 authentication: https://bybit-exchange.github.io/docs/v5/guide
- Wallet balance: https://bybit-exchange.github.io/docs/v5/account/wallet-balance
- Position info: https://bybit-exchange.github.io/docs/v5/position

## Network boundary

The real-account transport is pinned to `api.bybit.com` and implements `GET` only.
There is no `POST` method on the transport and no order placement, cancellation, leverage, margin,
transfer, withdrawal, or trading-stop method on the client.

The client also uses an explicit read-path allowlist. At present it permits only account/broker-truth
reads needed for visibility and reconciliation:

- `/v5/user/query-api`
- `/v5/account/wallet-balance`
- `/v5/account/info`
- `/v5/account/fee-rate`
- `/v5/account/transaction-log`
- `/v5/position/list`
- `/v5/position/closed-pnl`
- `/v5/execution/list`
- `/v5/order/realtime`
- `/v5/order/history`

A path such as `/v5/order/create` is rejected locally before transport invocation.

## Configuration

Configure protected deployment secrets:

```text
BYBIT_MAINNET_READONLY_API_KEY=<real account read-only key>
BYBIT_MAINNET_READONLY_API_SECRET=<real account read-only secret>
```

Keep the existing product variables unchanged:

```text
ASTRA_ENV=demo
BYBIT_REST_URL=https://api-demo.bybit.com
MAINNET_ENABLED=false
```

## Connection probe

Run the isolated probe from the deployment environment:

```text
python -m app.runtime.bybit_mainnet_readonly_probe
```

The probe sequence is deliberately ordered:

1. authenticate against `GET /v5/user/query-api`;
2. prove `readOnly=1`;
3. prove at least one server IP binding is present;
4. read account mode;
5. read Unified wallet/equity state;
6. read open USDT linear positions;
7. emit a sanitized JSON snapshot.

The output contains an SHA-256 fingerprint of the API key for operational identity checks, but it
never returns or logs the raw API key or API secret.

## What this unlocks safely

With a real read-only account connected, ASTRA can build production-grade monitoring around actual
wallet equity, available balance, open positions, execution history, closed PnL, fees, funding and
cash reconciliation without creating a route to submit a real order.

A future real-money execution adapter, if ever approved, must be a separate audited component and
must not be enabled by changing `MAINNET_ENABLED`, swapping a URL, or reusing this read-only client.
