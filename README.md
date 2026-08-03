# ASTRA 7.35.0 — Schema 105 production worker fleet operations

Schema 105 adds fleet-level safety and operations above the signed Schema 104 worker execution plane.

```text
Kubernetes-style deployment attestation
  -> signed one-time worker enrollment
  -> key/certificate rotation and revocation
  -> heartbeat and identity generation fencing
  -> controlled autoscaling
  -> graceful drain or quarantine
  -> fleet/zone/deployment/worker containment
  -> S3-compatible evidence delivery
  -> PostgreSQL append-only operational record
```

Safety boundaries:

- one active enrollment-signing key, with retiring-key verification and explicit revocation;
- replay-protected enrollment tokens and nonces;
- cluster, namespace, service-account, zone, image and configuration attestation;
- controlled scale-up/scale-down steps, cooldowns and stabilization windows;
- no scale changes during containment, dependency failure or incident-budget exhaustion;
- drain rejects new claims and requires zero active claims plus flushed evidence;
- drain timeout enters quarantine and requires recovery;
- containment release requires dual control and cleanup evidence;
- evidence uploads use HTTPS-only allowlisted S3-compatible endpoints;
- TLS verification enabled and redirects disabled;
- mutation calls are never blindly retried;
- ambiguous multipart mutations recover through read-only listing or HEAD verification;
- part and total SHA-256 verification;
- PostgreSQL task claiming uses `FOR UPDATE SKIP LOCKED` and monotonic fencing.

```text
external_order_routing_allowed = false
live_trading_allowed = false
```
