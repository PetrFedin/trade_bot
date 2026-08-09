# Release notes — 7.39.0 / Schema 109

Schema 109 adds a fail-closed remote KMS/HSM signing adapter compatible with the Schema 108 Ed25519 provider interface. It introduces root-signed remote-signer policy snapshots, exact HTTPS/mTLS/TLS1.3 transport constraints, transactional request/outbox dispatch markers, dual payload/attestation verification, monotonic provider audit and hardware counters, PostgreSQL compare-and-set checkpoints, GET-only ambiguity recovery and quarantine deadlines.

The release deliberately does **not** enable live trading, external order routing, automatic signing POST retries, or application-side private-key persistence. Production signer identity, certificates, hardware/firmware, audit observer and operational ceremonies remain external qualification requirements.
