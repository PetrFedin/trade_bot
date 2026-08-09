from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from typing import Sequence

from app.runtime.sandbox_qualification_v101 import (
    AccountSnapshot, Approval, ApprovalKey, EventStore, KillSwitchStore,
    OrderSnapshot, OrderStatus, Plan, Policy, QualificationService, Side,
    StreamEvidence,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class Gateway:
    paper_only = True
    writes_enabled = True
    credential_fingerprint = "a" * 16
    rest_endpoint = QualificationService.PAPER_REST
    stream_endpoint = QualificationService.PAPER_STREAM

    def __init__(self, client_order_id: str) -> None:
        self.client_order_id = client_order_id
        self.current: OrderSnapshot | None = None

    def get_account(self):
        return AccountSnapshot("acct-1", "ACTIVE", "USD", Decimal("1000"))

    def list_open_orders(self):
        return ()

    def _order(self, status, price):
        self.current = OrderSnapshot(
            self.client_order_id, "broker-1", "AAPL", Side.BUY,
            Decimal("1"), Decimal(price), status, Decimal("0"), NOW,
        )
        return self.current

    def submit_limit_order(self, **kwargs):
        return self._order(OrderStatus.ACKNOWLEDGED, "10")

    def replace_limit_order(self, **kwargs):
        return self._order(OrderStatus.REPLACED, "11")

    def cancel_order(self, **kwargs):
        return self._order(OrderStatus.CANCELLED, "11")

    def get_order_by_client_order_id(self, client_order_id):
        return self.current


def run_one(index: int) -> bool:
    client_order_id = f"astra-stress-{index}"
    with tempfile.TemporaryDirectory(prefix="astra101-") as directory:
        root = Path(directory)
        selected_plan = Plan(
            f"qual-{index}", index + 1, "acct-1", client_order_id,
            "AAPL", Side.BUY, Decimal("1"), Decimal("10"), Decimal("11"),
            NOW, NOW + timedelta(minutes=10), f"approval-{index}",
        ).seal()
        key = ApprovalKey("x" * 40)
        selected_approval = Approval(
            f"approval-{index}", "operator", f"nonce-{index:016d}", index + 1,
            "acct-1", "AAPL", Side.BUY, Decimal("1"), Decimal("100"),
            NOW, NOW + timedelta(minutes=5), True,
        ).seal(key)
        service = QualificationService(
            gateway=Gateway(client_order_id), plan=selected_plan,
            approval_key=key, event_store=EventStore(root / "events.jsonl"),
            kill_switch=KillSwitchStore(root / "kill.json"),
            policy=Policy(allowed_symbols=frozenset({"AAPL"})), sleeper=lambda _: None,
        )
        evidence = StreamEvidence(
            NOW, index + 1, True, True, "a" * 16,
            QualificationService.PAPER_REST, QualificationService.PAPER_STREAM,
        )
        service.probe(now=NOW, expected_generation=index + 1, stream=evidence)
        service.arm(approval=selected_approval, now=NOW + timedelta(seconds=1), expected_generation=index + 1)
        result = service.execute(now=NOW + timedelta(seconds=2), expected_generation=index + 1)
        return result.success and service.event_store.verify() and not result.kill_switch_engaged


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    if args.iterations < 1 or args.workers < 1:
        parser.error("iterations and workers must be positive")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(executor.map(run_one, range(args.iterations)))
    failures = results.count(False)
    print(json.dumps({
        "schema": 101,
        "status": "PASS" if failures == 0 else "FAIL",
        "iterations": args.iterations,
        "workers": args.workers,
        "successes": results.count(True),
        "failures": failures,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
    }, sort_keys=True))
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
