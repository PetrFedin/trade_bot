# ASTRA 7.37.0 — Schema 107 production rollout actuator

Schema 107 converts a verified Schema 106 rollout decision into a bounded Kubernetes Deployment scale action.

```text
verified Schema 106 action
  -> unsigned Schema 107 execution intent
  -> independent RELEASE approval
  -> independent RISK approval
  -> controller signature
  -> durable replay reservation + outbox
  -> strict Kubernetes GET preflight
  -> durable monotonic fencing + mutation marker
  -> exactly one JSON PATCH attempt
  -> exact full-readiness verification
  -> signed receipt
  -> GET-only reconciliation after ambiguity or crash
```

Safety invariants:

- the Schema 106 approver names are **not** treated as two cryptographic approvals;
- RELEASE and RISK approvals use distinct identities, keys, approval IDs and nonces;
- the command is revalidated immediately before the durable mutation marker;
- PostgreSQL accepts a fencing token only when it is strictly newer for the deployment UID;
- Kubernetes mutation is limited to one JSON Patch of Deployment annotations and `spec.replicas`;
- the patch tests deployment UID, `resourceVersion`, generation and current replicas;
- any timeout, 5xx or malformed success response is ambiguous and cannot trigger another PATCH;
- recovery reads Kubernetes state only;
- success requires desired, ready and available replicas to equal the signed target;
- image/configuration identity must remain pinned;
- external order routing and live trading remain disabled.

```text
production_kubernetes_mutation_authorized = false
external_order_routing_allowed = false
live_trading_allowed = false
```
