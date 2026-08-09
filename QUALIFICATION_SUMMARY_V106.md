# Qualification Summary V106

## Deterministic local qualification

- focused tests: **161 passed**;
- overall branch-aware runtime coverage: **99%**;
- deployment qualification runtime coverage: **99%**;
- Kubernetes read-only adapter coverage: **100%**;
- PostgreSQL adapter coverage: **100%**;
- architecture audit: **PASS**;
- static security audit: **PASS**;
- compileall: **PASS**;
- release identity verification: **PASS**;
- stress: **1,000 iterations / 8 workers / 0 failures**;
- replay ledger size: **1,000**;
- unique journal tail digests: **1,000 / 1,000**.

## Reproducible package

Two independent wheel builds produced byte-identical files.

```text
SHA-256: ee1bd2f01de1c0d9de75eab2323f368fee00ee89cfd03c2a9783cef60cab8d5a
```

The wheel was installed into a clean virtual environment. Installed CLI checks for `live-status` and `verify-release` both passed.

## Defects found before publication

1. Live-host validation originally used substring comparison. This incorrectly treated the paper hostname `paper-api.alpaca.markets` as containing the live hostname `api.alpaca.markets`. Validation now compares exact parsed hostnames.
2. Kubernetes origin verification originally compared a request only with the current API base. It now independently checks the immutable host allowlist as a second boundary.
3. Architecture audit originally reported a missing file and then crashed while reading it. It now returns a complete fail-closed finding set without raising an unrelated exception.

## External boundary

No external Kubernetes cluster, PostgreSQL repository, S3-compatible storage, certificate authority, restore environment or long-running read-only fleet campaign was used during deterministic CI qualification. Kubernetes mutations, external order routing and live trading remain disabled.
