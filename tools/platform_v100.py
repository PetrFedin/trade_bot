from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Sequence

from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperCredentialsV100,
    AlpacaTradeUpdateStreamV100,
)

UTC = timezone.utc


def _credentials_status() -> int:
    credentials = AlpacaPaperCredentialsV100.from_environment()
    print(
        json.dumps(
            {
                "provider": "alpaca-paper",
                "credentials_configured": True,
                "credentials_fingerprint": credentials.fingerprint,
                "rest_base_url": "https://paper-api.alpaca.markets",
                "stream_url": "wss://paper-api.alpaca.markets/stream",
                "external_order_routing_allowed": False,
                "live_trading_allowed": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _verify_stream(path: Path, *, generation: int) -> int:
    credentials = AlpacaPaperCredentialsV100.from_environment()
    stream = AlpacaTradeUpdateStreamV100(
        generation=generation, credentials=credentials
    )
    now = datetime.now(UTC)
    stream.authentication_frame()
    for raw in path.read_bytes().splitlines():
        if not raw.strip():
            continue
        stream.ingest(raw, received_at=now, expected_generation=generation)
    evidence = stream.evidence(captured_at=now)
    print(json.dumps(evidence, default=str, sort_keys=True))
    return 0 if evidence["ready"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="astra-platform-v100")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("credentials-status")
    verify = subparsers.add_parser("stream-verify")
    verify.add_argument("path", type=Path)
    verify.add_argument("--generation", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "credentials-status":
        return _credentials_status()
    if arguments.command == "stream-verify":
        return _verify_stream(arguments.path, generation=arguments.generation)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
