from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_ZERO = Decimal("0")
_ONE = Decimal("1")
_NEAR_MISS_RATIO = Decimal("0.90")
_MID_RATIO = Decimal("0.75")
_SIDES = ("LONG", "SHORT")


def audit_runner_opportunity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Measure how close accepted entries were to the frozen runner admission gate.

    This is intentionally a pre-entry diagnostic. Fixed-target exits censor the subsequent price
    path, so this audit does not infer hypothetical runner PnL from fixed-target MFE.
    """

    events = candidate.get("decision_events")
    if not isinstance(events, list):
        raise ValueError("runner opportunity candidate decision_events must be an array")

    entries = [event for event in events if _is_entry(event)]
    diagnostics = _summarize_entries(entries)
    by_side = {
        side: _summarize_entries(
            [event for event in entries if str(event.get("side", "")).upper() == side]
        )
        for side in _SIDES
    }
    unexpected_sides = {
        str(event.get("side", "")).upper()
        for event in entries
        if str(event.get("side", "")).upper() not in _SIDES
    }
    if unexpected_sides:
        raise ValueError(
            f"unexpected runner opportunity sides: {sorted(unexpected_sides)}"
        )

    return {
        "qualification": "CRYPTO_RUNNER_ADMISSION_OPPORTUNITY_DIAGNOSTIC",
        **diagnostics,
        "by_side": by_side,
        "ratio_bucket_contract": {
            "far_below_gate": "ratio < 0.75",
            "below_gate": "0.75 <= ratio < 0.90",
            "near_miss": "0.90 <= ratio < 1.00",
            "gate_cleared": "ratio >= 1.00",
        },
        "fixed_target_post_exit_path_is_censored": True,
        "fixed_target_mfe_may_validate_runner_counterfactual": False,
        "runner_threshold_retuning_allowed": False,
        "automatic_runner_activation_allowed": False,
        "strategy_promotion_allowed": False,
        "live_promotion_allowed": False,
    }


def audit_report_runner_opportunity(report: Mapping[str, Any]) -> dict[str, Any]:
    candidates = report.get("strategy_shadow_candidates")
    if not isinstance(candidates, Mapping):
        raise ValueError("runner opportunity report strategy_shadow_candidates are missing")

    audited: dict[str, Any] = {}
    for name, candidate in candidates.items():
        if not isinstance(name, str) or not isinstance(candidate, Mapping):
            raise ValueError("runner opportunity candidate mapping is invalid")
        if candidate.get("runner_minimum_expected_edge_multiple") is None:
            continue
        audited[name] = audit_runner_opportunity(candidate)

    if not audited:
        raise ValueError("runner opportunity report has no conditional runner candidates")
    return {
        "qualification": "CRYPTO_RUNNER_ADMISSION_OPPORTUNITY_AUDIT",
        "source": report.get("source"),
        "archive_dates": report.get("archive_dates"),
        "candidates": audited,
        "runner_threshold_retuning_allowed": False,
        "automatic_runner_activation_allowed": False,
        "strategy_promotion_allowed": False,
        "live_promotion_allowed": False,
    }


def _is_entry(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("event") == "ENTRY"


def _summarize_entries(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ratios: list[Decimal] = []
    fixed_ratios: list[Decimal] = []
    fixed_gaps: list[Decimal] = []
    runner_selected = 0
    fixed_selected = 0
    missing_required = 0
    reason_counts: Counter[str] = Counter()

    for event in entries:
        exit_mode = str(event.get("exit_mode", ""))
        if exit_mode == "RUNNER":
            runner_selected += 1
        elif exit_mode == "FIXED_20_TARGET":
            fixed_selected += 1
        else:
            raise ValueError(f"unexpected runner opportunity exit mode: {exit_mode!r}")

        for reason in _string_sequence(event.get("runner_admission_reasons")):
            reason_counts[reason] += 1

        required_raw = event.get("runner_required_expected_net_edge_usd")
        if required_raw is None:
            missing_required += 1
            continue
        required = _positive_decimal(required_raw, "runner_required_expected_net_edge_usd")
        expected = _finite_decimal(event.get("expected_net_edge_usd"), "expected_net_edge_usd")
        ratio = expected / required
        ratios.append(ratio)
        if exit_mode == "FIXED_20_TARGET":
            fixed_ratios.append(ratio)
            fixed_gaps.append(required - expected)

    bucket_counts = _ratio_buckets(ratios)
    fixed_bucket_counts = _ratio_buckets(fixed_ratios)
    return {
        "entry_count": len(entries),
        "runner_selected_entry_count": runner_selected,
        "fixed_target_selected_entry_count": fixed_selected,
        "runner_selected_fraction": (
            None if not entries else float(Decimal(runner_selected) / Decimal(len(entries)))
        ),
        "entries_with_required_edge_count": len(ratios),
        "entries_missing_required_edge_count": missing_required,
        "expected_edge_to_required_edge_ratio": _distribution(ratios),
        "expected_edge_to_required_edge_ratio_bucket_counts": bucket_counts,
        "fixed_target_expected_edge_to_required_edge_ratio": _distribution(fixed_ratios),
        "fixed_target_ratio_bucket_counts": fixed_bucket_counts,
        "fixed_target_near_miss_count": fixed_bucket_counts["near_miss"],
        "fixed_target_required_edge_gap_usd": _distribution(fixed_gaps),
        "runner_admission_reason_counts": dict(sorted(reason_counts.items())),
    }


def _ratio_buckets(values: Sequence[Decimal]) -> dict[str, int]:
    buckets = {
        "far_below_gate": 0,
        "below_gate": 0,
        "near_miss": 0,
        "gate_cleared": 0,
    }
    for value in values:
        if value < _MID_RATIO:
            buckets["far_below_gate"] += 1
        elif value < _NEAR_MISS_RATIO:
            buckets["below_gate"] += 1
        elif value < _ONE:
            buckets["near_miss"] += 1
        else:
            buckets["gate_cleared"] += 1
    return buckets


def _distribution(values: Sequence[Decimal]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "minimum": None,
            "p25_nearest_rank": None,
            "median_nearest_rank": None,
            "p75_nearest_rank": None,
            "p90_nearest_rank": None,
            "maximum": None,
            "mean": None,
        }
    ordered = sorted(values)
    total = sum(ordered, start=_ZERO)
    return {
        "count": len(ordered),
        "minimum": float(ordered[0]),
        "p25_nearest_rank": float(_nearest_rank(ordered, Decimal("0.25"))),
        "median_nearest_rank": float(_nearest_rank(ordered, Decimal("0.50"))),
        "p75_nearest_rank": float(_nearest_rank(ordered, Decimal("0.75"))),
        "p90_nearest_rank": float(_nearest_rank(ordered, Decimal("0.90"))),
        "maximum": float(ordered[-1]),
        "mean": float(total / Decimal(len(ordered))),
    }


def _nearest_rank(values: Sequence[Decimal], fraction: Decimal) -> Decimal:
    if not values:
        raise ValueError("nearest-rank distribution cannot be empty")
    rank = int((fraction * Decimal(len(values))).to_integral_value(rounding="ROUND_CEILING"))
    return values[max(1, rank) - 1]


def _string_sequence(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("runner admission reasons must be an array of strings")
    return tuple(value)


def _finite_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"runner opportunity {field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"runner opportunity {field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"runner opportunity {field} must be a finite decimal")
    return parsed


def _positive_decimal(value: object, field: str) -> Decimal:
    parsed = _finite_decimal(value, field)
    if parsed <= 0:
        raise ValueError(f"runner opportunity {field} must be positive")
    return parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit conditional Bybit runner admission opportunity without retuning"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("runner opportunity input must be an object")
    result = audit_report_runner_opportunity(raw)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("BYBIT_RUNNER_OPPORTUNITY_AUDIT=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
