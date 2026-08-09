# Schema 103 operator runbook

## Before enabling a campaign

- verify the package/version and policy digest;
- verify the PostgreSQL migration and role grants;
- confirm the campaign window, lease TTL, heartbeat TTL, probe timeout and retention period;
- confirm the allowlist contains paper/read-only hosts only;
- confirm `external_order_routing_allowed=false` and `live_trading_allowed=false`;
- confirm no unresolved critical incidents exist.

## Scheduler and lease

- process due campaigns in deterministic order;
- use `FOR UPDATE SKIP LOCKED` to avoid duplicate workers;
- persist the returned fencing token;
- reject every operation whose generation, owner or fencing token differs;
- never reuse a token after lease loss or operator release.

## Worker heartbeat

- heartbeat well before `heartbeat_ttl`;
- include deployment ID and immutable build identity;
- treat timestamp regression as integrity corruption;
- block the campaign after a stale heartbeat or expired lease;
- do not automatically resume an interrupted probe or upload.

## Read-only probe

- permit only `GET` or `HEAD`;
- use only allowlisted paper endpoints;
- prohibit submit, replace, cancel and any mutation flag;
- verify account, open-order and authenticated-stream evidence;
- block failed/error outcomes and quarantine identity/time/generation mismatches.

## Evidence upload

- upload contiguous chunks from the current `next_offset`;
- accept replay only when the stored bytes and chunk digest are identical;
- quarantine a conflicting replay;
- finalize only after byte count and final SHA-256 match;
- preserve manifest digests and legal-hold state.

## Incident response

- acknowledge incidents without reducing their severity;
- investigate critical incidents before lease acquisition;
- confirm zero residual orders and zero residual position quantity;
- record a cleanup evidence digest;
- resolve critical incidents;
- release `BLOCKED` only through explicit operator action, which increments generation.

## Retention

- respect retention deadlines and legal holds;
- verify the manifest before deleting retained bytes;
- preserve append-only tombstone/event evidence;
- quarantine any manifest mismatch.
