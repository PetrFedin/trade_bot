# ASTRA 7.28.0 / Schema 98 Release Notes

Schema 98 introduces a supervised lifecycle for paper sessions. It converts continuous qualification into a persisted enforcement boundary around paper-order admission.

## Safety changes

- paper-order admission is allowed only while the persisted state is exactly `RUNNING`;
- start requires current approval, an open session window, fresh qualification evidence and healthy dependencies;
- every mutating command is generation-fenced and idempotent;
- missed heartbeat moves an uncertain session to `RECOVERING`;
- controlled drain waits for zero orders, workers and outbox backlog;
- drain timeout, dependency loss, critical incident or qualification loss fails closed;
- kill switch is sticky for the current session;
- recovery requires a newer generation and can return only to `STOPPED`;
- event history and checkpoint detect tampering, truncation and removal;
- persistence failure cannot advance in-memory state;
- CLI evidence validation remains active under optimized Python execution;
- deterministic wheel and ZIP builders do not depend on externally installed packaging tools;
- unrestricted external order routing and live trading remain disabled.

## Qualification

- focused tests: 45 passed;
- full regression: 721 passed, 6 external PostgreSQL checks skipped;
- architecture audit: PASS;
- static audit: PASS;
- stress: 1,000 iterations, 8 workers, 0 failures;
- reproducible wheel and ZIP: PASS;
- exact patch-tree reproduction and Git bundle verification: PASS.

## Release identity

- final commit: `71a705895a51573b8897c8e48543089366e61c0f`;
- final tree: `4faa170db703a3b27caa94060384c13ceda30e22`;
- manifest digest: `dcfca53c6b66f8b62ea8c549515c867b93c828ba12ec170b4ffe2ff6993ce15d`.

No predictive formulas, indicators or strategy parameters were added. The full source history is distributed as the qualified Schema 98 Git bundle and is not yet the content of remote `main` until the guarded import is run.