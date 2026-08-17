from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_ZERO = Decimal("0")
_BASELINE = "CONDITIONAL_1_5X"
_SESSION = "CONDITIONAL_SESSION_RISK"
_COMBINED = "CONDITIONAL_COMBINED_RISK"
_SIDES = ("LONG", "SHORT")


def audit_walk_forward_session_risk(report: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize where the frozen session overlay changed chronological folds."""

    folds = report.get("folds")
    if not isinstance(folds, list) or not folds:
        raise ValueError("walk-forward session-risk audit requires non-empty folds")

    reason_counts: Counter[str] = Counter()
    active_fold_count = 0
    total_entry_blocks = 0
    total_flatten_trades = 0
    session_net_delta = _ZERO
    active_session_net_delta = _ZERO
    inactive_session_net_delta = _ZERO
    combined_minus_session_delta = _ZERO
    side_delta = {
        side: {"closed_trade_delta": 0, "net_pnl_delta_usdt": _ZERO}
        for side in _SIDES
    }
    fold_rows: list[dict[str, Any]] = []

    for fold in folds:
        if not isinstance(fold, Mapping):
            raise ValueError("walk-forward fold must be an object")
        candidate_metrics = fold.get("candidate_metrics")
        if not isinstance(candidate_metrics, Mapping):
            raise ValueError("walk-forward fold candidate_metrics must be an object")
        baseline = _candidate(candidate_metrics, _BASELINE)
        session = _candidate(candidate_metrics, _SESSION)
        combined = _candidate(candidate_metrics, _COMBINED)
        session_state = session.get("session_risk")
        if not isinstance(session_state, Mapping):
            raise ValueError("walk-forward session-risk summary is missing")
        if session_state.get("enabled") is not True:
            raise ValueError("walk-forward session-risk candidate must be enabled")

        risk_events = _non_negative_int(
            session_state.get("risk_event_count"),
            "risk_event_count",
        )
        entry_blocks = _non_negative_int(
            session_state.get("entry_block_count"),
            "entry_block_count",
        )
        flatten_trades = _non_negative_int(
            session_state.get("flatten_trade_count"),
            "flatten_trade_count",
        )
        raw_reasons = session_state.get("reason_counts")
        if not isinstance(raw_reasons, Mapping):
            raise ValueError("walk-forward session-risk reason_counts must be an object")
        normalized_reasons = {
            str(reason): _non_negative_int(count, f"reason {reason}")
            for reason, count in raw_reasons.items()
        }
        if sum(normalized_reasons.values()) < risk_events:
            raise ValueError("session-risk reason counts cannot undercount risk events")
        reason_counts.update(normalized_reasons)

        baseline_metrics = _metrics(baseline)
        session_metrics = _metrics(session)
        combined_metrics = _metrics(combined)
        fold_session_delta = (
            session_metrics["total_net_pnl_usdt"]
            - baseline_metrics["total_net_pnl_usdt"]
        )
        fold_combined_residual = (
            combined_metrics["total_net_pnl_usdt"]
            - session_metrics["total_net_pnl_usdt"]
        )
        session_net_delta += fold_session_delta
        combined_minus_session_delta += fold_combined_residual
        if risk_events > 0:
            active_fold_count += 1
            active_session_net_delta += fold_session_delta
        else:
            inactive_session_net_delta += fold_session_delta

        side_rows: dict[str, Any] = {}
        for side in _SIDES:
            baseline_side = _side_metrics(baseline, side)
            session_side = _side_metrics(session, side)
            trade_delta = (
                session_side["closed_trade_count"]
                - baseline_side["closed_trade_count"]
            )
            pnl_delta = (
                session_side["total_net_pnl_usdt"]
                - baseline_side["total_net_pnl_usdt"]
            )
            side_delta[side]["closed_trade_delta"] += trade_delta
            side_delta[side]["net_pnl_delta_usdt"] += pnl_delta
            side_rows[side] = {
                "baseline_closed_trade_count": baseline_side["closed_trade_count"],
                "session_closed_trade_count": session_side["closed_trade_count"],
                "closed_trade_delta": trade_delta,
                "baseline_net_pnl_usdt": float(
                    baseline_side["total_net_pnl_usdt"]
                ),
                "session_net_pnl_usdt": float(
                    session_side["total_net_pnl_usdt"]
                ),
                "net_pnl_delta_usdt": float(pnl_delta),
            }

        total_entry_blocks += entry_blocks
        total_flatten_trades += flatten_trades
        fold_rows.append(
            {
                "fold": _non_negative_int(fold.get("fold"), "fold"),
                "first_date": fold.get("first_date"),
                "last_date": fold.get("last_date"),
                "session_risk_event_count": risk_events,
                "session_entry_block_count": entry_blocks,
                "session_flatten_trade_count": flatten_trades,
                "session_reason_counts": normalized_reasons,
                "session_vs_baseline_net_pnl_delta_usdt": float(fold_session_delta),
                "combined_minus_session_net_pnl_delta_usdt": float(
                    fold_combined_residual
                ),
                "side_deltas": side_rows,
            }
        )

    return {
        "qualification": "CRYPTO_WALK_FORWARD_SESSION_RISK_SUMMARY_AUDIT",
        "fold_count": len(folds),
        "session_risk_active_fold_count": active_fold_count,
        "session_risk_inactive_fold_count": len(folds) - active_fold_count,
        "session_risk_reason_counts": dict(sorted(reason_counts.items())),
        "session_risk_entry_block_count": total_entry_blocks,
        "session_risk_flatten_trade_count": total_flatten_trades,
        "session_vs_baseline_total_net_pnl_delta_usdt": float(session_net_delta),
        "active_folds_session_vs_baseline_net_pnl_delta_usdt": float(
            active_session_net_delta
        ),
        "inactive_folds_session_vs_baseline_net_pnl_delta_usdt": float(
            inactive_session_net_delta
        ),
        "combined_minus_session_total_net_pnl_delta_usdt": float(
            combined_minus_session_delta
        ),
        "side_deltas": {
            side: {
                "closed_trade_delta": values["closed_trade_delta"],
                "net_pnl_delta_usdt": float(values["net_pnl_delta_usdt"]),
            }
            for side, values in side_delta.items()
        },
        "folds": fold_rows,
        "attribution_is_observational_not_causal": True,
        "session_threshold_retuning_allowed": False,
        "directional_filter_activation_allowed": False,
        "automatic_strategy_activation_allowed": False,
        "strategy_promotion_allowed": False,
        "live_promotion_allowed": False,
    }


def _candidate(metrics: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    candidate = metrics.get(name)
    if not isinstance(candidate, Mapping):
        raise ValueError(f"walk-forward candidate is missing: {name}")
    return candidate


def _metrics(candidate: Mapping[str, Any]) -> dict[str, Decimal | int]:
    metrics = candidate.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("walk-forward candidate metrics are missing")
    return {
        "closed_trade_count": _non_negative_int(
            metrics.get("closed_trade_count"),
            "closed_trade_count",
        ),
        "total_net_pnl_usdt": _finite_decimal(
            metrics.get("total_net_pnl_usdt"),
            "total_net_pnl_usdt",
        ),
    }


def _side_metrics(candidate: Mapping[str, Any], side: str) -> dict[str, Decimal | int]:
    side_metrics = candidate.get("side_metrics")
    if not isinstance(side_metrics, Mapping):
        raise ValueError("walk-forward candidate side_metrics are missing")
    raw = side_metrics.get(side)
    if not isinstance(raw, Mapping):
        raise ValueError(f"walk-forward side metrics are missing: {side}")
    return {
        "closed_trade_count": _non_negative_int(
            raw.get("closed_trade_count"),
            f"{side} closed_trade_count",
        ),
        "total_net_pnl_usdt": _finite_decimal(
            raw.get("total_net_pnl_usdt"),
            f"{side} total_net_pnl_usdt",
        ),
    }


def _finite_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"walk-forward session-risk {field} must be finite")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"walk-forward session-risk {field} must be finite"
        ) from exc
    if not parsed.is_finite():
        raise ValueError(f"walk-forward session-risk {field} must be finite")
    return parsed


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"walk-forward session-risk {field} must be non-negative integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"walk-forward session-risk {field} must be non-negative integer"
        ) from exc
    if parsed < 0 or str(parsed) != str(value):
        raise ValueError(
            f"walk-forward session-risk {field} must be non-negative integer"
        )
    return parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit session-risk mechanism in crypto walk-forward summaries"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("walk-forward session-risk input must be an object")
    result = audit_walk_forward_session_risk(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("BYBIT_WALK_FORWARD_SESSION_RISK_AUDIT=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
