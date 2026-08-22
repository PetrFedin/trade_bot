# Bybit mainnet read-only activity evidence

## Scope

This layer reads broker activity from the real Bybit account without creating a real-money
order-routing path. It is stacked on the qualified mainnet read-only connection and public
server-time preflight.

The current scope is deliberately narrow: **linear USDT trading activity**, not every cash movement
that can occur in a Unified account.

It reads:

- `GET /v5/execution/list` for execution evidence;
- `GET /v5/position/closed-pnl` for closed-position PnL evidence;
- `GET /v5/account/transaction-log` for linear USDT cash-flow, funding, fee and balance changes.

## Bounded broker window

All activity reads use explicit `startTime` and `endTime`. The local model rejects windows longer
than seven days. The operational probe defaults to the latest 24 hours and anchors the end of that
window to the already-qualified Bybit server-time preflight, not to the deployment host's wall
clock.

Run:

```text
python -m app.runtime.bybit_mainnet_readonly_activity_probe
```

If clock readiness fails, the first authenticated activity request is never made.

## Execution evidence

The execution query is fixed to:

```text
category=linear
settleCoin=USDT
limit=100
startTime=<window start>
endTime=<window end>
```

The typed ledger retains Trade executions only. Non-Trade execution rows are counted explicitly in
`excluded_non_trade_execution_count` rather than being forced into fill economics.

Trade executions are keyed by broker `execId`. Exact repeats are idempotent; conflicting records
under the same `execId` fail closed. Output ordering is deterministic using execution time and
broker execution/order evidence.

An empty Bybit `feeCurrency` remains unknown. The ledger does not invent an `execution_fee_usdt`
summary when the broker row did not prove the fee currency.

## Closed PnL evidence

The linear closed-PnL endpoint can contain contracts settled outside USDT and does not provide a
settle-coin filter equivalent to execution history. The reader therefore keeps only normalized
symbols ending in `USDT` and exposes the number of excluded rows as
`excluded_non_usdt_closed_pnl_count`.

Each retained row preserves broker closed PnL, entry/exit values and average prices, fill count,
leverage, optional open/close fees, and broker timestamps. Conflicting rows under the same
symbol/order/update-time identity fail closed.

## Transaction-log accounting

The transaction query is fixed to:

```text
accountType=UNIFIED
category=linear
currency=USDT
limit=50
startTime=<window start>
endTime=<window end>
```

For every retained transaction, ASTRA verifies Bybit's documented accounting identity:

```text
change = cashFlow + funding - fee
```

The same identity is checked again over the aggregate window totals. A documented blank
`funding=""` marker is normalized to exact decimal zero; a missing funding field or an invalid
non-empty value remains a protocol error.

The current Bybit documentation labels transaction `id` as unique while its response example can
show the same `id` value on distinct rows. The ledger therefore uses a defensive composite broker
identity of transaction ID, transaction time, trade ID, order ID and transaction type. Exact
repeats are idempotent; conflicting economics under one composite identity fail closed.

The USDT totals exposed by this layer are explicitly limited to the filtered linear transaction
stream:

- `transaction_cash_flow_usdt`;
- `transaction_funding_usdt`;
- `transaction_fee_usdt`;
- `transaction_change_usdt`.

They are not described as complete account-wide cash reconciliation.

## Security boundary

This layer subclasses the qualified read-only Bybit client and preserves all of its controls:

- audited regional hostname allowlist;
- separate real-account read-only secrets;
- system-generated HMAC signing contract;
- exact API-key identity proof;
- Bybit `readOnly=1` proof;
- concrete API-key IP binding requirement;
- GET-only HTTPS transport;
- explicit read-path allowlist;
- `live_mainnet_order_routing_allowed=false`;
- `order_writes_supported=false`.

The activity snapshot independently revalidates its recorded API host against the same allowlist.
This code has no order placement, amendment, cancellation, transfer, withdrawal, leverage or margin
mutation surface.

## Next boundary

The next read-only financial layer should add an **account-wide USDT movement stream** without the
`category=linear` filter so deposits/transfers, spot/convert activity and other Unified-account cash
movements are not silently omitted. Only after that evidence is qualified should the project claim
full real-account cash-flow classification or reconciliation.
