from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.runtime.paper_broker_roundtrip_v99 import FileRoundTripJournalV99


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ASTRA Schema 99 operator tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify-journal")
    verify.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.command == "verify-journal":
        events = FileRoundTripJournalV99(args.path).load()
        print(json.dumps({
            "status": "PASS",
            "events": len(events),
            "tail_digest": events[-1].digest if events else "0" * 64,
            "external_order_routing_allowed": False,
            "live_trading_allowed": False,
        }, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
