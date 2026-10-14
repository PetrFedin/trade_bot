"""Fail-closed hourly reconstruction of the COMPLETE preselected broad-21 collision census.

Research only. Missing original Bybit hourly source is NOT_EVIDENCED and blocks
any claim of complete intraday ordering. Never replace portfolio PnL with the
conditional single-trade observations; never permit strategy promotion.
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
from tools.full_six_hourly_ambiguity import _assert_24_hourly_reconcile_with_raw_daily
from tools.historical_ambiguity_census import BROAD_21, census_for_universe
from tools.intraday_evidence_admission import validate_intraday_fixture
from tools.intraday_trade_execution_order import _trigger_order


def reconstruct_broad21(
    *,
    bars_dir: Path,
    previously_pinned_hourly_dir: Path,
    six_major_additional_dir: Path,
    broad_additional_dir: Path,
) -> dict:
    frozen = census_for_universe(
        bars_dir=bars_dir,
        symbols=BROAD_21,
        universe_id="BROAD_21_COMMON",
        pinned_hourly_dir=previously_pinned_hourly_dir,
    )
    if frozen["ambiguity_sample"]["cases"] != 24:
        raise ValueError("frozen broad-21 population changed")
    cases = []
    missing = []
    source_sha = {}
    counts = Counter()
    for case in frozen["cases"]:
        symbol, day = case["symbol"], case["exit_day"]
        directory_name = f"{symbol}_{day}"
        candidates = [
            directory / directory_name
            for directory in (
                previously_pinned_hourly_dir,
                six_major_additional_dir,
                broad_additional_dir,
            )
            if (directory / directory_name / f"{symbol}_60m.csv").is_file()
        ]
        if len(candidates) > 1:
            raise ValueError(f"duplicate hourly sources: {directory_name}")
        if not candidates:
            missing.append(directory_name)
            continue
        folder = candidates[0]
        manifest = json.loads((folder / "provenance.json").read_text())
        if manifest.get("data_origin") != "BYBIT_CONNECTOR_PUBLIC_KLINE":
            raise ValueError("unqualified source provenance")
        admitted = validate_intraday_fixture(
            folder, symbols=(symbol,), days=(day,),
            interval_minutes=60, maximum_missing_fraction=0,
        )
        if not admitted["admitted_for_research_only"] or admitted["test_fixture_only"]:
            raise ValueError("source not admitted for research")
        path = folder / f"{symbol}_60m.csv"
        with path.open(newline="", encoding="utf-8") as stream:
            hours = list(csv.DictReader(stream))
        _assert_24_hourly_reconcile_with_raw_daily(
            hours, daily_csv=bars_dir / f"{symbol}_D.csv", date=day,
        )
        order = _trigger_order(
            hours,
            protective=Decimal(case["effective_protective_price"]),
            take_profit=Decimal(case["take_profit_price"]),
        )
        if order["ordering"] == "NO_TRIGGER_IN_HOURLY":
            raise ValueError("daily collision has no verified intraday trigger")
        counts[order["ordering"]] += 1
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        source_sha[case["case_id"]] = digest
        cases.append({
            "case_id": case["case_id"],
            "symbol": symbol,
            "exit_day": day,
            "source_sha256": digest,
            "original_conservative_net_usd_before_funding":
                case["original_modelled_net_pnl_usd_before_funding"],
            "observed_hourly_threshold_order": order,
            "hourly_candles": len(hours),
            "actual_exchange_fill_verified": False,
        })
    original_net = sum(
        (Decimal(case["original_modelled_net_pnl_usd_before_funding"])
         for case in frozen["cases"]), Decimal("0"),
    )
    if abs(original_net - Decimal(
        frozen["ambiguity_sample"]["original_net_before_funding_usd"]
    )) > Decimal("0.00000001") * 24:
        raise ValueError("original 24-case PnL is not conserved")
    covered = len(cases)
    if covered + len(missing) != 24:
        raise ValueError("case population does not conserve")
    result = {
        "schema_version": "astra-broad21-hourly-collision-diagnostic-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_BROAD21_RESEARCH_ONLY",
        "research_only": True,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "actual_exchange_fills_verified": False,
        "counterfactual_portfolio_replayed": False,
        "untouched_holdout": False,
        "original_strategy_modified": False,
        "complete_case_coverage": covered == 24,
        "original_census_sha256": frozen["evidence_sha256"],
        "original_conservative_24_case_net_before_funding_usd": str(original_net),
        "summary": {
            "preselected_cases": 24,
            "hourly_source_verified_cases": covered,
            "missing_hourly_source_cases": len(missing),
            "hourly_candles": covered * 24,
            "first_touch_counts": dict(sorted(counts.items())),
        },
        "missing_cases_not_evidenced": missing,
        "source_sha256": dict(sorted(source_sha.items())),
        "cases": cases,
        "limitations": [
            "Source is public connector OHLCV, not independently signed exchange fills",
            "Hourly threshold ordering is not intrahour order-book execution",
            "Only historical inspected data; original portfolio PnL is unchanged",
            "No fees by venue order, queue, funding-account or market-impact proof",
            "No strategy optimization, activation or profitability conclusion",
        ],
    }
    result["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(result)
    ).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--original-hourly-dir", type=Path, required=True)
    parser.add_argument("--six-hourly-dir", type=Path, required=True)
    parser.add_argument("--broad-hourly-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = reconstruct_broad21(
        bars_dir=args.bars_dir,
        previously_pinned_hourly_dir=args.original_hourly_dir,
        six_major_additional_dir=args.six_hourly_dir,
        broad_additional_dir=args.broad_hourly_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print("BROAD21_HOURLY", json.dumps(report["summary"], sort_keys=True))
    return 0 if report["complete_case_coverage"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
