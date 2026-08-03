# Schema 104 operator runbook

## Before enabling a worker

- verify policy, source commit, image digest, generation, and key ID;
- confirm claim and attestation signatures;
- confirm exact paper REST base and read-only endpoint scope;
- confirm spool capacity and object-storage target;
- confirm external routing and live trading remain false.

## During execution

- monitor worker heartbeat sequence and fencing token;
- reject any heartbeat regression or identity change;
- do not reuse claim IDs or nonces;
- investigate spool-capacity alarms before accepting new work;
- treat upload checkpoint conflicts as evidence-integrity incidents.

## Crash recovery

- verify the worker event journal and spool manifest;
- do not rerun an interrupted claim automatically;
- finish only an already-started evidence upload from a verified checkpoint;
- place the interrupted claim in DLQ;
- issue a new signed claim after operator review.

## DLQ release

- confirm no external mutation occurred;
- verify claim, spool, journal, and object digest;
- record the operator identity;
- release the DLQ record only after the root cause is resolved.
