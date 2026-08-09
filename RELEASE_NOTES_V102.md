# ASTRA 7.32.0 / Schema 102

Schema 102 adds a durable sandbox soak orchestrator with scheduled run slots, generation fencing, lease ownership, failure budgets, evidence retention, append-only journals and restart replay.

Critical outcomes fail closed:

- residual paper exposure blocks immediately;
- unresolved recovery blocks future windows;
- evidence identity/time/generation corruption quarantines the campaign;
- stale lease holders cannot start or finish runs;
- missed schedule windows consume the failure budget.

Live routing remains disabled.
