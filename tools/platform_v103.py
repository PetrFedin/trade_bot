from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Sequence

from app.runtime.campaign_control_plane_v103 import ControlPlanePolicyV103, InMemoryControlPlaneStoreV103
from tools.architecture_audit_v103 import audit as architecture_audit
from tools.static_audit_v103 import audit as static_audit

UTC = timezone.utc


def truth_status() -> dict[str, object]:
    return {
        "schema": 103,
        "version": "7.33.0",
        "production_control_plane_implemented": True,
        "postgresql_adapter_implemented": True,
        "read_only_probe_boundary_implemented": True,
        "resumable_evidence_upload_implemented": True,
        "incident_escalation_implemented": True,
        "external_postgresql_cluster_verified": False,
        "external_alpaca_read_only_campaign_verified": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
    }


def demo_readiness() -> dict[str, object]:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    store = InMemoryControlPlaneStoreV103()
    store.register_campaign(
        ControlPlanePolicyV103(
            campaign_id="demo-v103",
            generation=1,
            starts_at=now,
            ends_at=now + timedelta(days=1),
            run_interval=timedelta(hours=1),
            lease_ttl=timedelta(minutes=5),
            heartbeat_ttl=timedelta(minutes=2),
            probe_timeout=timedelta(seconds=30),
            evidence_chunk_bytes=256,
            evidence_retention=timedelta(days=7),
        ),
        now,
    )
    return store.readiness("demo-v103", now)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("truth-status")
    sub.add_parser("demo-readiness")
    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    if args.command == "truth-status":
        result = truth_status()
    elif args.command == "demo-readiness":
        result = demo_readiness()
    else:
        root = args.root.resolve()
        result = {
            "architecture": architecture_audit(root),
            "static": static_audit(root),
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.command == "audit":
        return 0 if all(item["status"] == "PASS" for item in result.values()) else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
