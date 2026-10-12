"""Conserve hourly stop/target ordering for four frozen Strategy A trade examples.

Research-only. Replays the UNCHANGED portfolio on inspected daily history, then
inspects separately retrieved hourly Bybit candles for the daily exit bar.
Hourly OHLC resolves order only when trigger hours differ. No order-book fill,
funding settlement, downstream portfolio path or live execution is inferred.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.strategy.position_management import PositionManagementPolicy
from tools.historical_trade_attribution import run_shipped_portfolio
from tools.intraday_evidence_admission import validate_intraday_fixture


def _hour_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _reconcile_daily(rows: list[dict[str, str]], daily_bar) -> None:
    if len(rows) != 24:
        raise ValueError("hourly case must cover one entire UTC daily bar")
    derived = (
        Decimal(rows[0]["open"]),
        max(Decimal(row["high"]) for row in rows),
        min(Decimal(row["low"]) for row in rows),
        Decimal(rows[-1]["close"]),
        sum((Decimal(row["volume"]) for row in rows), Decimal("0")),
    )
    expected = (
        daily_bar.open, daily_bar.high, daily_bar.low, daily_bar.close,
        daily_bar.volume,
    )
    if derived != expected:
        raise ValueError("hourly case does not reconcile to frozen daily OHLCV")


def _trigger_order(
    rows: list[dict[str, str]],
    *,
    protective: Decimal,
    take_profit: Decimal,
) -> dict[str, object]:
    first_protective: int | None = None
    first_target: int | None = None
    for index, row in enumerate(rows):
        opening = Decimal(row["open"])
        low = Decimal(row["low"])
        high = Decimal(row["high"])
        if first_protective is None and (opening <= protective or low <= protective):
            first_protective = index
        if first_target is None and (opening >= take_profit or high >= take_profit):
            first_target = index

    if first_protective is None and first_target is None:
        ordering = "NO_TRIGGER_IN_HOURLY"
    elif first_protective == first_target:
        ordering = "UNRESOLVED_SAME_HOUR"
    elif first_protective is not None and (
        first_target is None or first_protective < first_target
    ):
        ordering = "PROTECTIVE_FIRST_HOURLY"
    else:
        ordering = "TAKE_PROFIT_FIRST_HOURLY"

    return {
        "ordering": ordering,
        "first_protective_hour": first_protective,
        "first_target_hour": first_target,
        "first_protective_timestamp": (
            None if first_protective is None else rows[first_protective]["timestamp"]
        ),
        "first_target_timestamp": (
            None if first_target is None else rows[first_target]["timestamp"]
        ),
        "remaining_same_hour_path_uncertainty": ordering == "UNRESOLVED_SAME_HOUR",
    }


def inspect_cases(*, bars_dir: Path, cases_dir: Path) -> dict[str, object]:
    symbols = (
        "BNBUSDT", "BTCUSDT", "DOGEUSDT",
        "ETHUSDT", "SOLUSDT", "XRPUSDT",
    )
    result, synchronized, _, daily_paths = run_shipped_portfolio(
        bars_dir=bars_dir, symbols=symbols
    )
    policy = PositionManagementPolicy()
    slip = Decimal("5") / Decimal("10000")
    taker_fee = Decimal("8") / Decimal("10000")

    cases: list[dict[str, object]] = []
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in daily_paths
    }
    for folder in sorted(cases_dir.iterdir()):
        if not folder.is_dir():
            continue
        manifest_path = folder / "provenance.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("data_origin") != "BYBIT_CONNECTOR_PUBLIC_KLINE":
            raise ValueError("trade case requires declared real Bybit connector candles")
        symbol = manifest["symbol"]
        day = manifest["days"][0]
        admitted = validate_intraday_fixture(
            folder,
            symbols=(symbol,),
            days=(day,),
            interval_minutes=60,
            maximum_missing_fraction=0,
        )
        if not admitted["admitted_for_research_only"] or admitted["test_fixture_only"]:
            raise ValueError("fixture cannot be used as trade-path evidence")
        hourly_path = folder / f"{symbol}_60m.csv"
        hourly = _hour_rows(hourly_path)
        frozen = {
            bar.timestamp.date().isoformat(): bar
            for bar in synchronized[symbol]
        }
        _reconcile_daily(hourly, frozen[day])
        entry_date = manifest["existing_trade_entry_day"]
        match = [
            trade for trade in result.closed_trades
            if trade.symbol == symbol
            and trade.entry_time.date().isoformat() == entry_date
            and trade.exit_time.date().isoformat() == day
            and trade.ambiguous_intrabar_exit
        ]
        if len(match) != 1:
            raise ValueError("unique frozen ambiguous strategy trade not found")
        trade = match[0]
        # The real PortfolioLedger capitalizes entry fees in average_cost.
        # Using entry execution price alone shifts the stop/target and can
        # wrongly classify a close-to-stop example.
        average_cost = trade.entry_execution_price + trade.entry_fee / trade.quantity
        hard_stop = average_cost * (Decimal("1") - policy.stop_loss_fraction)
        take_profit = average_cost * (Decimal("1") + policy.take_profit_fraction)

        historical_bars = synchronized[symbol]
        entry_index = next(
            i for i, bar in enumerate(historical_bars)
            if bar.timestamp == trade.entry_time
        )
        exit_index = next(
            i for i, bar in enumerate(historical_bars)
            if bar.timestamp == trade.exit_time
        )
        if exit_index < entry_index:
            raise ValueError("trade exits before its entry")
        peak_prior_completed = max(
            [trade.entry_reference_price]
            + [bar.high for bar in historical_bars[entry_index:exit_index]]
        )
        trailing_active = peak_prior_completed >= average_cost * (
            Decimal("1") + policy.trailing_activation_fraction
        )
        trailing = (
            peak_prior_completed * (Decimal("1") - policy.trailing_stop_fraction)
            if trailing_active else None
        )
        protective = max(hard_stop, trailing) if trailing is not None else hard_stop
        protective_reason = (
            "TRAILING_STOP" if trailing is not None and trailing > hard_stop
            else "HARD_STOP"
        )
        order = _trigger_order(
            hourly, protective=protective, take_profit=take_profit
        )
        conditional_target_net = None
        if order["ordering"] == "TAKE_PROFIT_FIRST_HOURLY":
            hypothetical_execution = take_profit * (Decimal("1") - slip)
            hypothetical_exit_fee = trade.quantity * hypothetical_execution * taker_fee
            conditional_target_net = (
                trade.quantity * (hypothetical_execution - average_cost)
                - hypothetical_exit_fee
            )
        identity = hashlib.sha256(hourly_path.read_bytes()).hexdigest()
        hashes[f"{folder.name}/{hourly_path.name}"] = identity
        cases.append({
            "symbol": symbol,
            "entry_day": entry_date,
            "exit_day": day,
            "entry_execution_price": str(trade.entry_execution_price),
            "ledger_average_cost_including_entry_fee": str(average_cost),
            "hard_stop": str(hard_stop),
            "take_profit": str(take_profit),
            "trailing_active_before_exit_day": trailing_active,
            "trailing_stop_from_prior_completed_bars": (
                None if trailing is None else str(trailing)
            ),
            "effective_protective": str(protective),
            "protective_reason": protective_reason,
            "daily_conservative_exit_reason": trade.exit_reason.value,
            "daily_ambiguous": trade.ambiguous_intrabar_exit,
            "original_daily_net_pnl_before_funding": str(trade.net_pnl),
            "hourly": order,
            "conditional_target_first_trade_net_before_funding": (
                None if conditional_target_net is None
                else str(conditional_target_net)
            ),
            "conditional_net_not_portfolio_replay": True,
            "source_sha256": identity,
        })

    if not cases:
        raise ValueError("no independently sourced intraday trade cases")
    payload: dict[str, object] = {
        "schema_version": "astra-intraday-trade-order-research-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_EXECUTION_ORDER_DIAGNOSTIC_ONLY",
        "research_only": True,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "untouched_holdout": False,
        "provider_authenticity_independently_verified": False,
        "strategy_changed": False,
        "original_closed_trade_count": result.closed_trade_count,
        "original_portfolio_return_pre_funding": str(result.total_return),
        "original_cost_assumptions": {
            "taker_fee_bps_per_fill": "8",
            "slippage_bps_per_side": "5",
        },
        "source_sha256": dict(sorted(hashes.items())),
        "summary": {
            "cases": len(cases),
            "take_profit_first_hourly": sum(
                row["hourly"]["ordering"] == "TAKE_PROFIT_FIRST_HOURLY"
                for row in cases
            ),
            "protective_first_hourly": sum(
                row["hourly"]["ordering"] == "PROTECTIVE_FIRST_HOURLY"
                for row in cases
            ),
            "same_hour_ambiguous": sum(
                row["hourly"]["ordering"] == "UNRESOLVED_SAME_HOUR"
                for row in cases
            ),
        },
        "cases": cases,
        "limitations": [
            "Exact provider order fills are not observed",
            "Conditional one-trade target PnL is NOT a recomputed portfolio result",
            "Historical source already inspected; no fresh OOS claim",
            "Hourly OHLC cannot resolve within-hour paths or queue priority",
            "Funding not allocated to individual trade examples",
            "No strategy promotion or alteration of protective orders",
        ],
    }
    payload["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--cases-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = inspect_cases(bars_dir=args.bars_dir, cases_dir=args.cases_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], sort_keys=True, indent=2))
    for item in report["cases"]:
        print(
            "CASE", item["symbol"], item["exit_day"],
            item["hourly"]["ordering"],
            "hourly_protective=", item["hourly"]["first_protective_hour"],
            "hourly_target=", item["hourly"]["first_target_hour"],
            "daily_net=", item["original_daily_net_pnl_before_funding"],
            "conditional_target_net=",
            item["conditional_target_first_trade_net_before_funding"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
