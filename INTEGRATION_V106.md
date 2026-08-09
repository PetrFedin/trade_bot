# Integration V106

The integration boundary accepts external evidence only through narrow adapters:

- Kubernetes: GET-only API reads for Deployment, Pods, PodDisruptionBudget and NetworkPolicy.
- PostgreSQL: explicit transactions for replay guards, evidence append, rollout-action claim and acknowledgement.
- Object storage: inherited Schema 105 S3-compatible evidence delivery.
- Broker: inherited read-only paper endpoint; this schema adds no broker mutation operation.

Production enablement requires separate credentials and environment-specific qualification. CI uses deterministic transports and fake DB-API connections; it does not claim an external Kubernetes, PostgreSQL, S3, certificate-authority or restore-session result.
