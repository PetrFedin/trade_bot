from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

_ZERO = Decimal("0")
_PNL_EPSILON_USDT = Decimal("0.000001")
_MFE_REFERENCE_LEVELS_R = (
    Decimal("0.5"),
    Decimal("1.0"),
    Decimal("1.5"),
    Decimal("2.0"),
)


def diagnose_crypto_replay_quality(report: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize realized edge, MFE preservation and downside by exit mode/symbol/side.

    The function is diagnostic only. It does not select a strategy, retune thresholds, or grant
    demo/live authority. Expected edge comes from the pre-entry ENTRY event; realized outcomes
    come only from closed trades.
    """

    closed = report.get("closed_trades")
    events = report.get("decision_events")
    if not isinstance(closed, list) or not isinstance(events, list):
        raise ValueError("crypto replay quality requires closed_trades and decision_events")

    entries = _entry_events(events)
    rows: list[dict[str, Any]] = []
    for trade in closed:
        if not isinstance(trade, Mapping):
            raise ValueError("crypto closed trade must be an object")
        key = (str(trade["symbol"]), str(trade["entry_time"]))
        entry = entries.get(key)
        expected_edge = (
            None
            if entry is None or entry.get("expected_net_edge_usd") is None
            else Decimal(str(entry["expected_net_edge_usd"]))
        )
        risk_budget = Decimal(str(trade["risk_budget_usdt"]))
        net_pnl = Decimal(str(trade["net_pnl_usdt"]))
        mfe_r = Decimal(str(trade["maximum_favorable_r_before_exit"]))
        mae_r = Decimal(str(trade["maximum_adverse_r_before_exit"]))
        realized_r = net_pnl / risk_budget if risk_budget > 0 else _ZERO
        positive_realized_r = max(_ZERO, realized_r)
        capture_ratio = (
            None
            if mfe_r <= 0
            else min(Decimal("1"), positive_realized_r / mfe_r)
        )
        giveback_r = max(_ZERO, mfe_r - positive_realized_r)
        rows.append(
            {
                "symbol": str(trade["symbol"]),
                "side": str(trade["side"]),
                "exit_reason": str(trade["exit_reason"]),
                "exit_mode": (
                    "UNKNOWN" if entry is None else str(entry.get("exit_mode") or "UNKNOWN")
                ),
                "net_pnl_usdt": net_pnl,
                "fees_usdt": Decimal(str(trade["fees_usdt"])),
                "risk_budget_usdt": risk_budget,
                "realized_net_r": realized_r,
                "mfe_r": mfe_r,
                "mae_r": mae_r,
                "mfe_capture_ratio": capture_ratio,
                "mfe_giveback_r": giveback_r,
                "expected_net_edge_usd": expected_edge,
                "edge_realization_ratio": (
                    None
                    if expected_edge is None or expected_edge <= 0
                    else net_pnl / expected_edge
                ),
            }
        )

    by_exit_mode = _group(rows, "exit_mode")
    by_exit_reason = _group(rows, "exit_reason")
    by_symbol = _group(rows, "symbol")
    by_side = _group(rows, "side")
    overall = _summarize(rows)
    return {
        "qualification": "CRYPTO_TRADE_QUALITY_DIAGNOSTIC_V2",
        "overall": overall,
        "by_exit_mode": by_exit_mode,
        "by_exit_reason": by_exit_reason,
        "by_symbol": by_symbol,
        "by_side": by_side,
        "trade_count": len(rows),
        "pnl_epsilon_usdt": float(_PNL_EPSILON_USDT),
        "profit_preservation_reference_levels_r": [
            float(value) for value in _MFE_REFERENCE_LEVELS_R
        ],
        "profit_preservation_breakpoints_are_diagnostic_only": True,
        "strategy_selection_allowed": False,
        "threshold_retuning_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
    }


def _entry_events(events: Iterable[object]) -> dict[tuple[str, str], Mapping[str, Any]]:
    entries: dict[tuple[str, str], Mapping[str, Any]] = {}
    for event in events:
        if not isinstance(event, Mapping) or event.get("event") != "ENTRY":
            continue
        symbol = event.get("symbol")
        execution_time = event.get("execution_time")
        if isinstance(symbol, str) and isinstance(execution_time, str):
            entries[(symbol, execution_time)] = event
    return entries


def _group(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[field])].append(row)
    return {key: _summarize(values) for key, values in sorted(grouped.items())}


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "trade_count": 0,
            "winning_trade_count": 0,
            "breakeven_trade_count": 0,
            "losing_trade_count": 0,
            "total_net_pnl_usdt": 0.0,
            "profit_factor": None,
            "average_net_pnl_usdt": None,
            "average_realized_net_r": None,
            "average_mfe_r": None,
            "average_mae_r": None,
            "average_mfe_capture_ratio": None,
            "average_mfe_giveback_r": None,
            "positive_mfe_lost_trade_count": 0,
            "positive_mfe_non_positive_close_fraction": None,
            "mfe_at_least_half_r_nonpositive_close_count": 0,
            "mfe_at_least_one_r_nonpositive_close_count": 0,
            "mfe_at_least_one_and_half_r_nonpositive_close_count": 0,
            "mfe_at_least_two_r_nonpositive_close_count": 0,
            "average_mfe_giveback_r_on_nonpositive_close": None,
            "average_edge_realization_ratio": None,
            "fees_usdt": 0.0,
        }

    positive = [
        row["net_pnl_usdt"]
        for row in rows
        if row["net_pnl_usdt"] > _PNL_EPSILON_USDT
    ]
    negative = [
        -row["net_pnl_usdt"]
        for row in rows
        if row["net_pnl_usdt"] < -_PNL_EPSILON_USDT
    ]
    breakeven = [
        row for row in rows if abs(row["net_pnl_usdt"]) <= _PNL_EPSILON_USDT
    ]
    captures = [
        row["mfe_capture_ratio"]
        for row in rows
        if row["mfe_capture_ratio"] is not None
    ]
    edge_realization = [
        row["edge_realization_ratio"]
        for row in rows
        if row["edge_realization_ratio"] is not None
    ]
    total_net = sum((row["net_pnl_usdt"] for row in rows), start=_ZERO)
    gross_profit = sum(positive, start=_ZERO)
    gross_loss = sum(negative, start=_ZERO)
    count = Decimal(len(rows))
    nonpositive = [
        row for row in rows if row["net_pnl_usdt"] <= _PNL_EPSILON_USDT
    ]
    positive_mfe_nonpositive = [row for row in nonpositive if row["mfe_r"] > 0]
    nonpositive_giveback = [row["mfe_giveback_r"] for row in nonpositive]
    return {
        "trade_count": len(rows),
        "winning_trade_count": len(positive),
        "breakeven_trade_count": len(breakeven),
        "losing_trade_count": len(negative),
        "total_net_pnl_usdt": float(total_net),
        "profit_factor": (
            None if gross_loss == 0 else float(gross_profit / gross_loss)
        ),
        "average_net_pnl_usdt": float(total_net / count),
        "average_realized_net_r": float(
            sum((row["realized_net_r"] for row in rows), start=_ZERO) / count
        ),
        "average_mfe_r": float(
            sum((row["mfe_r"] for row in rows), start=_ZERO) / count
        ),
        "average_mae_r": float(
            sum((row["mae_r"] for row in rows), start=_ZERO) / count
        ),
        "average_mfe_capture_ratio": (
            None
            if not captures
            else float(sum(captures, start=_ZERO) / Decimal(len(captures)))
        ),
        "average_mfe_giveback_r": float(
            sum((row["mfe_giveback_r"] for row in rows), start=_ZERO) / count
        ),
        "positive_mfe_lost_trade_count": len(positive_mfe_nonpositive),
        "positive_mfe_non_positive_close_fraction": (
            None
            if not nonpositive
            else float(Decimal(len(positive_mfe_nonpositive)) / Decimal(len(nonpositive)))
        ),
        "mfe_at_least_half_r_nonpositive_close_count": _mfe_nonpositive_count(
            nonpositive,
            minimum_mfe_r=Decimal("0.5"),
        ),
        "mfe_at_least_one_r_nonpositive_close_count": _mfe_nonpositive_count(
            nonpositive,
            minimum_mfe_r=Decimal("1.0"),
        ),
        "mfe_at_least_one_and_half_r_nonpositive_close_count": _mfe_nonpositive_count(
            nonpositive,
            minimum_mfe_r=Decimal("1.5"),
        ),
        "mfe_at_least_two_r_nonpositive_close_count": _mfe_nonpositive_count(
            nonpositive,
            minimum_mfe_r=Decimal("2.0"),
        ),
        "average_mfe_giveback_r_on_nonpositive_close": (
            None
            if not nonpositive_giveback
            else float(
                sum(nonpositive_giveback, start=_ZERO)
                / Decimal(len(nonpositive_giveback))
            )
        ),
        "average_edge_realization_ratio": (
            None
            if not edge_realization
            else float(
                sum(edge_realization, start=_ZERO) / Decimal(len(edge_realization))
            )
        ),
        "fees_usdt": float(
            sum((row["fees_usdt"] for row in rows), start=_ZERO)
        ),
    }


def _mfe_nonpositive_count(
    rows: Iterable[Mapping[str, Any]],
    *,
    minimum_mfe_r: Decimal,
) -> int:
    return sum(1 for row in rows if row["mfe_r"] >= minimum_mfe_r)
