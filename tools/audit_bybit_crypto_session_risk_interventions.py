from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_ZERO = Decimal("0")
_SESSION_LATCH = "SESSION_RISK_LATCHED"
_SESSION_ENTRY_BLOCK = "SESSION_RISK_ENTRY_BLOCK"
_SESSION_FLATTEN = "SESSION_RISK_FLATTEN"


def audit_session_risk_interventions(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Explain session-risk interventions without retuning the frozen thresholds."""

    session = candidate.get("session_risk")
    if not isinstance(session, Mapping) or session.get("enabled") is not True:
        raise ValueError("session-risk intervention audit requires an enabled session overlay")
    events = candidate.get("decision_events")
    if not isinstance(events, list):
        raise ValueError("session-risk candidate decision_events must be an array")

    latches = [event for event in events if _event_is(event, _SESSION_LATCH)]
    entry_blocks = [event for event in events if _event_is(event, _SESSION_ENTRY_BLOCK)]
    flatten_exits = [
        event
        for event in events
        if _event_is(event, "EXIT") and event.get("exit_reason") == _SESSION_FLATTEN
    ]

    latch_reasons: Counter[str] = Counter()
    latch_actions: Counter[str] = Counter()
    latch_drawdowns_pct: list[Decimal] = []
    latch_realized_pnl: list[Decimal] = []
    latch_execution_costs: list[Decimal] = []
    latch_loss_streaks: list[int] = []
    flatten_requested_count = 0
    for event in latches:
        reasons = _strings(event.get("reasons"), field="latch reasons")
        latch_reasons.update(reasons)
        action = _required_text(event.get("action"), field="latch action")
        latch_actions[action] += 1
        if event.get("flatten_at_next_open") is True:
            flatten_requested_count += 1
        current = _finite_decimal(event.get("current_equity_usdt"), "current_equity_usdt")
        peak = _positive_decimal(event.get("peak_equity_usdt"), "peak_equity_usdt")
        if current < 0 or current > peak:
            raise ValueError("session-risk latch equity state is invalid")
        latch_drawdowns_pct.append((peak - current) / peak * Decimal("100"))
        latch_realized_pnl.append(
            _finite_decimal(event.get("realized_pnl_usdt"), "realized_pnl_usdt")
        )
        cost = _finite_decimal(event.get("execution_cost_usdt"), "execution_cost_usdt")
        if cost < 0:
            raise ValueError("session-risk execution cost cannot be negative")
        latch_execution_costs.append(cost)
        streak = _non_negative_int(event.get("consecutive_losses"), "consecutive_losses")
        latch_loss_streaks.append(streak)

    block_side_counts: Counter[str] = Counter()
    block_symbol_counts: Counter[str] = Counter()
    block_reason_counts: Counter[str] = Counter()
    for event in entry_blocks:
        block_side_counts[_required_text(event.get("side"), field="entry-block side").upper()] += 1
        block_symbol_counts[_required_text(event.get("symbol"), field="entry-block symbol")] += 1
        block_reason_counts.update(
            _strings(event.get("latched_reasons"), field="entry-block latched reasons")
        )

    flatten_side_counts: Counter[str] = Counter()
    flatten_symbol_counts: Counter[str] = Counter()
    flatten_net_pnls: list[Decimal] = []
    for event in flatten_exits:
        flatten_side_counts[_required_text(event.get("side"), field="flatten side").upper()] += 1
        flatten_symbol_counts[_required_text(event.get("symbol"), field="flatten symbol")] += 1
        flatten_net_pnls.append(
            _finite_decimal(event.get("net_pnl_usdt"), "flatten net_pnl_usdt")
        )

    reported_reasons = session.get("reason_counts")
    if not isinstance(reported_reasons, Mapping):
        raise ValueError("session-risk reason_counts must be an object")
    normalized_reported_reasons = {
        str(key): _non_negative_int(value, f"reported reason {key}")
        for key, value in reported_reasons.items()
    }
    _assert_count_consistency(
        actual=len(latches),
        reported=session.get("risk_event_count"),
        field="risk_event_count",
    )
    _assert_count_consistency(
        actual=len(entry_blocks),
        reported=session.get("entry_block_count"),
        field="entry_block_count",
    )
    _assert_count_consistency(
        actual=len(flatten_exits),
        reported=session.get("flatten_trade_count"),
        field="flatten_trade_count",
    )
    if dict(latch_reasons) != normalized_reported_reasons:
        raise ValueError("session-risk event reasons do not match reported reason_counts")

    flatten_total = sum(flatten_net_pnls, start=_ZERO)
    return {
        "qualification": "CRYPTO_SESSION_RISK_INTERVENTION_DIAGNOSTIC",
        "latch_event_count": len(latches),
        "flatten_requested_latch_count": flatten_requested_count,
        "entry_block_event_count": len(entry_blocks),
        "flatten_exit_event_count": len(flatten_exits),
        "latch_reason_counts": dict(sorted(latch_reasons.items())),
        "latch_action_counts": dict(sorted(latch_actions.items())),
        "entry_block_side_counts": dict(sorted(block_side_counts.items())),
        "entry_block_symbol_counts": dict(sorted(block_symbol_counts.items())),
        "entry_block_latched_reason_counts": dict(sorted(block_reason_counts.items())),
        "flatten_side_counts": dict(sorted(flatten_side_counts.items())),
        "flatten_symbol_counts": dict(sorted(flatten_symbol_counts.items())),
        "flatten_total_net_pnl_usdt": float(flatten_total),
        "flatten_positive_count": sum(value > 0 for value in flatten_net_pnls),
        "flatten_non_positive_count": sum(value <= 0 for value in flatten_net_pnls),
        "latch_drawdown_pct": _distribution(latch_drawdowns_pct),
        "latch_realized_pnl_usdt": _distribution(latch_realized_pnl),
        "latch_execution_cost_usdt": _distribution(latch_execution_costs),
        "latch_consecutive_losses": _integer_distribution(latch_loss_streaks),
        "session_threshold_retuning_allowed": False,
        "automatic_strategy_activation_allowed": False,
        "strategy_promotion_allowed": False,
        "live_promotion_allowed": False,
    }


def audit_report_session_risk_interventions(report: Mapping[str, Any]) -> dict[str, Any]:
    candidates = report.get("strategy_shadow_candidates")
    if not isinstance(candidates, Mapping):
        raise ValueError("session-risk report strategy_shadow_candidates are missing")

    baseline = candidates.get("MIN_20_NET_EDGE_CONDITIONAL_RUNNER_1_5X")
    baseline_metrics = _metrics(baseline) if isinstance(baseline, Mapping) else None
    audited: dict[str, Any] = {}
    for name, candidate in candidates.items():
        if not isinstance(name, str) or not isinstance(candidate, Mapping):
            raise ValueError("session-risk candidate mapping is invalid")
        session = candidate.get("session_risk")
        if not isinstance(session, Mapping) or session.get("enabled") is not True:
            continue
        result = audit_session_risk_interventions(candidate)
        candidate_metrics = _metrics(candidate)
        result["metrics"] = candidate_metrics
        result["vs_conditional_baseline"] = (
            None
            if baseline_metrics is None
            else _metric_delta(candidate_metrics, baseline_metrics)
        )
        audited[name] = result

    if not audited:
        raise ValueError("session-risk report has no enabled session-risk candidates")
    return {
        "qualification": "CRYPTO_SESSION_RISK_INTERVENTION_AUDIT",
        "source": report.get("source"),
        "archive_dates": report.get("archive_dates"),
        "candidates": audited,
        "attribution_is_observational_not_causal": True,
        "session_threshold_retuning_allowed": False,
        "automatic_strategy_activation_allowed": False,
        "strategy_promotion_allowed": False,
        "live_promotion_allowed": False,
    }


def _metrics(candidate: Mapping[str, Any]) -> dict[str, Decimal | int | None]:
    raw = candidate.get("metrics")
    if not isinstance(raw, Mapping):
        raise ValueError("session-risk candidate metrics are missing")
    return {
        "closed_trade_count": _non_negative_int(raw.get("closed_trade_count"), "closed_trade_count"),
        "total_net_pnl_usdt": _finite_decimal(raw.get("total_net_pnl_usdt"), "total_net_pnl_usdt"),
        "profit_factor": _optional_decimal(raw.get("profit_factor"), "profit_factor"),
        "maximum_drawdown_pct": _finite_decimal(raw.get("maximum_drawdown_pct"), "maximum_drawdown_pct"),
        "fees_usdt": _finite_decimal(raw.get("fees_usdt"), "fees_usdt"),
        "risk_budget_breach_count": _non_negative_int(
            raw.get("risk_budget_breach_count"), "risk_budget_breach_count"
        ),
    }


def _metric_delta(
    candidate: Mapping[str, Decimal | int | None],
    baseline: Mapping[str, Decimal | int | None],
) -> dict[str, float | int | None | bool]:
    candidate_pf = candidate["profit_factor"]
    baseline_pf = baseline["profit_factor"]
    return {
        "closed_trade_delta": int(candidate["closed_trade_count"]) - int(baseline["closed_trade_count"]),
        "total_net_pnl_delta_usdt": float(
            Decimal(candidate["total_net_pnl_usdt"]) - Decimal(baseline["total_net_pnl_usdt"])
        ),
        "profit_factor_delta": (
            None
            if candidate_pf is None or baseline_pf is None
            else float(Decimal(candidate_pf) - Decimal(baseline_pf))
        ),
        "maximum_drawdown_delta_pct": float(
            Decimal(candidate["maximum_drawdown_pct"])
            - Decimal(baseline["maximum_drawdown_pct"])
        ),
        "fees_delta_usdt": float(Decimal(candidate["fees_usdt"]) - Decimal(baseline["fees_usdt"])),
        "risk_budget_breach_delta": int(candidate["risk_budget_breach_count"])
        - int(baseline["risk_budget_breach_count"]),
        "causal_attribution_allowed": False,
    }


def _event_is(value: object, event_name: str) -> bool:
    return isinstance(value, Mapping) and value.get("event") == event_name


def _strings(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"session-risk {field} must be an array of non-empty strings")
    return tuple(value)


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"session-risk {field} must be non-empty text")
    return value


def _finite_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"session-risk {field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"session-risk {field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"session-risk {field} must be a finite decimal")
    return parsed


def _positive_decimal(value: object, field: str) -> Decimal:
    parsed = _finite_decimal(value, field)
    if parsed <= 0:
        raise ValueError(f"session-risk {field} must be positive")
    return parsed


def _optional_decimal(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    return _finite_decimal(value, field)


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"session-risk {field} must be a non-negative integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"session-risk {field} must be a non-negative integer") from exc
    if parsed < 0 or str(parsed) != str(value):
        raise ValueError(f"session-risk {field} must be a non-negative integer")
    return parsed


def _assert_count_consistency(*, actual: int, reported: object, field: str) -> None:
    parsed = _non_negative_int(reported, field)
    if actual != parsed:
        raise ValueError(f"session-risk {field} does not match decision events")


def _distribution(values: Sequence[Decimal]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "minimum": None, "maximum": None, "mean": None}
    return {
        "count": len(values),
        "minimum": float(min(values)),
        "maximum": float(max(values)),
        "mean": float(sum(values, start=_ZERO) / Decimal(len(values))),
    }


def _integer_distribution(values: Sequence[int]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "minimum": None, "maximum": None, "mean": None}
    return {
        "count": len(values),
        "minimum": min(values),
        "maximum": max(values),
        "mean": float(Decimal(sum(values)) / Decimal(len(values))),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit Bybit crypto session-risk interventions without threshold retuning"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("session-risk audit input must be an object")
    result = audit_report_session_risk_interventions(raw)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("BYBIT_SESSION_RISK_INTERVENTION_AUDIT=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
