from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_postgres_bootstrap import (
    apply_bybit_demo_postgres_bootstrap,
    verify_bybit_demo_postgres_schema,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify or explicitly bootstrap Bybit Demo PostgreSQL v119 through v123."
    )
    parser.add_argument("--mode", choices=("verify", "apply"), default="verify")
    parser.add_argument("--confirmation", default="")
    parser.add_argument(
        "--output",
        default="artifacts/bybit-demo-postgres-bootstrap.json",
    )
    return parser


def _write(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _failure(error_type: str, *, mode: str) -> dict[str, Any]:
    return {
        "schema": "BYBIT_DEMO_POSTGRES_BOOTSTRAP_V3",
        "status": "BOOTSTRAP_FAILED",
        "passed": False,
        "mode": mode,
        "error_type": error_type,
        "database_identity_exposed": False,
        "bybit_credentials_required": False,
        "bybit_order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }


def main() -> int:
    args = _parser().parse_args()
    dsn = os.environ.get("BYBIT_DEMO_DATABASE_DSN", "")
    if not dsn:
        payload = _failure("DEMO_DATABASE_CONFIG_UNAVAILABLE", mode=args.mode)
        _write(args.output, payload)
        print(json.dumps(payload, sort_keys=True))
        return 2

    try:
        if args.mode == "apply":
            result = apply_bybit_demo_postgres_bootstrap(
                dsn,
                confirmation_phrase=args.confirmation,
            )
        else:
            result = verify_bybit_demo_postgres_schema(dsn)
        payload = result.to_payload()
        payload["mode"] = args.mode
        exit_code = 0 if result.passed else 2
    except Exception as exc:  # noqa: BLE001 - artifact must remain sanitized on any DB error.
        payload = _failure(type(exc).__name__, mode=args.mode)
        exit_code = 2
    _write(args.output, payload)
    print(json.dumps(payload, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
