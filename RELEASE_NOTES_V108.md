# Release notes — 7.38.0

- Adds an Ed25519 signing-provider boundary designed for external KMS/HSM implementations.
- Adds root-signed, strictly monotonic keyring snapshots with validity and revocation checks.
- Adds purpose/domain/payload/generation/time/nonce binding for every signature.
- Requires independent RELEASE, RISK and CONTROLLER owners for rollout admission.
- Adds a separate EXECUTOR signature over receipt, command and authorization-bundle digests.
- Adds a V107 compatibility bridge that makes the existing HMAC command necessary but insufficient.
- Adds durable PostgreSQL storage for keyring snapshots, bundle evidence, atomic replay reservations and append-only events.
- Adds real PostgreSQL migration apply/re-apply validation and schema-contract checks.
- Adds concurrent replay-ledger stress and high branch-aware runtime coverage.

This release contains no production private key, KMS credential, order-routing permission or live-trading authorization.
