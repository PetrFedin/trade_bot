from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_trading_credential_preflight import (
    BybitDemoTradingCredentialReadOnlyInspector,
    run_bybit_demo_trading_credential_preflight,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "GET-only Bybit Demo trading-credential readiness preflight. "
            "No order mutation is exposed or attempted."
        )
    )
    parser.add_argument(
        "--output",
        default="artifacts/bybit-demo-trading-credential-preflight.json",
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
        "schema": "BYBIT_DEMO_TRADING_CREDENTIAL_PREFLIGHT_V1",
        "status": "PREFLIGHT_FAILED",
        "passed": False,
        "error_type": error_type,
        "authenticated_get_only": True,
        "order_write_performed": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }


def main() -> int:
    args = _parser().parse_args()
    trading_api_key = os.environ.get("BYBIT_DEMO_TRADING_API_KEY", "")
    trading_api_secret = os.environ.get("BYBIT_DEMO_TRADING_API_SECRET", "")
    demo_readonly_sha = os.environ.get("BYBIT_DEMO_READONLY_API_KEY_SHA256", "")
    mainnet_readonly_sha = os.environ.get("BYBIT_MAINNET_READONLY_API_KEY_SHA256", "")
    if not all(
        (
            trading_api_key,
            trading_api_secret,
            demo_readonly_sha,
            mainnet_readonly_sha,
        )
    ):
        payload = _failure("DEMO_TRADING_CREDENTIAL_CONFIG_UNAVAILABLE")
        _write(args.output, payload)
        print(json.dumps(payload, sort_keys=True))
        return 2

    try:
        inspector = BybitDemoTradingCredentialReadOnlyInspector(
            api_key=trading_api_key,
            api_secret=trading_api_secret,
        )
        result = run_bybit_demo_trading_credential_preflight(
            inspector,
            demo_readonly_api_key_sha256=demo_readonly_sha,
            mainnet_readonly_api_key_sha256=mainnet_readonly_sha,
        )
        payload = result.to_payload()
        exit_code = 0 if result.passed else 2
    except Exception as exc:  # noqa: BLE001 - never serialize credential-bearing errors.
        payload = _failure(type(exc).__name__)
        exit_code = 2

    _write(args.output, payload)
    print(json.dumps(payload, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
