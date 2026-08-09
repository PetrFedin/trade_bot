from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from typing import Sequence

from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperCredentialsV100,
    AlpacaTradeUpdateStreamV100,
)

UTC = timezone.utc


def _order(index: int) -> dict[str, object]:
    occurred = datetime(2026, 8, 3, 12, 0, tzinfo=UTC) + timedelta(microseconds=index)
    return {
        "id": f"broker-{index}",
        "client_order_id": f"astra-v100-{index}",
        "symbol": "AAPL",
        "side": "buy",
        "status": "new",
        "qty": "1",
        "filled_qty": "0",
        "submitted_at": occurred.isoformat(),
        "updated_at": occurred.isoformat(),
        "limit_price": "10",
    }


def run(*, iterations: int, workers: int) -> dict[str, object]:
    if iterations < 1 or workers < 1:
        raise ValueError("iterations and workers must be positive")
    credentials = AlpacaPaperCredentialsV100(key_id="stress-key", secret_key="stress-secret")
    stream = AlpacaTradeUpdateStreamV100(generation=1, credentials=credentials)
    base = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    stream.authentication_frame()
    stream.ingest(
        json.dumps({"stream": "authorization", "data": {"status": "authorized"}}),
        received_at=base,
        expected_generation=1,
    )
    stream.ingest(
        json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}}),
        received_at=base,
        expected_generation=1,
    )

    def submit(index: int) -> None:
        frame = {"stream": "trade_updates", "data": {"event": "new", "order": _order(index)}}
        stream.ingest(
            json.dumps(frame, sort_keys=True),
            received_at=base + timedelta(microseconds=index),
            expected_generation=1,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        tuple(executor.map(submit, range(iterations)))

    evidence = stream.evidence(captured_at=base + timedelta(seconds=1))
    return {
        "iterations": iterations,
        "workers": workers,
        "accepted_updates": evidence["accepted_updates"],
        "duplicate_updates": evidence["duplicate_updates"],
        "ready": evidence["ready"],
        "external_order_routing_allowed": evidence["external_order_routing_allowed"],
        "live_trading_allowed": evidence["live_trading_allowed"],
        "digest": evidence["digest"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    report = run(iterations=args.iterations, workers=args.workers)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ready"] and report["accepted_updates"] == args.iterations else 1


if __name__ == "__main__":
    raise SystemExit(main())
