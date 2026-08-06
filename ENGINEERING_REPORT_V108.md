# Engineering report — Schema 108 asymmetric signing authority

## Decision

Schema 108 removes symmetric HMAC possession from the production authorization trust boundary. Schema 107 remains fully verified as a predecessor compatibility layer, but a command cannot pass the V108 bridge unless it also carries independent Ed25519 signatures from RELEASE, RISK and CONTROLLER identities described by a root-signed, monotonic keyring snapshot.

## Threat model addressed

In Schema 107, a process that verifies HMAC signatures necessarily possesses the same shared secret that can create them. Compromise of a verifier therefore risks becoming signing authority. Schema 108 separates signing and verification:

- production signers are represented only by the `Ed25519SigningProviderV108` protocol;
- the runtime stores public keys, signatures and provider metadata, never private-key bytes;
- provider backends are restricted to KMS or HSM designations;
- every signature binds purpose, domain, payload digest, signer key/generation, keyring generation, validity interval, signature ID and nonce;
- RELEASE, RISK and CONTROLLER must resolve to three distinct owners;
- EXECUTOR signs the receipt under a separate purpose and identity.

## Keyring model

`RootSignedKeyringSnapshotV108` is signed by an externally trusted Ed25519 root. Acceptance is fail-closed when:

- the generation is not strictly greater than the previously accepted generation;
- the root is unknown or the root signature is invalid;
- the snapshot is not yet valid or expired;
- a required purpose lacks an active, non-revoked key;
- a descriptor has an invalid public key, interval or generation.

The verified keyring is exposed as an immutable mapping.

## Rollout binding

The authorization payload binds:

1. the complete Schema 107 command digest;
2. the Schema 107 execution-policy digest;
3. the predecessor release-identity digest.

The bridge first executes the complete Schema 107 verification and then verifies the V108 root, keyring and three-party authorization. HMAC alone is therefore insufficient after the bridge is installed.

## Receipt binding

The executor signature binds the Schema 107 receipt digest, command digest and authorization-bundle digest. This prevents a valid receipt signature from being transplanted onto another command or authorization decision.

## Durable state

The PostgreSQL repository persists only public authorization evidence:

- the latest monotonic root-signed keyring snapshot;
- authorization bundles;
- signature IDs and nonces in a unique replay table;
- append-only signing events.

Keyring updates use a conditional upsert that accepts only a strictly newer generation. Bundle insertion and all three replay reservations share one transaction.

## Explicit non-goals

This release does not provide a software private-key store, an embedded KMS SDK, production credentials, production mutation authorization, order routing or live trading.

## Durable receipt evidence

The verified EXECUTOR envelope is reserved in PostgreSQL in the same transaction as its receipt authorization and audit event. The receipt row has a foreign-key binding to both the exact authorization-bundle digest and command digest, and to the consumed executor signature ID. A missing authorization, replayed nonce, duplicate receipt or event-write failure rolls the complete reservation back.
