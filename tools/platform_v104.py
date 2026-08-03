from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Sequence
from app.runtime.worker_execution_plane_v104 import DeadLetterQueueV104, EvidenceSpoolV104, WorkerEventJournalV104


def main(argv: Sequence[str] | None = None) -> int:
    parser=argparse.ArgumentParser(prog="astra-platform-v104")
    sub=parser.add_subparsers(dest="command", required=True)
    for name in ("verify-journal","verify-spool","verify-dlq"):
        item=sub.add_parser(name); item.add_argument("path", type=Path)
    args=parser.parse_args(argv)
    if args.command == "verify-journal": result={"events":len(WorkerEventJournalV104(args.path).verify())}
    elif args.command == "verify-spool": result={"records":len(EvidenceSpoolV104(args.path,10**9,10**15).verify())}
    else: result={"records":len(DeadLetterQueueV104(args.path).verify())}
    print(json.dumps({"schema":104,"status":"PASS",**result}, sort_keys=True))
    return 0

if __name__ == "__main__": raise SystemExit(main())
