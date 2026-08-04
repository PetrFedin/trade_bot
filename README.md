# ASTRA 7.36.0 — Schema 106 production fleet deployment qualification

Schema 106 qualifies the complete deployment process above the Schema 105 worker fleet boundary.

```text
signed deployment manifest
  -> read-only Kubernetes evidence collection
  -> preflight safety gates
  -> isolated canary observation
  -> dual-control signed rollout action
  -> full-rollout verification
  -> certificate renewal drill
  -> disaster-recovery drill
  -> immutable evidence bundle
```

Safety invariants:

- Kubernetes API access is GET-only, HTTPS-only, TLS-verified and redirect-free;
- image and configuration digests must match the signed manifest;
- default-deny network policy and exact egress allowlist are mandatory;
- live broker endpoints, external order routing and live trading remain disabled;
- rollout actions are dual-control, signed, fenced, idempotent and single-attempt;
- canary promotion requires a complete observation window and zero critical failures;
- certificate renewal requires generation increment, bounded overlap and old-certificate revocation;
- disaster recovery runs only in an isolated `drill-*` environment and enforces RPO/RTO;
- all evidence is content-addressed and hash-chain journaled.

```text
external_order_routing_allowed = false
live_trading_allowed = false
kubernetes_mutations_allowed = false
```
