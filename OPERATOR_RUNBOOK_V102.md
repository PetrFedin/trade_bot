# Schema 102 operator runbook

## Before campaign start

- confirm the Schema 101 kill switch is not engaged;
- verify the campaign plan digest and generation;
- ensure the start/end window and run cadence are intentional;
- validate failure budgets and minimum verified run count;
- acquire a fresh lease and record its fencing token;
- verify event and evidence stores before any run.

## During the campaign

- renew or reacquire the lease before it expires;
- never reuse a fenced token;
- run no more than one qualification per claimed slot;
- store only sealed evidence matching campaign/run/generation;
- investigate every missed slot or preflight block;
- stop immediately for `RECOVERY_REQUIRED`, residual exposure or quarantine.

## Recovery

- reconstruct state by replaying the hash-chain journal;
- verify the evidence manifest and every artifact SHA-256;
- acquire a new fencing token after lease expiry;
- do not resume a blocked or quarantined campaign automatically;
- do not delete or rewrite event/evidence history.

## Completion

`COMPLETED` and `eligible_for_extended_paper_soak=true` permit only a longer paper-soak phase. They do not enable external live routing or live trading.
