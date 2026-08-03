# Schema 104 integration

1. Seal `WorkerPolicyV104` and publish its digest in Schema 103.
2. Attest each deployment with `WorkerAttestationV104`.
3. Sign each `SignedWorkClaimV104` with campaign/run/generation/fencing scope.
4. Claim work through `PostgresWorkerRepositoryV104.claim_next`.
5. Verify and consume claim nonce before any external request.
6. Run only allowlisted paper GET probes.
7. Persist evidence in `EvidenceSpoolV104` before upload.
8. Resume multipart delivery from the local checkpoint after network interruption.
9. Acknowledge spool records only after total object digest verification.
10. Send exhausted or conflicting work to `DeadLetterQueueV104` and require operator release.
