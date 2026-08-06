# Integration guide — Schema 108

## Required external components

- an Ed25519-capable KMS or HSM signing provider for each signing role;
- an offline or separately administered root key for keyring snapshots;
- trusted root public keys supplied through workload identity/configuration;
- PostgreSQL with the V108 migration applied;
- the existing Schema 107 policy, keyrings and command verification path.

## Provider contract

A provider implements only:

- `key_id`;
- `backend` (`KMS` or `HSM`);
- `generation`;
- `public_key_bytes()`;
- `sign(payload)`.

No export method for a private key exists in the protocol. Production adapters should translate the canonical payload bytes directly into the vendor's remote sign operation and return the raw 64-byte Ed25519 signature.

## Admission order

1. Load the previously accepted keyring generation from durable state.
2. Verify the root-signed incoming keyring snapshot.
3. Run the complete Schema 107 command verification against the exact V107 policy.
4. Verify that the V108 bundle binds the V107 command digest, policy digest and predecessor release identity.
5. Verify RELEASE, RISK and CONTROLLER envelopes and separation of duties.
6. Reserve bundle ID, signature IDs and nonces atomically in PostgreSQL.
7. Only then enqueue or execute through the bounded Schema 107 actuator.

A failed durable reservation must block execution even if all signatures are otherwise valid.

## Receipt order

1. Verify the Schema 107 receipt structure and digest.
2. Verify the EXECUTOR Ed25519 envelope.
3. Confirm binding to the command digest and authorization-bundle digest.
4. Reserve the receipt signature ID and nonce before accepting the receipt as durable evidence.

## Rotation

Publish a root-signed snapshot with generation `N+1`. Never overwrite generation `N` in place. Revocation is represented by `revoked_at` in a newer root-signed snapshot. A stale or same-generation snapshot is rejected.

### Durable receipt reservation

After cryptographic verification, call `PostgreSQLSigningRepositoryV108.reserve_receipt_authorization`. Do not treat the receipt as durable evidence until this transaction commits. It atomically consumes the executor signature ID/nonce, binds the receipt to the exact `(authorization_bundle_digest, command_digest)` pair, stores the public receipt evidence and appends `RECEIPT_AUTHORIZATION_RESERVED`.
