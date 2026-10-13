"""Full-preselected 17-case six-major daily OHLC collision reconstruction.

Every case is loaded from the frozen full six-major ambiguity census, *not*
from profitable intraday examples. Public Bybit hourly candles are SHA-locked
and must reconcile exactly to untouched raw daily candles. This establishes
observed order of threshold TOUCHES at 1h resolution, not real fills,
portfolio counterfactual PnL or strategy profitability.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tools.historical_ambiguity_census import SIX_MAJOR, census_for_universe
from tools.intraday_evidence_admission import validate_intraday_fixture
from tools.intraday_trade_execution_order import _trigger_order

D = Decimal


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _assert_24_hourly_reconcile_with_raw_daily(
    hours: list[dict[str, str]],
    *,
    daily_csv: Path,
    date: str,
) -> None:
    daily = [row for row in _rows(daily_csv) if row["timestamp"][:10] == date]
    if len(daily) != 1 or len(hours) != 24:
        raise ValueError("missing/duplicate daily or hourly source")
    source = daily[0]
    derived = {
        "open": D(hours[0]["open"]),
        "high": max(D(row["high"]) for row in hours),
        "low": min(D(row["low"]) for row in hours),
        "close": D(hours[-1]["close"]),
        "volume": sum((D(row["volume"]) for row in hours), D("0")),
    }
    for field, value in derived.items():
        if value != D(source[field]):
            raise ValueError(f"real intraday source differs from daily {date} {field}")


def reconstruct_all_six_major(
    *,
    bars_dir: Path,
    original_hourly_dir: Path,
    additional_hourly_dir: Path,
) -> dict[str, object]:
    census = census_for_universe(
        bars_dir=bars_dir,
        symbols=SIX_MAJOR,
        universe_id="SIX_MAJOR_FULL",
        pinned_hourly_dir=original_hourly_dir,
    )
    if census["ambiguity_sample"]["cases"] != 17:
        raise ValueError("frozen all-case census no longer contains 17 cases")
    rows: list[dict] = []
    source_sha = {}
    prior_count = 0
    new_count = 0
    for case in census["cases"]:
        symbol, day = case["symbol"], case["exit_day"]
        subdirectory = f"{symbol}_{day}"
        prior = original_hourly_dir / subdirectory
        additional = additional_hourly_dir / subdirectory
        exists_prior = (prior / f"{symbol}_60m.csv").is_file()
        exists_new = (additional / f"{symbol}_60m.csv").is_file()
        if exists_prior and exists_new:
            raise ValueError("ambiguous double source for preselected case")
        if not exists_prior and not exists_new:
            raise ValueError(f"missing preselected real 1h source: {symbol} {day}")
        directory = prior if exists_prior else additional
        prior_count += int(exists_prior)
        new_count += int(exists_new)
        manifest = json.loads(
            (directory / "provenance.json").read_text(encoding="utf-8")
        )
        if manifest["data_origin"] != "BYBIT_CONNECTOR_PUBLIC_KLINE":
            raise ValueError("fake/daily-resampled hourly source forbidden")
        admission = validate_intraday_fixture(
            directory,
            symbols=(symbol,),
            days=(day,),
            interval_minutes=60,
            maximum_missing_fraction=0,
        )
        if not admission["admitted_for_research_only"]:
            raise ValueError("provider source cannot enter hourly research")
        csv_path = directory / f"{symbol}_60m.csv"
        hours = _rows(csv_path)
        _assert_24_hourly_reconcile_with_raw_daily(
            hours,
            daily_csv=bars_dir / f"{symbol}_D.csv",
            date=day,
        )
        order = _trigger_order(
            hours,
            protective=D(case["effective_protective_price"]),
            take_profit=D(case["take_profit_price"]),
        )
        if order["ordering"] == "NO_TRIGGER_IN_HOURLY":
            raise ValueError("frozen daily collision has no hourly threshold touch")
        source_sha[case["case_id"]] = hashlib.sha256(
            csv_path.read_bytes()
        ).hexdigest()

        conditional_target_first_net: Decimal | None = None
        if order["ordering"] == "TAKE_PROFIT_FIRST_HOURLY":
            target = D(case["take_profit_price"])
            qty = D(case["position_base_quantity"])
            avg = D(case["ledger_average_cost"])
            simulated_exit = target * (D("1") - D("0.0005"))
            simulated_exit_fee = qty * simulated_exit * D("0.0008")
            conditional_target_first_net = (
                qty * (simulated_exit - avg) - simulated_exit_fee
            )

        rows.append({
            "case_id": case["case_id"],
            "symbol": symbol,
            "entry_utc": case["entry_utc"],
            "exit_utc": case["exit_utc"],
            "exit_day": day,
            "original_conservative_exit_reason": case[
                "daily_conservative_exit_reason"
            ],
            "original_trade_net_usd_before_funding": case[
                "original_modelled_net_pnl_usd_before_funding"
            ],
            "hourly_first_touch_order": order,
            "conditional_single_trade_target_net_if_target_first": (
                None if conditional_target_first_net is None
                else str(conditional_target_first_net)
            ),
            "source_hourly_candles": len(hours),
            "source_sha256": source_sha[case["case_id"]],
            "previously_pinned_first_four": exists_prior,
            "public_bybit_hourly_ohlcv_verified_against_daily": True,
            "actual_order_execution_verified": False,
            "portfolio_recomputed_after_conditional_exit": False,
        })
    if len(rows) != 17 or prior_count != 4 or new_count != 13:
        raise ValueError("not the full frozen 4+13 preselected population")

    outcomes = Counter(
        row["hourly_first_touch_order"]["ordering"] for row in rows
    )
    if sum(outcomes.values()) != len(rows):
        raise ValueError("hourly ordering counts do not conserve")
    original_pnl = sum(
        (D(row["original_trade_net_usd_before_funding"]) for row in rows),
        D("0"),
    )
    if abs(original_pnl - D(
        census["ambiguity_sample"]["original_net_before_funding_usd"]
    )) > D("0.00000001") * len(rows):
        raise ValueError("original daily collision PnL was modified")
    payload: dict[str, object] = {
        "schema_version": "astra-full-six-major-hourly-order-research-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_FULL_HOURLY_COLLISION_DIAGNOSTIC_ONLY",
        "research_only": True,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "strategy_changed": False,
        "original_portfolio_recomputed": False,
        "intraday_full_portfolio_backtest_run": False,
        "untouched_holdout": False,
        "provider_authenticity_independently_verified": False,
        "execution_venue_fills_verified": False,
        "funding_allocated_per_case": False,
        "six_major_census_evidence_sha256": census["evidence_sha256"],
        "source_sha256": dict(sorted(source_sha.items())),
        "original_portfolio_return_before_funding": census[
            "original_portfolio"
        ]["total_return_pre_funding"],
        "summary": {
            "preselected_cases": len(rows),
            "pinned_prior_hourly_cases": prior_count,
            "new_provider_hourly_cases": new_count,
            "total_hourly_candles": 24 * len(rows),
            "original_daily_conservative_trade_net_usd": str(original_pnl),
            "hourly_order_counts": dict(sorted(outcomes.items())),
            "outcome_unavailable_cases": 0,
        },
        "cases": rows,
        "limitations": [
            "Entire preselected six-major daily ambiguity set, but still inspected history",
            "Intrahour stop/target order unresolved when thresholds share an hour",
            "Bybit public OHLC source hashes prove retained bytes, not venue account fills",
            "No slippage/queue/partial-execution/mark-funding account evidence",
            "Conditional target PnL is a single-trade sensitivity, not portfolio return",
            "Broad21's 24 collision cases still require their own full source population",
            "No trading authority or qualification promotion",
        ],
    }
    payload["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bars-dir", type=Path, required=True)
    p.add_argument("--original-hourly-dir", type=Path, required=True)
    p.add_argument("--additional-hourly-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args(argv)
    payload = reconstruct_all_six_major(
        bars_dir=args.bars_dir,
        original_hourly_dir=args.original_hourly_dir,
        additional_hourly_dir=args.additional_hourly_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print("FULL_SIX", json.dumps(payload["summary"], sort_keys=True))
    for case in payload["cases"]:
        print(
            "CASE", case["symbol"], case["exit_day"],
            "ORDER", case["hourly_first_touch_order"]["ordering"],
            "DAILY_NET", case["original_trade_net_usd_before_funding"],
            "CONDITIONAL_SINGLE", case[
                "conditional_single_trade_target_net_if_target_first"
            ],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
