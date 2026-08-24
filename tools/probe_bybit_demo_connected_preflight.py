from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_connected_preflight import (
    BybitDemoPreflightAccountClient,
    PostgresBybitDemoOperationalStateReader,
    run_bybit_demo_connected_preflight,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only connected Bybit Demo + PostgreSQL operational preflight."
    )
    parser.add_argument(
        "--output",
        default="artifacts/bybit-demo-connected-preflight.json",
    )
    return parser


def _write(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _failure(error_type: str) -> dict[str, Any]:
    return {
        "schema": "BYBIT_DEMO_CONNECTED_PREFLIGHT_V1",
        "status": "PREFLIGHT_FAILED",
        "passed": False,
        "error_type": error_type,
        "preflight_only": True,
        "trade_actionable": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }


def main() -> int:
    args = _parser().parse_args()
    api_key = os.environ.get("BYBIT_DEMO_API_KEY", "")
    api_secret = os.environ.get("BYBIT_DEMO_API_SECRET", "")
    database_dsn = os.environ.get("BYBIT_DEMO_DATABASE_DSN", "")
    if not api_key or not api_secret or not database_dsn:
        payload = _failure("DEMO_OPERATIONAL_CONFIG_UNAVAILABLE")
        _write(args.output, payload)
        print(json.dumps(payload, sort_keys=True))
        return 2

    try:
        account_client = BybitDemoPreflightAccountClient(
            api_key=api_key,
            api_secret=api_secret,
        )
        database_reader = PostgresBybitDemoOperationalStateReader(database_dsn)
        result = run_bybit_demo_connected_preflight(account_client, database_reader)
        payload = result.to_payload()
        exit_code = 0 if result.passed else 2
    except Exception as exc:  # noqa: BLE001 - output must stay sanitized on any preflight failure.
        payload = _failure(type(exc).__name__)
        exit_code = 2
    _write(args.output, payload)
    print(json.dumps(payload, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
