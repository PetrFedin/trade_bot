from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from app.strategy.crypto_capacity import estimate_crypto_trade_capacity

_DEFAULT_TARGETS = (Decimal("15"), Decimal("20"), Decimal("25"))
_DEFAULT_NOTIONAL_CAPS = (Decimal("2"), Decimal("3"))


def build_capacity_report(
    *,
    opening_equity_usdt: Decimal = Decimal("1000"),
    targets_usd: tuple[Decimal, ...] = _DEFAULT_TARGETS,
    notional_caps: tuple[Decimal, ...] = _DEFAULT_NOTIONAL_CAPS,
    taker_fee_rate: Decimal = Decimal("0.0006"),
    slippage_bps_per_fill: Decimal = Decimal("2"),
    requested_trades_per_day: int = 100,
) -> dict[str, object]:
    scenarios: dict[str, object] = {}
    for cap in notional_caps:
        cap_key = f"{cap.normalize()}X_NOTIONAL"
        target_rows: dict[str, object] = {}
        for target in targets_usd:
            estimate = estimate_crypto_trade_capacity(
                opening_equity_usdt=opening_equity_usdt,
                notional_to_equity=cap,
                target_net_profit_usd=target,
                taker_fee_rate=taker_fee_rate,
                slippage_bps_per_fill=slippage_bps_per_fill,
                requested_trades_per_day=requested_trades_per_day,
            )
            target_rows[f"TARGET_{target.normalize()}_USD"] = {
                "target_net_profit_usd": float(target),
                "notional_usdt": float(estimate.notional_usdt),
                "estimated_round_trip_cost_usdt": float(
                    estimate.estimated_round_trip_cost_usdt
                ),
                "execution_cost_budget_usdt": float(estimate.execution_cost_budget_usdt),
                "maximum_full_cost_round_trips": estimate.maximum_full_cost_round_trips,
                "minimum_gross_profit_usdt": float(estimate.minimum_gross_profit_usdt),
                "minimum_price_move_fraction": float(estimate.minimum_price_move_fraction),
                "requested_trades_per_day": estimate.requested_trades_per_day,
                "requested_frequency_within_cost_budget": (
                    estimate.requested_frequency_within_cost_budget
                ),
                "theoretical_daily_net_target_usdt": float(
                    estimate.theoretical_daily_net_target_usdt or Decimal("0")
                ),
                "live_promotion_allowed": False,
            }
        scenarios[cap_key] = {
            "maximum_notional_to_equity": float(cap),
            "targets": target_rows,
            "live_promotion_allowed": False,
        }
    return {
        "qualification": "CRYPTO_CAPACITY_DIAGNOSTIC",
        "purpose": "COST_AND_TURNOVER_BOUND_ONLY_NOT_A_PROFIT_FORECAST",
        "opening_equity_usdt": float(opening_equity_usdt),
        "taker_fee_rate": float(taker_fee_rate),
        "slippage_bps_per_fill": float(slippage_bps_per_fill),
        "requested_trades_per_day": requested_trades_per_day,
        "strategy_promotion_allowed": False,
        "demo_order_writes_allowed": False,
        "live_promotion_allowed": False,
        "scenarios": scenarios,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate crypto target and turnover capacity before alpha assumptions"
    )
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--targets", default="15,20,25")
    parser.add_argument("--notional-caps", default="2,3")
    parser.add_argument("--taker-fee-rate", default="0.0006")
    parser.add_argument("--slippage-bps-per-fill", default="2")
    parser.add_argument("--requested-trades-per-day", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    targets = tuple(Decimal(value.strip()) for value in args.targets.split(",") if value.strip())
    notional_caps = tuple(
        Decimal(value.strip()) for value in args.notional_caps.split(",") if value.strip()
    )
    report = build_capacity_report(
        opening_equity_usdt=Decimal(args.opening_equity),
        targets_usd=targets,
        notional_caps=notional_caps,
        taker_fee_rate=Decimal(args.taker_fee_rate),
        slippage_bps_per_fill=Decimal(args.slippage_bps_per_fill),
        requested_trades_per_day=args.requested_trades_per_day,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("CRYPTO_CAPACITY=" + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
