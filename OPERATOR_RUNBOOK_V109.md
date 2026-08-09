# Operator runbook — Schema 109 remote signer attestation

## Safe operating state

Schema 109 is source/CI qualified only. `external_order_routing_allowed` and `live_trading_allowed` remain false. A remote signer may be used only in a controlled qualification environment until every external item in `LIVE_EXECUTION_STATUS_V109.json` is evidenced.

## Before enabling a remote signer

1. Verify the root key fingerprint out-of-band and verify the signed policy snapshot.
2. Confirm the policy endpoint is the exact approved HTTPS origin and the mTLS identity reference resolves to the intended workload certificate.
3. Confirm TLS hostname verification, peer verification and TLS 1.3 minimum are enforced by the supplied `SSLContext`.
4. Install the verified policy into PostgreSQL and confirm no generation-equivocation conflict.
5. Confirm the signing and attestation public keys, hardware-cluster allowlist and firmware-measurement allowlist match provider records.
6. Confirm the current audit checkpoint with the independent provider/audit observer.

## Request handling

- A request must be persisted with its outbox before dispatch.
- `DISPATCH_STARTED` must commit before the network POST.
- There is one POST attempt. Never manually replay a POST whose outcome is unknown.
- Deterministic 4xx rejection becomes `REJECTED`.
- Timeout, transport loss, 202/204, redirects, 408/409/425/429, 5xx and malformed success become `UNCERTAIN`.
- An `UNCERTAIN` request is reconciled only by GET using the original request ID.
- A request past its signed deadline becomes `QUARANTINED` and requires manual review.

## Incident response

For `UNCERTAIN`, stop the affected worker from issuing any replacement signing mutation, preserve the request/outbox/event rows, query the provider by request ID, and compare provider audit sequence, hardware signing counter and audit-chain root with the local checkpoint.

For policy equivocation, counter rollback, attestation failure, unapproved firmware/hardware, or checkpoint CAS rejection, quarantine the signer integration. Do not reset counters or overwrite policy generations. Preserve evidence and rotate/revoke only through the provider's approved ceremony.

## Recovery acceptance

A recovered signature is accepted only after both the payload signature and provider-attestation signature verify, all request/policy bindings match, hardware/firmware are allowlisted, and both monotonic counters advance. Only then may PostgreSQL atomically move the request to `SIGNED` and advance the checkpoint.
