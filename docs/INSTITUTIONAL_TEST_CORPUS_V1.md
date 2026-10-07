# Institutional Test Corpus v1

Institutional Test Corpus v1 is a portable, immutable vector format and deterministic comparison runner for the ASTRA verification stack.

It is deliberately not a verifier. It does not interpret signatures, trusted roots, TrustState or execution authority.

## Case classes

The v1 corpus recognizes twelve classes:

- POSITIVE_VERIFICATION
- DETERMINISTIC_REJECTION
- ROLLBACK_REPLAY_STALE_STATE
- IDEMPOTENCY_SINGLE_FLIGHT
- CAS_CONFLICT
- SERIALIZATION_REJECTION
- TRUSTED_ROOT_SET_MISMATCH
- REFERENCE_PROFILE_CONFORMANCE
- SDK_RETRY_CORRELATION
- TRANSPORT_FRAMING
- CRASH_RECOVERY
- FORWARD_COMPATIBILITY

## Case identity

Each case contains:

- schema version;
- case class;
- human-readable intent;
- exact input bytes encoded as strict canonical base64;
- expected result class;
- expected failure code when applicable;
- expected mutation behavior;
- expected authority generation delta;
- optional expected output SHA-256;
- optional Reference Profile identity;
- provenance label.

The case SHA covers all of these fields.

The case ID is derived from the case SHA.

The same logical case therefore produces the same canonical bytes, SHA and ID.

## Mutation expectations

The corpus uses explicit mutation semantics:

- NONE
- ADVANCE_ONCE
- CONFLICT_NO_ADVANCE
- RECOVERY_NO_SECOND_ADVANCE

Mutation expectation and generation delta are validated together.

This makes stateful safety part of the vector itself instead of an informal test comment.

## Executor boundary

The corpus runner receives an external executor implementing the corpus executor protocol.

The executor is responsible for running the system under test.

The corpus runner only compares the returned observation with the case contract.

It does not:

- verify signatures;
- inspect keyrings;
- select trusted roots;
- mutate TrustState;
- perform semantic retries;
- call broker, OMS, strategy, risk or live-routing authority.

## Observation

An observation contains:

- result class;
- failure code;
- mutation behavior;
- authority generation delta;
- optional output SHA-256.

The observation has its own deterministic SHA identity.

## Deterministic comparison

The runner compares:

- result class;
- failure code;
- mutation behavior;
- generation delta;
- output SHA when the case declares one.

Mismatch codes are canonically sorted.

The runner output therefore remains deterministic even when several expectations fail at once.

## Report

A corpus report contains:

- corpus SHA;
- ordered case results;
- pass/fail;
- observation payloads and observation SHAs;
- deterministic report SHA.

Cases and results are ordered by case ID.

Running the same corpus against the same observations must produce byte-identical report bytes and the same report SHA.

## Reference Profiles

Corpus cases may bind:

- profile_ref;
- profile_sha256.

Both fields must be present together.

This binds profile conformance vectors to the exact canonical Reference Profile definition.

## Serialization boundary

The case decoder is strict:

- canonical UTF-8 JSON only;
- no BOM;
- no duplicate keys;
- no floats or non-finite JSON;
- exact field set;
- strict canonical base64;
- case ID and SHA recomputed and verified.

Malformed raw input intended for the system under test may still be stored in input_b64. The corpus layer preserves those bytes but does not parse or normalize them.

## Real-stack qualification

Qualification tests include vectors that exercise the real canonical Verification API stack:

- positive read-only verification with no authority mutation;
- wrong trusted-root-set rejection with no authority mutation;
- idempotent verify.advance replay with exactly one authority generation advancement and byte-identical replay output.

All twelve corpus classes and all five canonical Reference Profiles are represented by portable vectors in the test suite.

## Security and reproducibility

Institutional Test Corpus v1 requires:

- no external network;
- no wall-clock dependency in the corpus layer;
- no hidden randomness;
- no committed private signing keys;
- no duplicated trust semantics;
- no expansion of the frozen runtime import boundary.

## Next gate

After Institutional Test Corpus v1 passes qualification, schema, security, hygiene, import-boundary and status-source-of-truth gates and merges, the next layer is OEM / Embedded Qualification.
