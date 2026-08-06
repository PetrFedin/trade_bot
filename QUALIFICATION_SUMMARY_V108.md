# Qualification summary — Schema 108 asymmetric signing authority

## Qualified source boundary

Schema 108 is a source-qualified asymmetric authorization layer stacked on the corrected Schema 107 rollout actuator. It defines an Ed25519 KMS/HSM provider protocol and public-key verification boundary; it does not claim that any production KMS/HSM, credential or root ceremony has been externally qualified.

## Qualification results

- focused Schema 108 suite: **43 passed**;
- expected complete stacked suite after publication: **761 passed**;
- exact branch-aware V108 runtime coverage: **98.234350%**;
- runtime statements measured: **477**;
- runtime branches measured: **146**;
- architecture audit: **PASS**;
- static security audit: **PASS**;
- compileall: **PASS**;
- V107 predecessor release verification: **PASS**;
- V108 release verification: **PASS**;
- canonical/package migration byte comparison: **PASS**;
- migration apply and idempotent re-apply against PostgreSQL 16: **PASS**;
- stress target: **1,000 authorization bundles / 8 workers / 0 failures / replay rejected**.

The complete stacked count and GitHub-hosted PostgreSQL result are confirmed by the permanent PR workflow before the branch is considered qualified.

## Verified invariants

- no runtime class imports or stores an Ed25519 private key;
- KMS/HSM provider output is locally verified before acceptance;
- root-signed keyring generations are strictly monotonic;
- revoked, inactive, unknown, wrong-purpose and wrong-generation keys fail closed;
- command authorization binds V107 command, policy and predecessor release identity;
- RELEASE, RISK and CONTROLLER resolve to distinct owners;
- executor receipt authorization binds receipt, command and bundle;
- signature IDs and nonces are consumed atomically in memory and durably in PostgreSQL;
- private-key-like fields are forbidden by the static audit;
- production mutation, external routing and live trading remain disabled.
