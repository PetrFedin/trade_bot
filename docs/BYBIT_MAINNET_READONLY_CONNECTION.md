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
5. Select the Bybit site using `BYBIT_MAINNET_READONLY_SITE`; do not supply a URL or hostname directly.
6. Do not grant a read/write key merely because the desired permission categories are narrow. The hard safety condition is Bybit's `readOnly=1` result.

Official Bybit references:

- Integration/authentication and regional API domains: https://bybit-exchange.github.io/docs/v5/guide
- API key information: https://bybit-exchange.github.io/docs/v5/user/apikey-info
- Wallet balance: https://bybit-exchange.github.io/docs/v5/account/wallet-balance
- Position info: https://bybit-exchange.github.io/docs/v5/position

## Network boundary

The real-account transport implements `GET` only. There is no `POST` method on the transport and no
order placement, cancellation, leverage, margin, transfer, withdrawal, or trading-stop method on the
client.

Bybit currently documents regional mainnet API domains. ASTRA does not accept an arbitrary endpoint;
it maps a small site profile to an audited hostname allowlist. Current profiles are:

| `BYBIT_MAINNET_READONLY_SITE` | Audited hostname |
| --- | --- |
| `global` | `api.bybit.com` |
| `global-alt` | `api.bytick.com` |
| `nl` | `api.bybit.nl` |
| `tr` | `api.bybit.tr` |
| `kz` | `api.bybit.kz` |
| `georgia` | `api.bybitgeorgia.ge` |
| `ae` | `api.bybit.ae` |
| `eu` | `api.bybit.eu` |
| `id` | `api.bybit.id` |
| `jp` | `api.manepa.jp` |
| `hk` | `api-spark-fintech.com` |

Use the site/account profile on which the API key was created. Availability and product scope can
differ by jurisdiction; the allowlist is a transport safety boundary, not a statement that every
Bybit product is available in every region.

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

A path such as `/v5/order/create` is rejected locally before transport invocation. A value such as
`BYBIT_MAINNET_READONLY_SITE=https://api.bybit.com` is also rejected because site selection is an
enum, not a configurable URL.

## Configuration

Configure protected deployment secrets and the regional site profile:

```text
BYBIT_MAINNET_READONLY_API_KEY=<real account read-only key>
BYBIT_MAINNET_READONLY_API_SECRET=<real account read-only secret>
BYBIT_MAINNET_READONLY_SITE=global
```

For a Bybit Netherlands account, for example, the profile is `nl`; for the documented EEA endpoint it
is `eu`. Use the profile corresponding to the account/API key actually issued by Bybit.

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

1. resolve the configured site only through the audited regional host allowlist;
2. authenticate against `GET /v5/user/query-api`;
3. prove `readOnly=1`;
4. prove at least one server IP binding is present;
5. read account mode;
6. read Unified wallet/equity state;
7. read open USDT linear positions;
8. emit a sanitized JSON snapshot.

The output contains the selected audited API hostname and an SHA-256 fingerprint of the API key for
operational identity checks, but it never returns or logs the raw API key or API secret.

## What this unlocks safely

With a real read-only account connected, ASTRA can build production-grade monitoring around actual
wallet equity, available balance, open positions, execution history, closed PnL, fees, funding and
cash reconciliation without creating a route to submit a real order.

A future real-money execution adapter, if ever approved, must be a separate audited component and
must not be enabled by changing `MAINNET_ENABLED`, swapping a URL, selecting a regional site profile,
or reusing this read-only client.
