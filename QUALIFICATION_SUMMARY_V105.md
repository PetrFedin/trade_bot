# Schema 105 qualification summary

## Reproduced final-tree results

- focused tests: **106 passed**;
- total branch-aware runtime coverage: **96%**;
- fleet operations runtime coverage: **95%**;
- S3-compatible evidence adapter coverage: **99%**;
- PostgreSQL fleet repository coverage: **100%**;
- architecture audit: **PASS**;
- static security audit: **PASS**;
- compileall: **PASS**;
- deterministic stress: **1,000 iterations / 8 workers / 0 failures**;
- unique event-chain tail digests: **1,000 / 1,000**;
- deterministic wheel: two byte-identical builds;
- wheel SHA-256: `ac67fcdad26142d13293961e44a69ed46262ae730a46acdb25cc5403a57240b6`;
- clean virtualenv installation: **PASS**;
- installed `astra-platform-v105 live-status` smoke: **PASS**.

## Defects found before publication

1. Scale-down was originally permitted while active claims existed when the reduced capacity still covered the count. The final implementation blocks every scale-down while any claim is active or any worker is draining.
2. The informational `live-status` CLI originally returned exit code 2 because the honest status was `LOCAL_QUALIFICATION_ONLY`. It now returns success; non-zero remains reserved for failed verification.

## Safety invariants exercised

- enrollment token and nonce are single-use;
- signing-key and certificate rotation are generation-fenced;
- retired signing keys may validate unexpired tokens but cannot issue new tokens;
- revoked keys and certificates fail closed;
- no claims are accepted during drain, containment, quarantine, stop or revocation;
- drain completion requires zero active claims and flushed evidence;
- drain timeout quarantines the worker;
- autoscaling is bounded by replica limits, step limits, cooldowns and stabilization;
- autoscaling is held during containment, dependency failure or incident-budget exhaustion;
- containment release requires two distinct operators and cleanup evidence;
- S3-compatible transport is HTTPS-only, TLS-verified and redirect-disabled;
- storage mutations are single-attempt; only read recovery is retried;
- multipart parts and final object metadata are SHA-256 verified;
- PostgreSQL task claims use `FOR UPDATE SKIP LOCKED` and monotonic fencing;
- append-only operational relations reject update/delete;
- external order routing and live trading remain disabled.

## Honest external boundary

These results do not prove a deployed Kubernetes fleet, external S3-compatible service, external PostgreSQL HA repository, production autoscaling actuator, production identity authority or external Alpaca fleet session.
