"""Enumerate EVERY daily OHLC stop/target collision before sourcing intraday bars.

The frozen shipped Strategy A is replayed on both six-major and broad-21
synchronized Bybit daily histories. Every preexisting ambiguous closed trade
is emitted with its original PnL and independently reconstructible stop/
target levels. This file has no exchange connections or strategy optimization.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.strategy.position_management import PositionManagementPolicy
from tools.historical_trade_attribution import run_shipped_portfolio

SIX_MAJOR = (
    "BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"
)
BROAD_21 = (
    "ADAUSDT", "APTUSDT", "ARBUSDT", "ATOMUSDT", "AVAXUSDT",
    "BNBUSDT", "BTCUSDT", "DOGEUSDT", "DOTUSDT", "ETHUSDT",
    "FILUSDT", "INJUSDT", "LINKUSDT", "LTCUSDT", "NEARUSDT",
    "OPUSDT", "SOLUSDT", "SUIUSDT", "TONUSDT", "TRXUSDT", "XRPUSDT",
)


def census_for_universe(
    *,
    bars_dir: Path,
    symbols: tuple[str, ...],
    universe_id: str,
    pinned_hourly_dir: Path | None = None,
) -> dict:
    if universe_id not in ("SIX_MAJOR_FULL", "BROAD_21_COMMON"):
        raise ValueError("invalid frozen universe identity")
    frozen = SIX_MAJOR if universe_id == "SIX_MAJOR_FULL" else BROAD_21
    if symbols != frozen:
        raise ValueError("universe must remain exact frozen symbol order")
    result, synchronized, timeline, paths = run_shipped_portfolio(
        bars_dir=bars_dir,
        symbols=symbols,
        opening_cash=Decimal("10000"),
        fee_bps_per_fill=Decimal("8"),
        slippage_bps=Decimal("5"),
    )
    policy = PositionManagementPolicy()
    index_by_symbol = {
        symbol: {bar.timestamp: i for i, bar in enumerate(bars)}
        for symbol, bars in synchronized.items()
    }
    cases: list[dict] = []
    case_ids: set[str] = set()
    for trade in result.closed_trades:
        if not trade.ambiguous_intrabar_exit:
            continue
        symbol = trade.symbol
        lookup = index_by_symbol[symbol]
        if trade.entry_time not in lookup or trade.exit_time not in lookup:
            raise ValueError("trade timestamp absent in synchronized history")
        entry_index = lookup[trade.entry_time]
        exit_index = lookup[trade.exit_time]
        if exit_index < entry_index:
            raise ValueError("trade exits before entry")
        if trade.quantity <= 0:
            raise ValueError("closed trade has invalid quantity")
        # PortfolioLedger capitalizes the entry fee into average_cost.
        average_cost = trade.entry_execution_price + (
            trade.entry_fee / trade.quantity
        )
        hard_stop = average_cost * (
            Decimal("1") - policy.stop_loss_fraction
        )
        take_profit = average_cost * (
            Decimal("1") + policy.take_profit_fraction
        )
        completed_peak = max(
            [trade.entry_reference_price]
            + [
                bar.high
                for bar in synchronized[symbol][entry_index:exit_index]
            ]
        )
        trailing_active = completed_peak >= average_cost * (
            Decimal("1") + policy.trailing_activation_fraction
        )
        trailing = (
            completed_peak * (Decimal("1") - policy.trailing_stop_fraction)
            if trailing_active else None
        )
        protective = max(hard_stop, trailing) if trailing is not None else hard_stop
        exit_bar = synchronized[symbol][exit_index]
        if exit_bar.low > protective or exit_bar.high < take_profit:
            raise ValueError(
                "daily collision classification contradicts frozen exit bar"
            )
        identity = (
            f"{universe_id}:{symbol}:"
            f"{trade.entry_time.isoformat()}:{trade.exit_time.isoformat()}"
        )
        if identity in case_ids:
            raise ValueError("duplicate research ambiguity case")
        case_ids.add(identity)
        day = trade.exit_time.date().isoformat()
        pinned = (
            pinned_hourly_dir / f"{symbol}_{day}" / f"{symbol}_60m.csv"
            if pinned_hourly_dir is not None else None
        )
        cases.append({
            "case_id": identity,
            "universe": universe_id,
            "symbol": symbol,
            "entry_utc": trade.entry_time.isoformat(),
            "exit_utc": trade.exit_time.isoformat(),
            "exit_day": day,
            "entry_execution_price": str(trade.entry_execution_price),
            "entry_fee": str(trade.entry_fee),
            "position_base_quantity": str(trade.quantity),
            "ledger_average_cost": str(average_cost),
            "hard_stop_price": str(hard_stop),
            "take_profit_price": str(take_profit),
            "prior_completed_peak_price": str(completed_peak),
            "trailing_active": trailing_active,
            "trailing_stop_price": None if trailing is None else str(trailing),
            "effective_protective_price": str(protective),
            "daily_high": str(exit_bar.high),
            "daily_low": str(exit_bar.low),
            "daily_conservative_exit_reason": trade.exit_reason.value,
            "original_modelled_net_pnl_usd_before_funding": str(trade.net_pnl),
            "original_holding_bars": trade.holding_bars,
            "prior_pinned_hourly_data_on_same_symbol_day": (
                bool(pinned and pinned.is_file())
            ),
            "hourly_execution_order_not_yet_reconstructed_by_census": True,
        })
    cases.sort(key=lambda row: row["case_id"])
    original_net = sum(
        (Decimal(row["original_modelled_net_pnl_usd_before_funding"])
         for row in cases),
        Decimal("0"),
    )
    if original_net != sum(
        (trade.net_pnl for trade in result.closed_trades
         if trade.ambiguous_intrabar_exit),
        Decimal("0"),
    ):
        raise ValueError("ambiguous trade PnL does not conserve")

    payload: dict[str, object] = {
        "schema_version": "astra-historical-daily-ambiguity-census-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_AMBIGUITY_CENSUS_ONLY",
        "research_only": True,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "strategy_changed": False,
        "intraday_replay_performed": False,
        "funding_attributed_per_trade": False,
        "universe": universe_id,
        "symbols": list(symbols),
        "timeline": {
            "start": timeline[0].isoformat(),
            "end": timeline[-1].isoformat(),
            "bars": len(timeline),
        },
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        },
        "original_portfolio": {
            "opening_cash_usd": str(result.opening_cash),
            "ending_equity_usd": str(result.ending_equity),
            "total_return_pre_funding": str(result.total_return),
            "total_closed_trades": result.closed_trade_count,
            "all_ambiguous_closed_trades": sum(
                trade.ambiguous_intrabar_exit for trade in result.closed_trades
            ),
        },
        "ambiguity_sample": {
            "cases": len(cases),
            "original_net_before_funding_usd": str(original_net),
            "negative_original_cases": sum(
                Decimal(row["original_modelled_net_pnl_usd_before_funding"]) < 0
                for row in cases
            ),
            "positive_original_cases": sum(
                Decimal(row["original_modelled_net_pnl_usd_before_funding"]) > 0
                for row in cases
            ),
            "same_entry_exit_bar_cases": sum(
                row["original_holding_bars"] == 0 for row in cases
            ),
            "pinned_hourly_source_day_coverage": sum(
                row["prior_pinned_hourly_data_on_same_symbol_day"] for row in cases
            ),
            "by_symbol": dict(sorted(Counter(row["symbol"] for row in cases).items())),
            "by_conservative_exit_reason": dict(
                sorted(Counter(
                    row["daily_conservative_exit_reason"] for row in cases
                ).items())
            ),
        },
        "cases": cases,
        "limitations": [
            "This is a full daily OHLC collision census, not an intraday PnL backtest",
            "Only case-level original daily-model PnL, no counterfactual portfolio equity",
            "Prior pinned 1h data is source-day coverage only, not this universe fill proof",
            "Missing 1h/5m provider data must remain visibly NOT_EVIDENCED",
            "Daily high/low ordering, real stop fills, market impact and funding uncertain",
            "No sample-selection optimism: all ambiguous trades retained including losses",
            "No live execution authority, no strategy promotion or fresh OOS claim",
        ],
    }
    payload["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bars-dir", type=Path, required=True)
    p.add_argument("--universe", choices=("six", "broad"), required=True)
    p.add_argument("--hourly-dir", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args(argv)
    symbols = SIX_MAJOR if args.universe == "six" else BROAD_21
    identity = "SIX_MAJOR_FULL" if args.universe == "six" else "BROAD_21_COMMON"
    payload = census_for_universe(
        bars_dir=args.bars_dir,
        symbols=symbols,
        universe_id=identity,
        pinned_hourly_dir=args.hourly_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print("CENSUS", json.dumps({
        "universe": identity,
        "portfolio": payload["original_portfolio"],
        "cases": payload["ambiguity_sample"],
    }, sort_keys=True))
    for case in payload["cases"]:
        print(
            "CASE", case["symbol"], case["entry_utc"],
            "EXIT", case["exit_utc"],
            "NET", case["original_modelled_net_pnl_usd_before_funding"],
            "HOUR_SOURCE", case["prior_pinned_hourly_data_on_same_symbol_day"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
