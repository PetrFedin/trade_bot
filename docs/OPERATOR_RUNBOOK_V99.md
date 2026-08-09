# Schema 99 operator runbook

## Purpose

Schema 99 verifies one bounded paper-only broker mutation cycle. It is not a live-trading enablement mechanism.

## Required admission evidence

Before starting a round trip, confirm all of the following:

- paper session is `RUNNING`;
- generation matches the active deployment lease;
- operator approval covers the entire plan window;
- platform, broker reliability and continuous qualification are ready;
- kill switch is not engaged;
- account ID, status and currency match policy;
- broker reports positive buying power and no trading block;
- instrument is allowlisted and quantity/notional limits pass.

## Normal sequence

1. Acquire the deployment lease.
2. Mark the worker `RUNNING`.
3. Execute one paper-only limit submit.
4. Confirm broker acknowledgement.
5. Replace only when the policy requires it.
6. Cancel the broker order.
7. Reconcile that the client order is absent from open orders.
8. Verify the hash-chain journal.
9. Drain the worker and stop it with zero outstanding work.

## Failure handling

- Never blindly repeat an ambiguous submit, replace or cancel.
- Use read-only lookup by `client_order_id`.
- Enter `RECOVERING` when broker evidence is insufficient.
- Enter `BLOCKED` on identity mismatch, stale evidence, kill switch, policy failure or any fill.
- Treat any fill as residual paper exposure requiring operator review.
- Quarantine deployment after crash-budget exhaustion or drain timeout.

## Verification

```bash
python -m pytest -q
python -m tools.platform_v99 verify-journal ./roundtrip.jsonl
```

Expected immutable boundary:

```text
external_order_routing_allowed = false
live_trading_allowed = false
```
