# Verification Reference Profiles v1

Reference Profiles v1 define portable, versioned conformance profiles over the canonical ASTRA verification stack.

They are configuration and admission metadata only. They are not verifiers, Qualification Profile Registry records, trust authorities or trading permissions.

## Identity

Each profile has:

- schema version;
- profile ID;
- profile version;
- profile reference;
- canonical bytes;
- stable SHA-256 identity.

The same logical profile produces byte-identical canonical JSON and the same profile SHA.

## Declared contract

A profile declares:

- the exact supported Verification API operation set;
- artifact type;
- artifact codec schema;
- bundle schema;
- canonicalization policy;
- trusted-root-set identifier policy;
- maximum clock skew;
- interface type;
- idempotency preservation requirement;
- transport-only retry requirement;
- semantic retry prohibition;
- authority persistence requirement;
- crash-recovery requirement;
- CAS requirement;
- external-network allowance.

Operation sets are exact. A read-only profile does not conform if the capability also exposes verify.advance. A stateful authority profile does not conform if verify.advance is absent.

## Trust-root policy

Reference Profiles contain no public or private key material.

The v1 policy is EXPLICIT_CONFIGURED_ID: the implementation must supply a non-blank logical trusted_root_set_id through its own configured registry.

Profiles therefore describe the requirement for explicit trust-root selection without becoming a second source of trust roots.

## Canonical v1 profiles

1. Offline Institutional Verifier
2. Local HTTP Institutional Verifier
3. Embedded/OEM Verifier
4. Read-only Auditor
5. Stateful Authority Operator

Each profile has a distinct stable identity and SHA.

## Conformance capability

A capability declaration describes the implementation/environment being tested:

- exposed operations;
- artifact contract;
- trusted-root-set ID;
- clock skew;
- interface;
- SDK idempotency and retry behavior;
- authority persistence;
- crash recovery;
- CAS behavior;
- external-network dependency.

The capability itself has a stable SHA-256 identity.

## Conformance evidence

Evaluation produces deterministic evidence containing:

- profile_ref;
- profile_sha256;
- capability_sha256;
- compatible;
- canonically ordered failure codes;
- conformance_sha256.

The evaluator does not verify artifacts, signatures, keyrings or TrustState. It compares declared configuration and behavioral requirements only.

## Fail-closed mismatch taxonomy

v1 includes stable failures for:

- operation-set mismatch;
- artifact type mismatch;
- artifact codec schema mismatch;
- bundle schema mismatch;
- canonicalization mismatch;
- missing explicit trusted-root-set ID;
- excessive clock skew;
- interface mismatch;
- missing idempotency preservation;
- missing transport-only retry discipline;
- forbidden semantic retry;
- authority persistence mismatch;
- missing crash recovery;
- missing CAS;
- forbidden external-network dependency.

Multiple failures are returned in deterministic lexical order.

## Security boundary

Reference Profiles v1 do not:

- import VerificationServiceV4;
- mutate TrustState;
- interpret signatures or keyrings;
- contain private signing keys;
- embed trusted-root public keys;
- authorize broker, OMS, strategy, risk or live-routing actions;
- claim profitability, performance or regulatory certification.

## Next gate

After Reference Profiles v1 passes qualification, schema, security, hygiene, import-boundary and status-source-of-truth gates and merges, the next layer is the Institutional Test Corpus.
