# Bybit forward liquidation evidence v116

## Purpose

Build the missing **forward-only liquidation context** for the continuously refreshed Bybit
Top-10 / extended ranked opportunity universe without pretending that liquidation history can be
reconstructed from ordinary candles, open interest or funding history.

The source is Bybit V5 public WebSocket topic `allLiquidation.{symbol}`. Bybit documents this topic
as the stream of all liquidations for the contract, pushed up to every 500 ms. The message contains
the event timestamp, symbol, liquidated position side, executed size and bankruptcy price.

Official references:

- https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation
- https://bybit-exchange.github.io/docs/v5/ws/connect

## Important semantics

For Bybit `allLiquidation`:

- raw `S=Buy` means a **LONG position was liquidated**;
- raw `S=Sell` means a **SHORT position was liquidated**;
- `v` is executed size;
- `p` is bankruptcy price.

v116 therefore stores both the raw side and the explicit interpreted side. It computes:

```text
estimated_notional_usdt = quantity_base × bankruptcy_price
```

This is deliberately called an **estimated liquidation notional at bankruptcy price**. It is not
claimed to be the liquidated trader's loss, the exchange insurance loss, an execution-price PnL or
an account-ledger amount.

## Why forward-only

The public topic is a real-time stream. v116 does not fabricate historical liquidation events from
price/OI/crowding/funding data and does not label absence of stored events as zero liquidation.

Every subscription is linked to the exact immutable v110 opportunity snapshot that selected its
ranked symbols. The collector refuses a stale v110 source snapshot. A typical production flow is:

```text
10-minute v110 ranked market snapshot
-> exact Top-10 + optional ranks 11..50
-> public allLiquidation subscriptions
-> immutable raw liquidation events
-> append-only stream health observations
-> 5-minute liquidation aggregates
-> later point-in-time join to prospective opportunity evidence
```

## Event identity and deduplication boundary

The documented public liquidation payload does not provide a unique exchange liquidation-event ID.
v116 therefore **does not claim one exists**.

The deterministic event ID is SHA-256 over the exact received message occurrence:

```text
system message timestamp
+ event timestamp
+ symbol
+ raw position side
+ quantity
+ bankruptcy price
+ ordinal within the message
```

This makes exact replay of the same received message occurrence idempotent and lets parallel writes
converge on the same stored record. It cannot prove that two economically identical liquidation
records emitted as different message occurrences represent the same underlying exchange event.
That uncertainty must remain visible in downstream research.

## PostgreSQL authority

Migration `migrations/v116/001_bybit_forward_liquidation_evidence.sql` adds:

- `astra_bybit_liquidation_subscription_v116` — immutable capture plan pinned to v110;
- `astra_bybit_liquidation_event_v116` — immutable forward liquidation events;
- `astra_bybit_liquidation_stream_status_v116` — append-only connection/heartbeat evidence;
- `astra_bybit_liquidation_5m_v116` — 5-minute side/count/notional aggregate view;
- `astra_bybit_liquidation_subscription_health_v116` — latest stream-health read model.

UPDATE and DELETE are rejected by database triggers. PUBLIC privileges are revoked.

## 5-minute context

For every symbol and event-time 5-minute bucket the view exposes:

- event count;
- LONG-liquidation count;
- SHORT-liquidation count;
- LONG estimated liquidation notional;
- SHORT estimated liquidation notional;
- total estimated liquidation notional;
- LONG minus SHORT estimated notional;
- normalized signed imbalance in `[-1, +1]`;
- largest event estimated notional;
- first/last event timestamps.

A bucket exists only when at least one stored event exists. A missing bucket is **not automatically a
zero**: downstream evidence must also inspect stream-health/coverage before using it as a known-zero
feature.

## Collector

The operational command is:

```bash
export BYBIT_OPPORTUNITY_DATABASE_DSN='postgresql://...'
python -m tools.capture_bybit_forward_liquidations \
  --ws-host stream.bybit.com \
  --rank-limit 50 \
  --source-max-age-minutes 20 \
  --migrate-postgres
```

The collector:

1. loads the latest complete v110 opportunity snapshot from PostgreSQL;
2. fails closed if the source snapshot is stale;
3. keeps the exact rank order and exact Top-10 prefix;
4. subscribes to `allLiquidation.{symbol}` for up to 50 ranked candidates;
5. validates topic, symbol, type, timestamps, side, size and bankruptcy price;
6. stores events append-only;
7. stores CONNECTING / CONNECTED / HEARTBEAT / DISCONNECTED / STOPPED observations;
8. sends the documented application heartbeat and reconnects with bounded exponential delay.

Public market data needs no API key. This collector has no authenticated client and no order method.

## Safety boundary

Every v116 table and object remains research-only:

- `historical_backfill_available=false`;
- `exchange_event_id_available=false`;
- `trade_actionable=false`;
- `live_mainnet_order_routing_allowed=false`.

This PR does **not** change strategy parameters, evidence weights, LONG/SHORT policy, demo selection or
real-money routing.

## Next evidence unit

After enough clean forward coverage exists, a separate bounded change should join only liquidation
context that was known at or before each decision timestamp to the already-qualified live/prospective
chain:

```text
price / EMA / ATR
+ OI delta
+ crowding
+ prior funding
+ volatility/stress regime
+ execution economics
+ forward liquidation context with coverage proof
-> exact current condition cell
-> prospective 15m / 60m / 240m outcomes
-> discrimination / calibration report
```

Liquidation data must not be retroactively attached to historical periods that were never captured.
