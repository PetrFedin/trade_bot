# Bybit ENTRY crash-window recovery contract

This document defines the canonical safety contract for a Bybit demo ENTRY that may execute before the process creates the normal active-trade checkpoint.

## Qualified boundaries

The recovery work is deliberately stacked so each safety boundary has an exact qualified revision:

- PR #45 cash reconciliation: `cb09c8723acc64b23c66854b706c21035ab7b3f5`;
- PR #46 immutable pre-submit recovery envelope: `ab263b3dcef25d805434ea4f2701041092c1dfc0`;
- PR #47 executed-entry restart convergence: `9d1c61f57346df500271065160f4c6d8b31f429c`.

PR #47 completed successfully on all nine workflows triggered for that exact head, including the dedicated recovery gate, full trading regression, PostgreSQL OMS, product composition, operational readiness and schema roundtrip. A later stack must qualify its own exact head; parent CI is not proof for changed code.

## Crash window

The protected window is:

```text
fee-tier reconciliation
  -> fee-adjusted approved trade plan
  -> immutable recovery envelope
  -> broker ENTRY POST / possible execution
  -> process crash
  -> position exists at broker
  -> normal protected-trade checkpoint not yet durable
```

The OMS already prevents blind resubmission after `SUBMIT_STARTED`. The recovery contract adds the exact frozen information and deterministic convergence required to protect or reduce the resulting broker exposure without guessing.

## Pre-submit invariant

Before a risk-adding ENTRY POST the canonical cycle durably persists an immutable recovery envelope keyed by the deterministic ENTRY `orderLinkId`.

The envelope freezes:

- the fee-adjusted `CryptoTradePlan` actually approved for submission;
- the exact `BybitInstrumentSpec` used for quantity and price constraints;
- the effective `CryptoPerpStrategyConfig`, including the reconciled account taker fee;
- the approved order side and quantity;
- the pre-fill exit mode (`FIXED_20_TARGET` or `OPEN_ENDED_RUNNER`).

Persistence happens after account fee reconciliation and before `place_market_order`. Missing state, database failure, conflicting state or unsafe recovery-store capability blocks the ENTRY before POST.

`astra_bybit_entry_recovery` is immutable. Exact re-persistence of the same canonical payload is idempotent; a different payload for the same `orderLinkId` conflicts. PostgreSQL UPDATE and DELETE are rejected by trigger.

## Executed ENTRY restart convergence

The canonical product startup is recovery-aware. It discovers eligible crash candidates without changing the operational unresolved-entry SLO, reads broker truth by deterministic `orderLinkId`, loads and checksum-verifies the frozen envelope, and requires exact order/position identity before taking recovery ownership.

For linear perpetuals, automatic ownership requires terminal broker order truth: `Filled`, or `Cancelled` with a positive executed quantity. An open `New` or `PartiallyFilled` order remains blocked because outstanding quantity can still increase broker exposure.

After exact broker proof, recovery:

1. may retire only an expired stale runtime lease; an unexpired owner is never preempted;
2. acquires the next fencing token;
3. recomputes post-fill economics only from the frozen inputs plus actual broker fill basis;
4. never upgrades a frozen fixed target to runner; a frozen runner may only remain runner or downgrade;
5. installs and verifies exchange-native protection;
6. if protection/economics/liquidation safety is not provable, uses the canonical reduce-only emergency close;
7. persists the normal PostgreSQL trade checkpoint under the new fence, including after a successful recovery flatten so terminal accounting cannot be skipped;
8. converges OMS lifecycle state without a second risk-adding ENTRY POST;
9. releases the recovery lease and hands the checkpoint back to the normal product lifecycle.

Entries already represented by immutable terminal evidence are historical and are excluded from recovery discovery. A `FILLED` entry already matching the active checkpoint is handed directly to normal management and is not protected a second time.

The crash/restart path is qualified against real PostgreSQL, including stale-fence retirement, fencing-token increment, checkpoint creation, OMS `FILLED`, old-owner invalidation and an idempotent second startup.

## Final mutation-boundary defense

The canonical OMS-aware ENTRY client is bound to the same PostgreSQL recovery store. The current hardening stack adds a second independent check immediately before the OMS ENTRY claim: reload the immutable record, verify its canonical checksum through the store, validate the envelope, and require exact `orderLinkId`, symbol, side and quantity agreement with the broker request.

Therefore a future direct call path cannot rely only on the surrounding cycle to have done the right thing. Missing, corrupt or mismatched recovery state must stop the risk-adding mutation before both the OMS claim and broker POST.

Reduce-only safety orders intentionally do not depend on the ENTRY recovery envelope. Operator pause/read-only/kill and recovery hardening must never prevent a required risk-reducing close.

## Non-negotiable safety boundaries

- no blind ENTRY resubmission after durable submission start;
- no second risk-adding ENTRY POST during restart recovery;
- generic automatic stale-lease takeover remains disabled;
- no reconstruction from current strategy defaults;
- no strategy/risk retuning as part of recovery;
- no LONG/SHORT policy or runner-threshold change;
- `next_entry_allowed=false` throughout recovery until the normal lifecycle independently authorizes entry;
- mainnet/live order routing remains disabled.

This document describes qualified parent behavior plus the current mutation-boundary hardening. The current hardening branch is not qualified until all workflows triggered for its own exact head complete successfully.
