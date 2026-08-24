# Operator-approved Bybit Demo execution

This layer connects the evidence-ranked review queue to the existing Bybit Demo execution stack without enabling autonomous real-money trading.

## Boundary

The live mainnet account remains read-only. This change does not add a mainnet order client, does not change the `bybit_live_order_routing_allowed=false` contract, and does not turn the v111 opportunity rows themselves into actionable orders.

A demo order requires a separate, short-lived operator approval capability.

## Canonical flow

```text
latest v111 positive-evidence review queue
-> operator chooses exact evidence rank (and optionally exact symbol)
-> reproduce the fixed signal from completed 5m bars through the source decision bar
-> reproduce source notional / risk / cost / expected edge
-> exact confirmation phrase
-> <= 2 minute BYBIT_DEMO approval
-> canonical single-writer runtime lease
-> durable active-trade checkpoint check
-> latest review-row identity recheck
-> current canonical demo selector independently chooses the same signal
-> demo wallet / margin / session ledger / previous-trade reconciliation
-> fresh demo quote + execution-risk resize
-> exact-identity single-use demo client guard
-> exact non-reduce-only entry reaches the pre-network boundary
-> immutable authorization lineage is persisted and burned
   source snapshot/ranks -> approval_id -> deterministic entry orderLinkId
-> raw Bybit Demo entry HTTP mutation
-> existing guarded protection / recovery / accounting
-> immutable protected-entry provenance on the same orderLinkId
-> restart-safe active-trade management
-> terminal evidence + fees + funding + realized PnL
-> approved-trade attribution on the same orderLinkId
```

If any step changes the symbol, side, decision, source snapshot, rank or source economics, the approval is rejected rather than rerouted.

The operator-approved path does **not** use ranked fallback after approval. If the exact approved candidate becomes uneconomic or non-executable at the fresh quote/fee/account boundary, the result is no entry. It cannot silently substitute another symbol.

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

Historical evidence ranking and the current demo selector are related but they are not yet the same execution policy. The safe bridge therefore does **not** force the evidence-ranked symbol into the strategy runtime.

Immediately before the account-sized demo cycle, the current canonical selector must independently select the same:

```text
symbol × side × decision_time
```

The selected current plan may only be equal or smaller than the source-approved notional, risk, modeled cost and quantity caps. If refreshed account equity, portfolio state, session risk, correlation, exchange minimums or other current guards make another candidate preferable or block this candidate, execution stops.

Changing the execution selector to prefer evidence rank would be a separate strategy/execution-policy change and must be prospectively qualified before activation.

## Canonical single-writer runtime

`run_operator_approved_bybit_demo_trading_runtime` uses the existing `run_bybit_demo_trading_runtime` instead of creating a second lifecycle engine.

The canonical runtime remains authoritative for:

- exclusive runtime lease acquisition;
- durable active-excursion checkpoint inspection;
- choosing **entry or management**, never both;
- protected-entry provenance;
- restart-safe open-position polling;
- terminal evidence handoff;
- preventing a replacement entry in the same invocation after terminal handoff.

The short-lived approval is consulted only when the canonical runtime has proven that no active checkpoint exists and is about to enter the new-entry boundary. An already-open Demo position therefore continues to be managed and reconciled even after its original approval expires.

## Durable pre-submit authorization lineage

The durable record is intentionally written as late as possible while still being strictly **before the raw Demo HTTP mutation**.

The client stack for an approved entry is:

```text
canonical trading runtime
-> approved account-sized bridge
-> OperatorApprovedBybitDemoClient
   (exact identity + in-process single-use guard)
-> DurableApprovalLineageBybitDemoClient
   (durable pre-network burn)
-> raw BybitDemoOrderClient
   (api-demo.bybit.com HTTP mutation)
```

This ordering matters. Wallet, margin, session-risk, fee, fresh-quote, quantity and other pre-order checks happen before the durable layer receives an entry request. A harmless pre-order rejection therefore does **not** burn the approval.

Only when the exact approved non-reduce-only order is actually about to be sent does the durable layer persist an immutable outcome-free record keyed by the deterministic expected entry `orderLinkId`. The raw Demo client is called only after that persistence succeeds.

