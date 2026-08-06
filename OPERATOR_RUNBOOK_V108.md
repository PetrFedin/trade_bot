# Operator runbook — Schema 108 signing authority

## Before deployment

- confirm every production signer is backed by the intended KMS/HSM and uses Ed25519;
- compare provider public keys with the root-signed descriptor set;
- verify that RELEASE, RISK, CONTROLLER and EXECUTOR owners are distinct as required;
- record the trusted root key IDs out of band;
- apply the canonical migration with `ON_ERROR_STOP=1`;
- run V107 and V108 release verification, architecture audit, static audit and stress.

## Keyring acceptance

Reject and quarantine the snapshot when generation is stale, a root signature fails, a required purpose is absent, a key is expired/revoked, or time is outside the allowed interval. Do not fall back to the Schema 107 HMAC-only path.

## Signing failure

Treat provider timeout, denial, malformed signature length or locally unverifiable signature as a failed authorization. Do not retry under another role key and do not substitute a software key.

## Replay or conflict

A duplicate bundle ID, command digest, signature ID or nonce is a security event. Keep the transaction rolled back, emit an alert, and investigate whether the request was retried, replayed or forked.

## Rotation and revocation

- issue a strictly newer root-signed snapshot;
- retain prior public evidence for audit;
- verify the new snapshot before durable persistence;
- do not delete replay records during routine rotation;
- use a separate incident process for emergency root replacement.

## Rollback

Rolling application code back to Schema 107 removes the asymmetric enforcement layer and must not be treated as a safe automatic fallback. Production mutation authorization remains false until an explicit security decision approves the resulting trust boundary.
