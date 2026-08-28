from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_same_account import (
    BybitDemoAccountIdentityInspector,
    prove_same_bybit_demo_account,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prove Demo read-only and trading credentials belong to the same account using "
            "authenticated GET only."
        )
    )
    parser.add_argument("--git-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument(
        "--output",
        default="artifacts/bybit-demo-same-account-preflight.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        git_sha = _git_sha(args.git_sha)
        readonly_key = _required_env("BYBIT_DEMO_READONLY_API_KEY")
        readonly_secret = _required_env("BYBIT_DEMO_READONLY_API_SECRET")
        trading_key = _required_env("BYBIT_DEMO_TRADING_API_KEY")
        trading_secret = _required_env("BYBIT_DEMO_TRADING_API_SECRET")
        proof = prove_same_bybit_demo_account(
            BybitDemoAccountIdentityInspector(
                api_key=readonly_key,
                api_secret=readonly_secret,
            ),
            BybitDemoAccountIdentityInspector(
                api_key=trading_key,
                api_secret=trading_secret,
            ),
        )
        payload = proof.to_payload() | {"git_sha": git_sha}
        exit_code = 0 if proof.passed else 2
    except Exception as exc:  # noqa: BLE001 - artifact exposes only the failure class.
        payload = _failure(type(exc).__name__, git_sha=args.git_sha)
        exit_code = 2
    _emit(args.output, payload)
    return exit_code


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing:{name}")
    return value


def _git_sha(value: str) -> str:
    normalized = value.strip()
    if len(normalized) != 40 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("Bybit Demo same-account git SHA must be lowercase 40-char hex")
    return normalized


def _failure(error_type: str, *, git_sha: str) -> dict[str, Any]:
    normalized_sha = git_sha.strip()
    return {
        "schema": "BYBIT_DEMO_SAME_ACCOUNT_PREFLIGHT_V1",
        "status": "PREFLIGHT_FAILED",
        "passed": False,
        "reasons": ["DEMO_SAME_ACCOUNT_PREFLIGHT_FAILED"],
        "same_user_id": False,
        "same_parent_uid": False,
        "same_master_scope": False,
        "authenticated_get_only": True,
        "order_write_performed": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
        "git_sha": normalized_sha if len(normalized_sha) == 40 else None,
        "error_type": error_type,
    }


def _emit(path: str, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, target)
    print(text, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
