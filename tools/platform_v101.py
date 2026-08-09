from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import Sequence

from app.runtime.sandbox_qualification_v101 import (
    Approval,
    ApprovalKey,
    EventStore,
    KillSwitchStore,
    Side,
    canonical_json,
)


def _approval_from_json(path: Path) -> Approval:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Approval(
        approval_id=str(raw["approval_id"]),
        operator_id=str(raw["operator_id"]),
        nonce=str(raw["nonce"]),
        generation=int(raw["generation"]),
        account_id=str(raw["account_id"]),
        symbol=str(raw["symbol"]),
        side=Side(str(raw["side"]).upper()),
        maximum_quantity=Decimal(str(raw["maximum_quantity"])),
        maximum_notional=Decimal(str(raw["maximum_notional"])),
        issued_at=datetime.fromisoformat(str(raw["issued_at"])),
        expires_at=datetime.fromisoformat(str(raw["expires_at"])),
        allow_paper_mutations=bool(raw["allow_paper_mutations"]),
        key_fingerprint=str(raw.get("key_fingerprint", "")),
        signature=str(raw.get("signature", "")),
    )


def _approval_document(value: Approval) -> dict[str, object]:
    return {
        **value.signing_document(),
        "key_fingerprint": value.key_fingerprint,
        "signature": value.signature,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="astra-platform-v101")
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify-journal")
    verify.add_argument("path", type=Path)

    status = commands.add_parser("kill-status")
    status.add_argument("path", type=Path)

    seal = commands.add_parser("seal-approval")
    seal.add_argument("input", type=Path)
    seal.add_argument("output", type=Path)

    check = commands.add_parser("verify-approval")
    check.add_argument("input", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "verify-journal":
        store = EventStore(args.path)
        valid = store.verify()
        events = store.load() if valid else ()
        print(canonical_json({
            "schema": 101,
            "status": "PASS" if valid else "FAIL",
            "events": len(events),
            "tail_digest": events[-1].event_digest if events else "0" * 64,
            "external_order_routing_allowed": False,
            "live_trading_allowed": False,
        }))
        return 0 if valid else 2
    if args.command == "kill-status":
        status = KillSwitchStore(args.path).status()
        print(canonical_json({
            "schema": 101,
            "engaged": status.engaged,
            "reason": status.reason,
            "engaged_at": status.engaged_at,
            "generation": status.generation,
            "status_digest": status.status_digest,
            "external_order_routing_allowed": False,
            "live_trading_allowed": False,
        }))
        return 0
    if args.command == "seal-approval":
        approval = _approval_from_json(args.input)
        sealed = approval.seal(ApprovalKey.from_environment())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(_approval_document(sealed), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(canonical_json({
            "schema": 101,
            "approval_id": sealed.approval_id,
            "operator_id": sealed.operator_id,
            "key_fingerprint": sealed.key_fingerprint,
            "output": str(args.output),
            "external_order_routing_allowed": False,
            "live_trading_allowed": False,
        }))
        return 0
    if args.command == "verify-approval":
        approval = _approval_from_json(args.input)
        valid = approval.verify(ApprovalKey.from_environment())
        print(canonical_json({
            "schema": 101,
            "approval_id": approval.approval_id,
            "valid": valid,
            "external_order_routing_allowed": False,
            "live_trading_allowed": False,
        }))
        return 0 if valid else 2
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
