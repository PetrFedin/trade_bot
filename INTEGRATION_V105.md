# Schema 105 integration

Schema 105 is stacked on Schema 104.

Control plane integration order:

1. Control plane creates a worker-fleet task.
2. PostgreSQL repository claims it with `FOR UPDATE SKIP LOCKED`, generation and fencing token increments.
3. Enrollment authority validates deployment attestation and issues a one-time token.
4. Fleet registry enrolls or rotates the worker identity.
5. Worker heartbeat and claim admission are fenced by identity generation, certificate fingerprint and containment state.
6. Autoscaler emits immutable chained decisions but does not directly mutate infrastructure.
7. Evidence adapter uploads read-only qualification evidence to an allowlisted S3-compatible endpoint.
8. Containment or drain blocks new work before worker termination.

The infrastructure actuator must separately validate the decision digest, policy digest and current containment epoch before applying replica changes.
