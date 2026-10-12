"""Research-only 5-minute reconstruction of four frozen Strategy A daily OHLC collisions.

The baseline is NOT rerun as an alternative portfolio. Original 6-major
PortfolioTrades and 60m case evidence are computed by the unchanged
historical code. The same levels are inspected against real Bybit 5m
candles; all 5m bars must exactly reconcile to the already pinned 60m
source. Remaining within-5m path ordering is explicitly unresolved.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tools.intraday_evidence_admission import validate_intraday_fixture
from tools.intraday_trade_execution_order import inspect_cases


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _reconcile_5m_to_hourly(
    fine: list[dict[str, str]],
    hourly: list[dict[str, str]],
) -> None:
    if len(fine) != 288 or len(hourly) != 24:
        raise ValueError("expected exactly 288 5m and 24 hourly candles")
    for index in range(24):
        chunk = fine[index * 12 : (index + 1) * 12]
        reference = hourly[index]
        if chunk[0]["timestamp"][:13] != reference["timestamp"][:13]:
            raise ValueError("mismatched UTC hour")
        derived = {
            "open": Decimal(chunk[0]["open"]),
            "high": max(Decimal(b["high"]) for b in chunk),
            "low": min(Decimal(b["low"]) for b in chunk),
            "close": Decimal(chunk[-1]["close"]),
            "volume": sum(
                (Decimal(b["volume"]) for b in chunk), Decimal("0")
            ),
        }
        for field, actual in derived.items():
            if actual != Decimal(reference[field]):
                raise ValueError(
                    f"5m/1h reconciliation failed at hour {index} {field}"
                )


def _five_minute_order(
    fine: list[dict[str, str]],
    *,
    protective: Decimal,
    take_profit: Decimal,
) -> dict[str, object]:
    first_protective = None
    first_target = None
    for index, row in enumerate(fine):
        opening = Decimal(row["open"])
        low = Decimal(row["low"])
        high = Decimal(row["high"])
        if first_protective is None and (opening <= protective or low <= protective):
            first_protective = index
        if first_target is None and (opening >= take_profit or high >= take_profit):
            first_target = index

    if first_protective is None and first_target is None:
        status = "NO_TRIGGER_IN_5M"
    elif first_protective == first_target:
        status = "UNRESOLVED_SAME_FIVE_MINUTE_BAR"
    elif first_protective is not None and (
        first_target is None or first_protective < first_target
    ):
        status = "PROTECTIVE_FIRST_5M"
    else:
        status = "TAKE_PROFIT_FIRST_5M"
    return {
        "ordering": status,
        "first_protective_index": first_protective,
        "first_target_index": first_target,
        "first_protective_timestamp": (
            None if first_protective is None
            else fine[first_protective]["timestamp"]
        ),
        "first_target_timestamp": (
            None if first_target is None
            else fine[first_target]["timestamp"]
        ),
        "five_minute_path_fully_known": False,
    }


def inspect_five_minute_cases(
    *,
    bars_dir: Path,
    hourly_cases_dir: Path,
    five_minute_cases_dir: Path,
) -> dict[str, object]:
    hourly_report = inspect_cases(
        bars_dir=bars_dir, cases_dir=hourly_cases_dir
    )
    case_lookup = {
        (case["symbol"], case["exit_day"]): case
        for case in hourly_report["cases"]
    }
    if len(case_lookup) != len(hourly_report["cases"]):
        raise ValueError("duplicate hourly case identity")

    source_sha = {}
    cases = []
    for folder in sorted(five_minute_cases_dir.iterdir()):
        if not folder.is_dir():
            continue
        manifest_path = folder / "provenance.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("data_origin") != "BYBIT_CONNECTOR_PUBLIC_KLINE":
            raise ValueError("5m case must use declared Bybit provider data")
        symbol, day = manifest["symbol"], manifest["days"][0]
        key = (symbol, day)
        if key not in case_lookup:
            raise ValueError(f"5m case lacks frozen 1h reference: {key}")
        base = case_lookup[key]
        fine_file = folder / f"{symbol}_5m.csv"
        hourly_file = hourly_cases_dir / folder.name / f"{symbol}_60m.csv"
        fine = _rows(fine_file)
        hourly = _rows(hourly_file)
        validation = validate_intraday_fixture(
            folder,
            symbols=(symbol,),
            days=(day,),
            interval_minutes=5,
            maximum_missing_fraction=0,
        )
        if validation["test_fixture_only"] or not validation["admitted_for_research_only"]:
            raise ValueError("test/synthetic 5m source cannot prove market movement")
        if manifest["hourly_reference_sha256"] != hashlib.sha256(
            hourly_file.read_bytes()
        ).hexdigest():
            raise ValueError("5m source's pinned hourly reference has changed")
        _reconcile_5m_to_hourly(fine, hourly)
        order = _five_minute_order(
            fine,
            protective=Decimal(base["effective_protective"]),
            take_profit=Decimal(base["take_profit"]),
        )
        coarse = base["hourly"]["ordering"]
        if coarse == "TAKE_PROFIT_FIRST_HOURLY":
            if order["ordering"] != "TAKE_PROFIT_FIRST_5M":
                raise ValueError("5m order inconsistent with separate-hour target-first")
        elif coarse == "PROTECTIVE_FIRST_HOURLY":
            if order["ordering"] != "PROTECTIVE_FIRST_5M":
                raise ValueError("5m order inconsistent with separate-hour protection")
        source_sha[folder.name] = hashlib.sha256(fine_file.read_bytes()).hexdigest()
        cases.append({
            "symbol": symbol,
            "exit_day": day,
            "daily_trade_entry_day": base["entry_day"],
            "original_daily_exit_reason": base["daily_conservative_exit_reason"],
            "original_daily_net_before_funding": base["original_daily_net_pnl_before_funding"],
            "conditional_single_trade_net_from_hourly": (
                base["conditional_target_first_trade_net_before_funding"]
            ),
            "effective_protective": base["effective_protective"],
            "take_profit": base["take_profit"],
            "hourly_first_trigger": base["hourly"],
            "five_minute_first_trigger": order,
            "five_minute_vs_hourly_ohlcv_reconciled": True,
            "exact_provider_fill_observed": False,
            "capital_redeployment_replayed": False,
        })
    if set((case["symbol"], case["exit_day"]) for case in cases) != set(case_lookup):
        raise ValueError("some frozen hourly ambiguous trades lack real 5m evidence")

    summary = {
        "cases": len(cases),
        "hourly_target_first": hourly_report["summary"]["take_profit_first_hourly"],
        "five_minute_target_first": sum(
            item["five_minute_first_trigger"]["ordering"] == "TAKE_PROFIT_FIRST_5M"
            for item in cases
        ),
        "five_minute_protective_first": sum(
            item["five_minute_first_trigger"]["ordering"] == "PROTECTIVE_FIRST_5M"
            for item in cases
        ),
        "five_minute_unresolved": sum(
            item["five_minute_first_trigger"]["ordering"] == (
                "UNRESOLVED_SAME_FIVE_MINUTE_BAR"
            ) for item in cases
        ),
        "complete_five_minute_candles": 288 * len(cases),
    }
    payload: dict[str, object] = {
        "schema_version": "astra-5m-intraday-collision-diagnostic-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_FINE_EXECUTION_ORDER_DIAGNOSTIC_ONLY",
        "research_only": True,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "untouched_holdout": False,
        "strategy_changed": False,
        "provider_authenticity_independently_verified": False,
        "full_portfolio_pnl_recomputed": False,
        "source_sha256": dict(sorted(source_sha.items())),
        "hourly_evidence_sha256": hourly_report["evidence_sha256"],
        "original_portfolio_return_pre_funding": (
            hourly_report["original_portfolio_return_pre_funding"]
        ),
        "summary": summary,
        "cases": cases,
        "limitations": [
            "Within-five-minute OHLC path ordering remains unresolved",
            "No order-book depth, queue, fill mechanics, latency, partial fills or true funding",
            "Only four deliberately selected ambiguous historical cases; selection bias",
            "Conditional single-trade result is NOT the path-dependent portfolio PnL",
            "Provider/public connector source hashes are not exchange signed attestations",
        ],
    }
    payload["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--hourly-cases-dir", type=Path, required=True)
    parser.add_argument("--five-minute-cases-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    data = inspect_five_minute_cases(
        bars_dir=args.bars_dir,
        hourly_cases_dir=args.hourly_cases_dir,
        five_minute_cases_dir=args.five_minute_cases_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(data["summary"], sort_keys=True))
    for item in data["cases"]:
        print(
            "CASE", item["symbol"], item["exit_day"],
            "five_minute_order=", item["five_minute_first_trigger"]["ordering"],
            "first_protective=", (
                item["five_minute_first_trigger"]["first_protective_timestamp"]
            ),
            "first_target=", (
                item["five_minute_first_trigger"]["first_target_timestamp"]
            ),
            "original_daily_trade_net=", item["original_daily_net_before_funding"],
            "conditional_target_net=", (
                item["conditional_single_trade_net_from_hourly"]
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
