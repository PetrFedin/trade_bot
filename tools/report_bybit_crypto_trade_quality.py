from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.strategy.crypto_trade_quality_diagnostics import diagnose_crypto_replay_quality


def build_trade_quality_report(report: dict[str, Any]) -> dict[str, Any]:
    variants: dict[str, Any] = {}
    raw_variants = report.get("variants", {})
    if isinstance(raw_variants, dict):
        for name, variant in raw_variants.items():
            if isinstance(variant, dict):
                variants[name] = diagnose_crypto_replay_quality(variant)

    candidates: dict[str, Any] = {}
    raw_candidates = report.get("strategy_shadow_candidates", {})
    if isinstance(raw_candidates, dict):
        for name, candidate in raw_candidates.items():
            if isinstance(candidate, dict):
                candidates[name] = diagnose_crypto_replay_quality(candidate)

    return {
        "qualification": "BYBIT_CRYPTO_TRADE_QUALITY_REPORTED",
        "source": report.get("source"),
        "archive_dates": report.get("archive_dates"),
        "symbols": report.get("symbols"),
        "variants": variants,
        "strategy_shadow_candidates": candidates,
        "diagnostic_only": True,
        "strategy_selection_allowed": False,
        "threshold_retuning_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report Bybit crypto replay trade quality")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = json.loads(args.input.read_text(encoding="utf-8"))
    quality = build_trade_quality_report(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(quality, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("BYBIT_CRYPTO_TRADE_QUALITY=" + json.dumps(quality, sort_keys=True))


if __name__ == "__main__":
    main()
