from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_trade_plan_feasibility import diagnose_crypto_trade_plan_feasibility

_DEFAULT_EQUITIES = "1000,950,900,850,844.8776608971926,800,750,700"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report fixed-target crypto trade-plan feasibility across equity levels"
    )
    parser.add_argument("--equities", default=_DEFAULT_EQUITIES)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    equities = tuple(
        Decimal(value.strip())
        for value in args.equities.split(",")
        if value.strip()
    )
    report = diagnose_crypto_trade_plan_feasibility(
        equities,
        config=CryptoPerpStrategyConfig(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("CRYPTO_TRADE_PLAN_FEASIBILITY=" + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
