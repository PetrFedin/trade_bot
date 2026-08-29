from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_Z_95 = 1.959963984540054


@dataclass(frozen=True)
class CryptoSignalPatternHoldoutPolicy:
    minimum_discovery_trades: int = 5
    minimum_holdout_trades: int = 5
    minimum_discovery_symbols: int = 2
    minimum_holdout_symbols: int = 2

    def validate(self) -> None:
        for name, value in (
            ("minimum_discovery_trades", self.minimum_discovery_trades),
            ("minimum_holdout_trades", self.minimum_holdout_trades),
            ("minimum_discovery_symbols", self.minimum_discovery_symbols),
            ("minimum_holdout_symbols", self.minimum_holdout_symbols),
        ):
            if not 1 <= value <= 100_000:
                raise ValueError(f"signal pattern holdout {name} is invalid")


def validate_crypto_signal_pattern_holdout(
    discovery_trade_rows: Sequence[Mapping[str, Any]],
    holdout_trade_rows: Sequence[Mapping[str, Any]],
    *,
    policy: CryptoSignalPatternHoldoutPolicy | None = None,
) -> dict[str, Any]:
    """Validate frozen historical patterns on a non-overlapping holdout population.

    Candidate discovery uses only the declared pattern key already present in the trade rows.
    No new indicator threshold, quality cut or ranking weight is fitted in this function.
    """

    active = CryptoSignalPatternHoldoutPolicy() if policy is None else policy
    active.validate()
    discovery = _group_trade_rows(discovery_trade_rows)
    holdout = _group_trade_rows(holdout_trade_rows)

    candidates: list[dict[str, Any]] = []
    all_patterns = sorted(set(discovery) | set(holdout))
    for pattern in all_patterns:
        discovery_summary = _summarize(discovery.get(pattern, ()))
        holdout_summary = _summarize(holdout.get(pattern, ()))
        discovery_supported = (
            discovery_summary["trade_count"] >= active.minimum_discovery_trades
            and discovery_summary["symbol_count"] >= active.minimum_discovery_symbols
        )
        discovery_perfect_positive = (
            discovery_supported
            and discovery_summary["positive_close_count"]
            == discovery_summary["trade_count"]
        )
        discovery_perfect_planned = (
            discovery_supported
            and discovery_summary["planned_profit_exit_count"]
            == discovery_summary["trade_count"]
        )
        if not discovery_perfect_positive and not discovery_perfect_planned:
            continue

        holdout_supported = (
            holdout_summary["trade_count"] >= active.minimum_holdout_trades
            and holdout_summary["symbol_count"] >= active.minimum_holdout_symbols
        )
        holdout_perfect_positive = (
            holdout_supported
            and holdout_summary["positive_close_count"] == holdout_summary["trade_count"]
        )
        holdout_perfect_planned = (
            holdout_supported
            and holdout_summary["planned_profit_exit_count"] == holdout_summary["trade_count"]
        )
        if not holdout_supported:
            status = "HOLDOUT_INSUFFICIENT_SUPPORT"
        elif discovery_perfect_planned and holdout_perfect_planned:
            status = "OBSERVED_HOLDOUT_PERFECT_PLANNED_PROFIT"
        elif discovery_perfect_positive and holdout_perfect_positive:
            status = "OBSERVED_HOLDOUT_PERFECT_POSITIVE"
        else:
            status = "HOLDOUT_BROKE_PERFECT_HISTORY"

        candidates.append(
            {
                "pattern": pattern,
                "status": status,
                "discovery_perfect_positive": discovery_perfect_positive,
                "discovery_perfect_planned_profit": discovery_perfect_planned,
                "holdout_supported": holdout_supported,
                "holdout_perfect_positive": holdout_perfect_positive,
                "holdout_perfect_planned_profit": holdout_perfect_planned,
                "discovery": discovery_summary,
                "holdout": holdout_summary,
            }
        )

    passed = [
        row
        for row in candidates
        if row["status"]
        in {
            "OBSERVED_HOLDOUT_PERFECT_PLANNED_PROFIT",
            "OBSERVED_HOLDOUT_PERFECT_POSITIVE",
        }
    ]
    return {
        "validation": "BYBIT_CRYPTO_SIGNAL_PATTERN_HOLDOUT_V1",
        "discovery_trade_count": len(discovery_trade_rows),
        "holdout_trade_count": len(holdout_trade_rows),
        "discovery_pattern_count": len(discovery),
        "holdout_pattern_count": len(holdout),
        "candidate_count": len(candidates),
        "observed_holdout_perfect_count": len(passed),
        "candidates": candidates,
        "observed_holdout_perfect_patterns": passed,
        "minimum_discovery_trades": active.minimum_discovery_trades,
        "minimum_holdout_trades": active.minimum_holdout_trades,
        "minimum_discovery_symbols": active.minimum_discovery_symbols,
        "minimum_holdout_symbols": active.minimum_holdout_symbols,
        "pattern_thresholds_fitted": False,
        "quality_threshold_retuned": False,
        "ranking_weights_changed": False,
        "strategy_parameters_changed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "prospective_confirmation_required": True,
        "predictive_guarantee_allowed": False,
    }


def _group_trade_rows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        pattern = _required_text(row, "pattern")
        symbol = _required_text(row, "symbol")
        decision_time = _required_text(row, "decision_time")
        side = _required_text(row, "side")
        if side not in {"LONG", "SHORT"}:
            raise ValueError("signal pattern holdout row side is invalid")
        key = (symbol, side, decision_time)
        if key in seen:
            raise ValueError("signal pattern holdout duplicate trade identity")
        seen.add(key)
        positive = row.get("positive_close")
        planned = row.get("planned_profit_exit")
        if not isinstance(positive, bool) or not isinstance(planned, bool):
            raise ValueError("signal pattern holdout requires boolean outcome flags")
        grouped[pattern].append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    symbols = sorted({_required_text(row, "symbol") for row in rows})
    positive = sum(bool(row["positive_close"]) for row in rows)
    planned = sum(bool(row["planned_profit_exit"]) for row in rows)
    pnl_values = [_required_number(row, "net_pnl_usdt") for row in rows]
    exits = Counter(_required_text(row, "exit_reason") for row in rows)
    return {
        "trade_count": count,
        "symbol_count": len(symbols),
        "symbols": symbols,
        "positive_close_count": positive,
        "positive_close_rate": None if count == 0 else positive / count,
        "positive_rate_wilson_lower_95": _wilson_lower(positive, count),
        "planned_profit_exit_count": planned,
        "planned_profit_exit_rate": None if count == 0 else planned / count,
        "planned_profit_rate_wilson_lower_95": _wilson_lower(planned, count),
        "total_net_pnl_usdt": sum(pnl_values),
        "exit_reason_counts": dict(sorted(exits.items())),
    }


def _wilson_lower(successes: int, total: int) -> float | None:
    if total == 0:
        return None
    p = successes / total
    z2 = _Z_95 * _Z_95
    denominator = 1.0 + z2 / total
    center = p + z2 / (2.0 * total)
    margin = _Z_95 * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return max(0.0, (center - margin) / denominator)


def _required_text(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"signal pattern holdout requires text {key}")
    return value


def _required_number(row: Mapping[str, Any], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"signal pattern holdout requires numeric {key}")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"signal pattern holdout {key} must be finite")
    return numeric


__all__ = [
    "CryptoSignalPatternHoldoutPolicy",
    "validate_crypto_signal_pattern_holdout",
]
