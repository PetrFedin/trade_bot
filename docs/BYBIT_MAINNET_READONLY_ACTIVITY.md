# Bybit mainnet read-only account activity

## Purpose

This layer reads authoritative activity from the real Bybit account without adding any real-money
order-writing capability. It is stacked on the qualified read-only connection and clock-preflight
boundaries.

It covers three broker sources:

- `GET /v5/execution/list` — fill-level execution evidence;
- `GET /v5/position/closed-pnl` — closed-position PnL evidence;
- `GET /v5/account/transaction-log` — Unified-account USDT cash-flow, funding, fee and balance
  changes.

The current layer is an evidence reader, not a trading adapter and not yet a claim that the three
sources have been fully reconciled into one accounting model.

## Bounded time window

Bybit currently limits these queries to a maximum seven-day interval when `startTime` and `endTime`
are both supplied. ASTRA therefore requires an explicit positive window of at most seven days and
always sends both endpoints.

The operational command uses the latest 24 hours, anchored to the already-qualified Bybit server
time preflight rather than the deployment host's local wall clock:

```text
python -m app.runtime.bybit_mainnet_readonly_activity_probe
```

If the clock preflight is unsafe, no authenticated activity request is sent.

## Execution evidence

Executions are requested with:

```text
category=linear
settleCoin=USDT
limit=100
startTime=<window start>
endTime=<window end>
```

ASTRA preserves fill-level `execId`, `orderId`, `orderLinkId`, side, quantity, price, value, fee,
fee currency, fee rate, maker/taker status, `closedSize`, `seq` and execution time.

Bybit documents that multiple executions may belong to one order and that rows sharing an
`execTime` can have unstable exchange ordering. ASTRA therefore treats `execId` as the broker
execution identity, fails closed if one ID returns conflicting economics, and emits deterministic
ordering using execution time plus the documented tie-break evidence.

An empty `feeCurrency` remains unknown. The reader intentionally does not label `execFee` as USDT
unless the broker record proves the fee currency.

## Closed-PnL evidence

The `linear` closed-PnL endpoint can contain both USDT and USDC contracts and does not expose a
`settleCoin` filter. ASTRA therefore:

- requests the bounded `linear` window;
- retains only normalized symbols ending in `USDT`;
- counts excluded non-USDT rows explicitly rather than silently mixing them into USDT accounting;
- preserves broker closed PnL, entry/exit values and prices, fill count, leverage, optional open/close
  fee fields and broker timestamps.

The filter is evidence isolation, not a deletion of the broker response: the excluded-row count is
part of the snapshot.

## Transaction-log accounting identity

The transaction log is requested as:

```text
accountType=UNIFIED
category=linear
currency=USDT
limit=50
startTime=<window start>
endTime=<window end>
```

Bybit documents:

```text
change = cashFlow + funding - fee
```

where positive `funding` means funding received, negative means funding paid, positive `fee` is an
expense and negative `fee` is a rebate. ASTRA validates that identity for every typed transaction
and again at the aggregate snapshot level. A conflicting broker row fails closed instead of being
silently normalized.

The snapshot totals are therefore explicitly named:

- `transaction_cash_flow_usdt`;
- `transaction_funding_usdt`;
- `transaction_fee_usdt`;
- `transaction_change_usdt`.

This is real-account broker evidence. It is not mixed with the Demo Trading session ledger.

## Idempotency and conflicts

- repeated identical execution IDs are deduplicated;
- the same execution ID with different fields is an error;
- repeated identical transaction IDs are deduplicated;
- the same transaction ID with different fields is an error;
- closed-PnL rows use broker symbol/order/update-time identity and conflicting records fail closed.

All emitted collections are sorted deterministically after deduplication.

## Security invariants

This layer inherits the qualified mainnet read-only client and therefore preserves:

- audited regional host allowlist;
- system-generated HMAC credential contract;
- exact API-key identity check;
- Bybit `readOnly=1` proof;
- concrete API-key IP binding requirement;
- GET-only transport;
- explicit private-read endpoint allowlist;
- `live_mainnet_order_routing_allowed=false`;
- `order_writes_supported=false`.

It does not implement placement, amendment, cancellation, leverage changes, margin changes,
withdrawals, transfers or any other mainnet mutation.

## Next accounting boundary

Once this typed broker evidence is qualified against repository-wide regression gates, the next
financial layer can reconcile the read-only transaction log against closed-PnL and fill evidence,
classify unexplained real-account cash movements and persist sanitized observations separately from
the Demo Trading authoritative state.
