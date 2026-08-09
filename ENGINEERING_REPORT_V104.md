# ASTRA 7.34.0 / Schema 104 Engineering Report

Schema 104 separates the worker execution plane from the Schema 103 campaign control plane. The worker accepts only a signed, scoped and unexpired read-only claim bound to a signed deployment attestation.

## Implemented boundaries

- HMAC key-ring verification for claim and attestation;
- generation and monotonic fencing-token checks;
- one-time claim ID and nonce consumption;
- exact paper endpoint allowlist;
- local fsync evidence spool with bounded files and bytes;
- resumable multipart upload checkpointing;
- object-part and total-object SHA-256 verification;
- append-only worker event journal and dead-letter queue;
- crash replay to `RECOVERY_REQUIRED` without automatic broker re-probe;
- DB-API PostgreSQL repository with transaction rollback and `FOR UPDATE SKIP LOCKED` claim acquisition.

No forecasting, signal, sizing, execution-price, or live-routing logic was added.
