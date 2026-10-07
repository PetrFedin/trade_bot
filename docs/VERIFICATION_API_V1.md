# Verification API v1

Verification API v1 is the transport-neutral institutional verification contract for ASTRA qualification evidence.

It is not an HTTP implementation and does not authorize trading.

## Core rule

Transport is an adapter.

The authoritative sequence is:

`request -> configured authority/root registry -> portable artifact -> typed decoder -> VerificationServiceV4 -> optional PersistentTrustStateAuthority CAS -> canonical response`

No caller supplies filesystem paths.

## Operations

- `verify.read_only`
- `verify.advance`
- `authority.status`

`authority.status` returns `STATUS_OK`; it is deliberately not reported as VERIFIED evidence.

## Request identity

Every request has a canonical `request_sha256`.

`verify.advance` also requires an idempotency key.

The idempotency key is hashed before being used as a filesystem storage name.

## Stable result classes

- `STATUS_OK`
- `VERIFIED_USABLE`
- `VERIFIED_UNUSABLE`
- `REJECTED`
- `CAS_CONFLICT`
- `IDEMPOTENCY_CONFLICT`
- `INPUT_ERROR`
- `AUTHORITY_ERROR`

Transport status codes may map onto these classes later but do not replace them.

## Persistent authority

The server resolves logical `authority_id` values through a configured registry.

The API caller cannot choose an arbitrary local path.

Likewise, `trusted_root_set_id` resolves through a configured root-set registry containing explicit 32-byte Ed25519 public roots.

## Idempotency journal

Mutating requests use a durable canonical journal:

`PREPARED -> AUTHORITY_COMMITTED -> FINALIZED`

A no-commit terminal outcome such as REJECTED or CAS_CONFLICT may finalize directly from PREPARED. In that case authority-before and authority-after are identical and no transition receipt is present.

A committed FINALIZED record requires the committed authority snapshot and transition receipt.

## Crash recovery

The PREPARED record binds:

- request SHA;
- authority snapshot;
- artifact/bundle/checkpoint identity;
- observed_at.

API-created authority transitions additionally bind `operation_context_sha256 = request_sha256`.

This prevents a later recovery from confusing an equivalent artifact transition performed by another request or external writer with the API request being recovered.

Recovery rules:

- FINALIZED -> return stored canonical response byte-for-byte;
- PREPARED + no matching committed transition -> resume against the stored authority snapshot;
- PREPARED + matching next authority generation and matching operation context -> reconstruct receipt and finalize without another commit;
- AUTHORITY_COMMITTED -> re-verify from the retained historical before-state and finalize the canonical response;
- incompatible authority movement -> deterministic CAS conflict;
- corrupt journal/history -> authority error and fail closed.

## Concurrency

PersistentTrustStateAuthority CAS remains the mutation authority.

The API never silently retries verification after a CAS conflict against a newer TrustState because that would change the verification context.

## Security boundary

The Verification API core:

- has no web-framework dependency;
- requires no network;
- requires no private signing key;
- accepts no arbitrary filesystem path from callers;
- imports no broker/OMS/strategy/risk/live-routing authority.

Optional HTTP, Unix-socket or embedded adapters may be added only after the transport-neutral contract is qualified.

## Next layer

After Verification API v1 passes qualification/security/coverage gates:

`transport adapter -> SDK contract -> Reference Profiles -> Institutional Test Corpus -> OEM/Embedded Qualification`
