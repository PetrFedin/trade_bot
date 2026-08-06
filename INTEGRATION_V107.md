# Integration guide — Schema 106 to Schema 107

1. Load and verify the Schema 106 `RolloutActionV106` with its legacy action keyring.
2. Read the exact Kubernetes Deployment snapshot.
3. Call `build_execution_intent_from_v106`.
4. Obtain new independent RELEASE and RISK attestations over the returned intent digest.
5. Sign the complete command with the controller key.
6. Call `DeploymentRolloutServiceV107.enqueue`; PostgreSQL becomes the replay source of truth.
7. Workers call `run_once`.
8. Recovery workers call `recover(command_id=...)`; this path never invokes PATCH.

The bridge intentionally ignores the cryptographic sufficiency of the two names embedded in the Schema 106 action. They remain provenance, not independent Schema 107 approvals.

Required deployment annotations before execution:

- `astra.openai.com/config-digest`;
- `astra.openai.com/external-order-routing-allowed=false`;
- `astra.openai.com/live-trading-allowed=false`.

Schema 107 writes only:

- `astra.openai.com/rollout-action-id`;
- `astra.openai.com/rollout-command-digest`;
- `astra.openai.com/rollout-fencing-token`;
- `astra.openai.com/rollout-target-replicas`;
- `spec.replicas`.