The record contains:

- `approval_id`;
- source v111 snapshot ID;
- source evidence rank;
- source market rank;
- symbol / LONG or SHORT / decision time;
- signal availability time;
- approval issued and expiry timestamps;
- deterministic entry and close `orderLinkId` values;
- explicit Demo-only, outcome-free and no-mainnet markers.

It deliberately contains no fill, fee, funding, MFE/MAE or realized PnL. Those are future outcomes and remain outside the pre-submit decision record.

The authorization payload is deterministic from the approval. Its authorization timestamp is the approval timestamp rather than the process restart time, so the same approval cannot create a different payload merely because the process restarted at another second.

### Durable single-use semantics

The first successful authorization persistence **burns the approval/order identity for new submissions before the network mutation**.

If the same authorization record is found again on a later invocation, the pre-network client treats it as:

```text
RECOVERY / RECONCILIATION REQUIRED
```

not as permission to resubmit.

This closes the crash window in which a process could have reached the network and died before local post-submit state became durable. Even if the prior HTTP outcome is unknown, the same approval cannot issue another entry. Recovery must first determine exchange state through the existing reconciliation paths.

A failure to durably persist authorization interrupts the order call before the raw Demo network client is reached.

## Last-line demo client guard

The qualified `BybitDemoOrderClient` can only reach `api-demo.bybit.com`. It is additionally wrapped by `OperatorApprovedBybitDemoClient` for an approved cycle, with the durable lineage client directly beneath that guard.

The approval guard permits:

- one non-reduce-only entry only;
- the exact approved symbol;
- the correct Buy/Sell side for approved LONG/SHORT;
- the deterministic `ASTRA-DEMO-E-*` orderLinkId derived from the approved decision identity;
- quantity no greater than the approved source quantity;
- protection only after the approved entry authorization is consumed;
- emergency reduce-only close only for the same trade identity and quantity cap;
- execution/cancel reads only for the same approved order identities.

The in-memory entry authorization is consumed before the lower client is invoked. The lower client then makes durable single-use/restart semantics explicit before forwarding the exact request to the raw Demo network client.

## End-to-end evidence attribution

The generic protected-entry provenance remains outcome-free and records the actual protected fill, execution economics and selection facts under the entry `orderLinkId`.

The existing terminal attribution later joins that same entry identity to fully reconciled:

- execution fees;
- funding;
- all-in net PnL;
- edge realization;
- R multiple;
- excursion / giveback diagnostics.

`build_bybit_demo_approved_trade_attribution` adds the evidence approval lineage only after the generic all-in attribution is already complete. The resulting post-trade diagnostic therefore provides the exact chain:

```text
source_snapshot_id
+ source_evidence_rank
+ source_market_rank
+ approval_id
+ entry_order_link_id
+ terminal_record_sha256
-> execution_fees_usdt
-> funding_net_usdt
-> all_in_net_pnl_usdt
-> all_in_r_multiple
```

Realized PnL remains explicitly forbidden from feeding the online selector, automatic threshold retuning, exit retuning or strategy promotion.

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

## Activation boundary

The GitHub workflow in this stage remains a **qualification workflow only**. It does not connect Demo credentials or submit a network order.

A later connected Demo execution path should be added only after this canonical runtime/lineage layer is fully qualified. It must still require an explicit short-lived operator approval and dedicated Demo credentials. It must never reuse the mainnet read-only key or turn evidence rows into autonomous order instructions.

A persistent execution runtime is preferable to a GitHub-hosted ephemeral runner for the actual managed-trade lifecycle because runtime lease, active excursion state, provenance and terminal handoff must survive process restarts. Any connected Demo deployment must prove that durable-state boundary before it is allowed to place a Demo entry.

## What this does not do

- It does not autonomously approve a trade.
- It does not schedule demo entries without an operator confirmation.
- It does not execute from `QUALIFIED_MIXED_EVIDENCE` or insufficient/incomplete evidence states.
- It does not use a historical result as a guarantee of future profit.
- It does not enable live mainnet order creation, amendment or cancellation.

Mainnet account data can continue to inform read-only equity/position context through the qualified read-only boundary, while all order writes in this bridge remain confined to Bybit Demo.
