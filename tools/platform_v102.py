from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from app.runtime.sandbox_soak_orchestrator_v102 import (
    FileCampaignEventStoreV102,
    FileEvidenceArchiveV102,
    FileLeaseStoreV102,
    SoakCorruption,
)


def _print(document: dict[str, object]) -> None:
    print(json.dumps(document, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-platform-v102")
    subparsers = parser.add_subparsers(dest="command", required=True)

    journal = subparsers.add_parser("verify-journal")
    journal.add_argument("path", type=Path)

    archive = subparsers.add_parser("verify-archive")
    archive.add_argument("path", type=Path)

    lease = subparsers.add_parser("verify-lease")
    lease.add_argument("path", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "verify-journal":
            events = FileCampaignEventStoreV102(args.path).load()
            _print({
                "status": "PASS",
                "events": len(events),
                "tail_digest": events[-1].event_digest if events else "0" * 64,
                "external_order_routing_allowed": False,
                "live_trading_allowed": False,
            })
            return 0
        if args.command == "verify-archive":
            records = FileEvidenceArchiveV102(args.path).load_manifest()
            _print({
                "status": "PASS",
                "records": len(records),
                "tail_digest": records[-1].record_digest if records else "0" * 64,
                "external_order_routing_allowed": False,
                "live_trading_allowed": False,
            })
            return 0
        record = FileLeaseStoreV102(args.path).load()
        _print({
            "status": "PASS",
            "present": record is not None,
            "owner_id": "" if record is None else record.owner_id,
            "generation": 0 if record is None else record.generation,
            "fencing_token": 0 if record is None else record.fencing_token,
            "released": True if record is None else record.released,
            "external_order_routing_allowed": False,
            "live_trading_allowed": False,
        })
        return 0
    except (SoakCorruption, ValueError, OSError) as exc:
        _print({
            "status": "FAIL",
            "error": type(exc).__name__,
            "external_order_routing_allowed": False,
            "live_trading_allowed": False,
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
