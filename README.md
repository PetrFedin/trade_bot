# ASTRA 7.34.0 — Schema 104 production worker execution plane

Schema 104 adds a signed, generation-fenced and read-only worker execution plane on top of the Schema 103 campaign control plane.

```text
signed work claim
  -> deployment attestation
  -> replay and fencing checks
  -> read-only Alpaca paper probe
  -> fsync evidence spool
  -> resumable multipart upload
  -> acknowledgement or dead-letter queue
```

Safety boundaries:

- exact Alpaca paper REST base;
- only `account`, `orders`, `positions`, and `clock` GET probes;
- no submit, replace, or cancel operations;
- HMAC-signed claims and worker attestations;
- claim nonce replay protection;
- generation and fencing-token validation;
- append-only hash-chain worker journal;
- bounded local evidence spool;
- resumable multipart upload with part and total SHA-256;
- dead-letter queue with explicit operator release;
- crash recovery enters `RECOVERY_REQUIRED` and never auto-reruns a claim.

```text
external_order_routing_allowed = false
live_trading_allowed = false
```
