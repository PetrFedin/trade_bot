# ASTRA 7.33.0 — Schema 103 production campaign control plane

Schema 103 adds a production-oriented control plane for repeated **read-only** paper-sandbox qualification. It builds on the Schema 102 soak orchestrator and does not enable trading mutations.

## Control flow

```text
immutable policy
  -> PostgreSQL due-campaign selection
  -> generation-fenced lease
  -> deployment heartbeat
  -> allowlisted read-only probe
  -> resumable evidence upload
  -> SHA-256 final verification
  -> retention / legal hold
  -> next scheduled window or operator-controlled block
```

## Main controls

- one active lease with monotonically increasing fencing token;
- `FOR UPDATE SKIP LOCKED` scheduler boundary;
- stale heartbeat and lease-expiry blocking;
- `GET`/`HEAD` probes only;
- zero broker mutation count;
- contiguous resumable chunks and idempotent replay;
- incident deduplication and escalation;
- explicit zero-residual cleanup proof;
- append-only hash-chain events;
- no automatic release from `BLOCKED` or `QUARANTINED`.

## Qualification commands

```bash
python -m pytest -q tests/test_campaign_control_plane_v103.py tests/test_postgres_control_plane_v103.py tests/test_tools_v103.py
coverage run --branch -m pytest -q tests/test_campaign_control_plane_v103.py tests/test_postgres_control_plane_v103.py tests/test_tools_v103.py
coverage report --include='app/runtime/*v103.py' --fail-under=85
python -m tools.architecture_audit_v103 .
python -m tools.static_audit_v103 .
python -m tools.stress_v103 --iterations 1000 --workers 8
```

```text
external_order_routing_allowed = false
live_trading_allowed = false
```
