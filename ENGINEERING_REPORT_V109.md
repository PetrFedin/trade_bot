# Engineering report — Schema 109 remote signer attestation

## Decision

Schema 109 replaces the abstract private signing call introduced in Schema 108 with a bounded remote KMS/HSM protocol. The runtime remains a public-key verifier. It never accepts or stores private-key material and it cannot automatically retry a signing mutation.

## Threats addressed

1. **Ambiguous remote mutation.** A process can lose the HTTP response after the provider signed. The request and outbox are committed before dispatch, `DISPATCH_STARTED` is committed before the sole POST, and every ambiguous outcome becomes `UNCERTAIN`. Recovery performs GET only.
2. **Endpoint or policy substitution.** Endpoint, mTLS identity, signing and attestation keys, hardware clusters and firmware measurements are carried by a root-signed, monotonic policy snapshot. Its digest and generation are bound into every request.
3. **Provider replay or rollback.** Request IDs and nonces are unique; provider audit sequence and hardware signing counter are strictly monotonic; PostgreSQL advances the audit checkpoint with compare-and-set semantics.
4. **Forged provider result.** The payload signature and a separate provider-attestation signature are verified with different Ed25519 public keys. The attestation binds request digest, signature digest, hardware identity, firmware measurement, counter, audit event and hash-chain roots.
5. **Crash between marker and response.** A persisted `DISPATCH_STARTED` record is treated as potentially executed. It can only enter durable `UNCERTAIN` and GET reconciliation; a second POST is structurally unavailable.
6. **Policy equivocation.** PostgreSQL uniquely binds `(provider_id, policy_generation)` to one snapshot digest. Reusing a generation with different policy content is rejected.

## Transaction boundaries

- `install_verified_policy`: immutable signed policy snapshot, with generation equivocation detection.
- `create_request_with_outbox`: request, outbox and request-created event in one transaction.
- `mark_dispatch_started`: conditional state transition and append-only event before network mutation.
- `record_signed`: response, terminal request state, monotonic checkpoint and append-only event in one transaction.
- failure transitions: deterministic state/event update with rollback on any conflict.

## HTTP boundary

- exact HTTPS origin only;
- TLS peer and hostname verification required;
- TLS 1.3 minimum;
- caller-provided mTLS identity context;
- redirects rejected;
- bounded timeout and response size;
- exactly one runtime POST call site;
- no automatic POST retry;
- GET-only reconciliation by request ID.

## Remaining external work

Schema 109 does not prove a production provider, certificate chain, HSM firmware, audit sink or operational key ceremony. Those controls remain explicit external qualification requirements and production/live flags remain false.
