# Operator-approved Bybit Demo execution

This layer connects the evidence-ranked review queue to the existing Bybit Demo execution stack without enabling autonomous real-money trading.

## Boundary

The live mainnet account remains read-only. This change does not add a mainnet order client, does not change the `bybit_live_order_routing_allowed=false` contract, and does not turn the v111 opportunity rows themselves into actionable orders.

A demo order requires a separate, short-lived operator approval capability.

## Flow

```text
latest v111 positive-evidence review queue
-> operator chooses exact evidence rank (and optionally exact symbol)
-> reproduce the fixed signal from completed 5m bars through the source decision bar
-> reproduce source notional / risk / cost / expected edge
-> exact confirmation phrase
-> <= 2 minute BYBIT_DEMO approval
-> latest review-row identity recheck
-> current canonical demo selector independently chooses the same signal
-> demo wallet / margin / session ledger / previous-trade reconciliation
-> fresh demo quote + execution-risk resize
-> exact-identity single-use demo client guard
-> existing guarded Bybit Demo orchestrator
-> protection / recovery / accounting
```

If any step changes the symbol, side, decision, source snapshot, rank or source economics, the approval is rejected rather than rerouted.

## Approval creation

The preparation command never sends an order:

```bash
python -m tools.prepare_bybit_demo_operator_approval \
  --site eu \
  --evidence-rank 1 \
  --symbol BTCUSDT \
  --confirm APPROVE_BYBIT_DEMO_EXECUTION \
  --output artifacts/bybit-demo-approval.json
```

`--symbol` is optional but recommended as an additional anti-drift check. The command reads only `QUALIFIED_POSITIVE_EVIDENCE` rows from the latest review queue and uses the regional public kline host to reproduce the fixed signal through the exact source decision bar.

The approval is refused when:

- the qualification state is not `QUALIFIED_POSITIVE_EVIDENCE`;
- the historical cell is not sample sufficient or is not positive;
- any original safety/activation flag became true;
- the confirmation phrase differs from `APPROVE_BYBIT_DEMO_EXECUTION`;
- the signal cannot be reproduced from completed bars;
- side, decision time, quality, planned notional, risk, modeled cost or expected edge differ from the source row;
- more than 10 minutes have passed since the signal became available;
- approval TTL is longer than 120 seconds.

The output is explicitly `prepared_only=true` and `order_write_performed=false`.

## Why the canonical demo selector must also agree

Historical evidence ranking and the current demo selector are related but they are not yet the same execution policy. The first safe bridge therefore does **not** force the evidence-ranked symbol into the strategy runtime.

Immediately before the account-sized demo cycle, the current canonical selector must independently select the same:

```text
symbol × side × decision_time
```

The selected current plan may only be equal or smaller than the source-approved notional, risk, modeled cost and quantity caps. If refreshed account equity, portfolio state, session risk, correlation, exchange minimums or other current guards make another candidate preferable or block this candidate, execution stops.

Changing the execution selector to prefer evidence rank would be a separate strategy/execution-policy change and must be prospectively qualified before activation.

## Last-line demo client guard

The underlying qualified `BybitDemoOrderClient` can only reach `api-demo.bybit.com`. It is additionally wrapped by `OperatorApprovedBybitDemoClient` for an approved cycle.

The wrapper permits:

- one non-reduce-only entry only;
- the exact approved symbol;
- the correct Buy/Sell side for approved LONG/SHORT;
- the deterministic `ASTRA-DEMO-E-*` orderLinkId derived from the approved decision identity;
- quantity no greater than the approved source quantity;
- protection only after the approved entry authorization is consumed;
- emergency reduce-only close only for the same trade identity and quantity cap;
- execution/cancel reads only for the same approved order identities.

The entry authorization is consumed **before** the underlying network mutation. An ambiguous HTTP outcome therefore cannot be retried through the same approval and accidentally duplicate the entry.

## Existing risk controls remain authoritative

The bridge delegates execution to the existing account-sized reconciled demo runtime. It does not bypass:

- authoritative demo wallet/equity refresh;
- supported margin-mode checks;
- available-balance checks;
- reconciled session-risk ledger;
- previous-trade lifecycle/funding/accounting reconciliation;
- maximum concurrent positions;
- fixed strategy signal/plan economics;
- current quote and execution-risk re-sizing;
- exchange quantity/min-notional constraints;
- fee-tier revalidation;
- liquidation safety;
- entry recovery envelope / reconciliation;
- hard stop and fixed-target or open-ended runner protection.

## What this does not do

- It does not autonomously approve a trade.
- It does not schedule demo entries without an operator confirmation.
- It does not execute from `QUALIFIED_MIXED_EVIDENCE` or insufficient/incomplete evidence states.
- It does not use a historical result as a guarantee of future profit.
- It does not enable live mainnet order creation, amendment or cancellation.

Mainnet account data can continue to inform read-only equity/position context through the already qualified PR #65 boundary, while all order writes in this bridge remain confined to Bybit Demo.
