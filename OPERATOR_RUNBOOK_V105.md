# Schema 105 operator runbook

## Enrollment

- Verify cluster, namespace, service account, zone, image digest and config digest.
- Confirm exactly one active enrollment-signing key.
- Issue a short-lived token with a unique token ID and nonce.
- Persist token/nonce consumption before accepting the worker as active.

## Key rotation

- Add the replacement key as active; existing active key becomes retiring.
- Confirm new enrollment issuance uses the replacement key.
- Keep the retiring key only for validation of unexpired tokens.
- Revoke the retiring key after the maximum enrollment TTL.

## Worker certificate rotation

- Require two distinct operator identities.
- Confirm replacement certificate fingerprint is new and not revoked.
- Increment identity generation.
- Reject heartbeat from the previous certificate immediately.

## Drain

- Set worker to `DRAINING`; do not assign new claims.
- Wait for active claims to reach zero.
- Flush and acknowledge evidence spool.
- Mark worker `STOPPED` only after both conditions are true.
- If the deadline expires, quarantine the worker and open recovery work.

## Containment

- Choose the narrowest sufficient scope: worker, deployment, zone, then fleet.
- Activation is immediate and blocks new claims and scaling.
- Release requires two operators, cleanup confirmation and a SHA-256 evidence digest.
- Do not release while any impacted worker has an active claim or `recovery_required=true`.

## Evidence upload

- Use only an allowlisted HTTPS endpoint.
- Require TLS verification and reject redirects.
- Do not put access keys in logs, object keys or manifests.
- Do not blindly retry multipart mutations.
- Recover ambiguous start through read-only upload listing.
- Recover ambiguous complete through HEAD metadata verification.

## External readiness

Keep the fleet read-only until external Kubernetes, PostgreSQL and S3-compatible qualifications are recorded. Trading mutation and live-routing flags remain false.
