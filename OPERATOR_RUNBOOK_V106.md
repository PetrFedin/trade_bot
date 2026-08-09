# Operator Runbook V106

## Before canary

- Verify release identity and signed manifest expiry.
- Confirm cluster, namespace, service account and deployment UID.
- Confirm exact image/config digests.
- Confirm default-deny ingress and egress.
- Confirm egress allowlist contains only DNS, PostgreSQL, approved S3 and Alpaca paper hosts.
- Confirm PodDisruptionBudget and required zone spread.
- Confirm current PostgreSQL backup freshness.

## During observation

- Record samples monotonically.
- Stop immediately on broker mutation, external routing, live trading, stale heartbeat, crashloop, DLQ or incident-budget violation.
- Do not retry a rollout mutation. Resolve ambiguous outcomes with read-only evidence.

## Promotion or rollback

- Require two distinct operators.
- Verify action signature, state digest, generation, fencing token and idempotency key.
- Claim the action once.
- Record a content-addressed receipt.
- Quarantine the qualification on failed or ambiguous acknowledgement.

## Certificate drill

- Drain active claims.
- Issue generation +1 certificate.
- Enforce bounded overlap.
- Activate the new certificate.
- Revoke the old fingerprint.
- Verify fresh heartbeat from the new identity.

## Disaster recovery drill

- Restore only to `drill-*`.
- Keep external routing and live trading disabled.
- Verify LSN, schema version and integrity digest.
- Enforce RPO and RTO.
- Quarantine all failed restore evidence.
