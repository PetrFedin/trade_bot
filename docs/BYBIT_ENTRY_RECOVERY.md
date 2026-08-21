# Bybit ENTRY crash-window recovery contract

This document defines the bounded prerequisite for unattended recovery of a Bybit ENTRY that may execute before the process creates the normal active-trade checkpoint.

## Qualified parent boundary

The parent cash-reconciliation stack is qualified at exact head `cb09c8723acc64b23c66854b706c21035ab7b3f5`. This recovery-envelope change must qualify its own exact head; parent CI is not proof for this branch.

## Crash window being closed

The unsafe reconstruction window is:

```text
fee-tier reconciliation
  -> fee-adjusted approved trade plan
  -> broker ENTRY POST / possible execution
  -> process crash
  -> position exists at broker
  -> normal protected-trade checkpoint not yet durable
```

Existing OMS behavior already prevents blind resubmission and leaves uncertain/filled lifecycle state blocked. What was missing was the exact pre-submit information required to reconstruct protection without guessing.

## New invariant

On the canonical OMS-aware Bybit path, before a risk-adding ENTRY POST the product must durably persist an immutable recovery envelope keyed by the deterministic ENTRY `orderLinkId`.

The envelope freezes:

- the fee-adjusted `CryptoTradePlan` actually approved for submission;
- the exact `BybitInstrumentSpec` used for quantity/price constraints;
- the effective `CryptoPerpStrategyConfig`, including the broker account taker fee used for the decision;
- the approved order side and quantity;
- the pre-fill exit mode (`FIXED_20_TARGET` or `OPEN_ENDED_RUNNER`).

Persistence happens after actual account fee reconciliation and before `place_market_order`. Missing state, database failure, conflicting state or unsafe recovery-store capability blocks the ENTRY before POST.

The canonical OMS-aware client is bound to the same PostgreSQL DSN and advertises `entry_recovery_required=true`, so this is not an optional production telemetry path.

## Durability

`astra_bybit_entry_recovery` is immutable. Exact re-persistence of the same canonical payload is idempotent; a different payload for the same `orderLinkId` is a conflict. PostgreSQL UPDATE and DELETE are rejected by trigger.

## What this stack does not claim

This stack does **not** yet claim that an already executed unprotected ENTRY will automatically converge after restart. It only removes the need to guess the original risk/protection inputs.

The following bounded stack must still:

1. read OMS and broker truth by deterministic `orderLinkId`;
2. load and checksum-verify the recovery envelope;
3. reconcile executions and the actual broker position;
4. recompute post-fill economics only from the frozen inputs plus actual fill truth;
5. place and verify exchange-native protection, or issue a canonical reduce-only emergency close if protection cannot be made safe;
6. create the normal active-trade checkpoint from the recovered position;
7. converge OMS lifecycle state without any second ENTRY POST;
8. prove the crash/restart path on real PostgreSQL with deterministic broker fault fixtures.

Until that follow-on stack is qualified, full unattended restart recovery remains incomplete and mainnet routing remains disabled.
