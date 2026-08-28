from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_operational_zone_binding import (
    build_bybit_demo_operational_zone_binding,
)
from app.execution.bybit_demo_same_account import BybitDemoAccountIdentityInspector

_EXIT_OK = 0
_EXIT_BLOCKED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Emit a sanitized HMAC binding for the protected Bybit Demo operational DB and, "
            "where already available, the authenticated Demo account."
        )
    )
    parser.add_argument("--producer", required=True)
    parser.add_argument("--git-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--include-demo-account", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/bybit-demo-operational-zone-binding.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inspector = None
        if args.include_demo_account:
            inspector = BybitDemoAccountIdentityInspector(
                api_key=_required_env("BYBIT_DEMO_READONLY_API_KEY"),
                api_secret=_required_env("BYBIT_DEMO_READONLY_API_SECRET"),
            )
        result = build_bybit_demo_operational_zone_binding(
            producer=args.producer,
            git_sha=args.git_sha,
            binding_secret=_required_env("BYBIT_DEMO_ZONE_BINDING_SECRET"),
            database_dsn=_required_env("BYBIT_DEMO_DATABASE_DSN"),
            account_inspector=inspector,
        )
        payload = result.to_payload()
        exit_code = _EXIT_OK
    except Exception as exc:  # noqa: BLE001 - sidecar intentionally exposes only class name.
        payload = _failure_payload(
            producer=args.producer,
            git_sha=args.git_sha,
            error_type=type(exc).__name__,
        )
        exit_code = _EXIT_BLOCKED

    _emit(args.output, payload)
    return exit_code


def _failure_payload(*, producer: str, git_sha: str, error_type: str) -> dict[str, Any]:
    return {
        "schema": "BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V2",
        "status": "BLOCKED",
        "passed": False,
        "producer": producer,
        "git_sha": git_sha if _looks_like_git_sha(git_sha) else None,
        "error_type": error_type,
        "binding_algorithm": "HMAC-SHA256",
        "binding_key_marker_sha256": None,
        "database_binding_present": False,
        "database_binding_sha256": None,
        "logical_database_identity_verified": False,
        "demo_account_binding_present": False,
        "demo_account_binding_sha256": None,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing:{name}")
    return value


def _looks_like_git_sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdef" for char in value)


def _emit(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, path)
    print(text, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
