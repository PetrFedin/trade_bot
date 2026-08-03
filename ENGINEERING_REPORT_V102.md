# ASTRA 7.32.0 / Schema 102 Engineering Report

Schema 102 converts one-off sandbox qualification into a durable, scheduled soak campaign.

## Runtime boundaries

- generation-fenced, expiring file lease with monotonically increasing fencing token;
- immutable campaign plan with deterministic SHA-256 identity;
- one active run per campaign;
- deterministic due slots and bounded schedule grace;
- explicit total and consecutive failure budgets;
- immediate block for recovery-required or residual paper exposure;
- quarantine for evidence identity, generation, time or routing corruption;
- append-only fsync/hash-chain campaign event journal;
- evidence artifact archive with chained manifest and retention deadline;
- restart reconstruction by replaying the journal;
- eligibility means only extended paper-soak eligibility, never live readiness.

No forecasting, signal, sizing or execution-price formulas were added.
