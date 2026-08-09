# Integration contract — Schema 109

## Inputs

The integrator must provide:

- a root-verified `VerifiedRemoteSignerPolicyV109`;
- an implementation of `MutualTlsContextProviderV109` returning an `ssl.SSLContext` for the exact policy identity reference;
- `PostgresRemoteSignerRepositoryV109` with a role restricted to Schema 109 tables/functions;
- a clock and request/nonce generator that cannot repeat identifiers;
- a worker identity used for durable dispatch claims.

## Required sequence

1. Verify the root-signed policy snapshot against the expected root key, predecessor keyring digest and minimum generation.
2. Persist that verified policy. Reject same provider/generation with another digest.
3. Build a request from the verified policy, not a raw policy object.
4. Atomically persist request, outbox and audit event.
5. Atomically mark `DISPATCH_STARTED`.
6. Send one POST to `/v1/signing/requests`.
7. On deterministic rejection, persist `REJECTED`.
8. On transport failure, redirect, malformed success or ambiguous status, persist `UNCERTAIN`.
9. Reconcile only with GET `/v1/signing/requests/{request_id}`.
10. Verify payload signature and provider attestation before atomically advancing the audit checkpoint.
11. If reconciliation exceeds the signed policy deadline, persist `QUARANTINED`; manual review is required.

## Ambiguous statuses

The implementation treats 202, 204, all 3xx, 408, 409, 425, 429, all 5xx, transport exceptions and malformed successful payloads as ambiguous. None can trigger a second POST.

## Compatibility

`RemoteEd25519SigningProviderV109` implements the Schema 108 provider interface for one bound purpose/domain/keyring generation. Its synchronous `sign(payload)` returns only after the remote result has been verified and durably stored. Rejected, uncertain and quarantined outcomes raise fail-closed exceptions.

## Forbidden integration shortcuts

- passing an unsigned policy;
- disabling hostname or certificate verification;
- following redirects;
- retrying POST after timeout or 5xx;
- resetting audit sequence, root or signing counter;
- accepting a hardware cluster or firmware measurement outside the signed allowlist;
- persisting private keys in application storage;
- enabling external order routing or live trading through this layer.
