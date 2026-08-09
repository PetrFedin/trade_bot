# Schema 103 integration

1. Apply `migrations/v103/001_production_campaign_control_plane.sql` through a controlled PostgreSQL migration process.
2. Grant only dedicated scheduler, worker, evidence-uploader and operator roles; PUBLIC retains no privileges.
3. Inject a DB-API compatible connection factory into `PostgresControlPlaneRepositoryV103`; credentials remain outside the package.
4. Register an immutable `ControlPlanePolicyV103` and record its SHA-256 digest.
5. Let one scheduler select due campaigns with `FOR UPDATE SKIP LOCKED`.
6. Claim a lease and retain its generation and fencing token for every subsequent write.
7. Send heartbeats before `heartbeat_ttl` and renew the lease before `lease_ttl`.
8. Execute only allowlisted `GET`/`HEAD` paper-sandbox probes.
9. Open an evidence upload, resume from `next_offset`, verify every chunk and finalize only after the total digest matches.
10. Run retention deletion only after retention expiry and when legal hold is false.
11. Resolve incidents and confirm zero residual broker state before releasing a blocked campaign.
12. Treat readiness as read-only qualification readiness only; it never enables external order routing or live trading.
