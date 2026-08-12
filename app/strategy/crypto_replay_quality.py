from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

_PNL_EPSILON_USDT = Decimal("0.000001")


def normalize_crypto_replay_report(report: dict[str, Any]) -> dict[str, Any]:
    """Normalize reporting artifacts without changing raw simulated cash/equity.

    Decimal arithmetic can leave sub-micro-USDT residuals around exact breakeven. Those
    residuals are retained as raw values but classified as breakeven for metrics. Likewise,
    a position forcibly closed only because the historical replay ended is distinguished
    from a strategy MAX_HOLD exit when the holding-period limit was not actually reached.
    """

    normalized = deepcopy(report)
    last_completed_bar = str(normalized["last_completed_bar"])
    strategy = normalized.get("strategy", {})
    maximum_holding_bars = int(strategy.get("maximum_holding_bars", 0))

    _normalize_variant_map(
        normalized.get("variants", {}),
        last_completed_bar=last_completed_bar,
        maximum_holding_bars=maximum_holding_bars,
    )
    candidates = normalized.get("notional_cap_shadow_candidates", {})
    if isinstance(candidates, dict):
        for candidate in candidates.values():
            if not isinstance(candidate, dict):
                continue
            _normalize_variant_map(
                candidate.get("variants", {}),
                last_completed_bar=last_completed_bar,
                maximum_holding_bars=maximum_holding_bars,
            )

    strategy_candidates = normalized.get("strategy_shadow_candidates", {})
    if isinstance(strategy_candidates, dict):
        for candidate in strategy_candidates.values():
            if not isinstance(candidate, dict):
                continue
            candidate_strategy = candidate.get("strategy", {})
            candidate_max_hold = maximum_holding_bars
            if isinstance(candidate_strategy, dict):
                candidate_max_hold = int(
                    candidate_strategy.get("maximum_holding_bars", maximum_holding_bars)
                )
            _normalize_variant(
                candidate,
                last_completed_bar=last_completed_bar,
                maximum_holding_bars=candidate_max_hold,
            )

    normalized["replay_quality_normalization"] = {
        "pnl_epsilon_usdt": float(_PNL_EPSILON_USDT),
        "raw_trade_pnl_preserved": True,
        "breakeven_residuals_classified_neutral": True,
        "terminal_forced_closes_distinguished_from_max_hold": True,
        "strategy_shadow_candidates_normalized": True,
    }
    return normalized


def _normalize_variant_map(
    variants: object,
    *,
    last_completed_bar: str,
    maximum_holding_bars: int,
) -> None:
    if not isinstance(variants, dict):
        return
    for variant in variants.values():
        if isinstance(variant, dict):
            _normalize_variant(
                variant,
                last_completed_bar=last_completed_bar,
                maximum_holding_bars=maximum_holding_bars,
            )


def _normalize_variant(
    variant: dict[str, Any],
    *,
    last_completed_bar: str,
    maximum_holding_bars: int,
) -> None:
    trades = variant.get("closed_trades", [])
    metrics = variant.get("metrics")
    if not isinstance(trades, list) or not isinstance(metrics, dict):
        return
    _normalize_trades(
        trades,
        last_completed_bar=last_completed_bar,
        maximum_holding_bars=maximum_holding_bars,
    )
    _normalize_metrics(metrics, trades)


def _normalize_trades(
    trades: list[object],
    *,
    last_completed_bar: str,
    maximum_holding_bars: int,
) -> None:
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        raw_pnl = Decimal(str(trade.get("net_pnl_usdt", "0")))
        pnl_class = _pnl_class(raw_pnl)
        trade["pnl_class"] = pnl_class
        trade["normalized_net_pnl_usdt"] = (
            0.0 if pnl_class == "BREAKEVEN" else float(raw_pnl)
        )
        raw_reason = str(trade.get("exit_reason", ""))
        normalized_reason = raw_reason
        holding_bars = int(trade.get("holding_bars", 0))
        if (
            raw_reason == "MAX_HOLD"
            and str(trade.get("exit_time", "")) == last_completed_bar
            and maximum_holding_bars > 0
            and holding_bars < maximum_holding_bars
        ):
            normalized_reason = "END_OF_REPLAY"
        trade["normalized_exit_reason"] = normalized_reason


def _normalize_metrics(metrics: dict[str, Any], trades: list[object]) -> None:
    valid_trades = [trade for trade in trades if isinstance(trade, dict)]
    pnl_values = [Decimal(str(trade.get("net_pnl_usdt", "0"))) for trade in valid_trades]
    wins = [value for value in pnl_values if value > _PNL_EPSILON_USDT]
    losses = [value for value in pnl_values if value < -_PNL_EPSILON_USDT]
    breakevens = [value for value in pnl_values if abs(value) <= _PNL_EPSILON_USDT]
    gross_profit = sum(wins, start=Decimal("0"))
    gross_loss = -sum(losses, start=Decimal("0"))
    profit_factor = gross_profit / gross_loss if gross_loss > _PNL_EPSILON_USDT else None
    decisive_count = len(wins) + len(losses)

    metrics["win_count"] = len(wins)
    metrics["loss_count"] = len(losses)
    metrics["breakeven_count"] = len(breakevens)
    metrics["win_rate"] = len(wins) / len(valid_trades) if valid_trades else 0.0
    metrics["decisive_win_rate"] = len(wins) / decisive_count if decisive_count else None
    metrics["profit_factor"] = float(profit_factor) if profit_factor is not None else None
    metrics["end_of_replay_exit_count"] = sum(
        trade.get("normalized_exit_reason") == "END_OF_REPLAY" for trade in valid_trades
    )
    metrics["max_hold_exit_count"] = sum(
        trade.get("normalized_exit_reason") == "MAX_HOLD" for trade in valid_trades
    )
    metrics["breakeven_epsilon_usdt"] = float(_PNL_EPSILON_USDT)

    target_or_better = 0
    for trade in valid_trades:
        net_pnl = Decimal(str(trade.get("net_pnl_usdt", "0")))
        target = Decimal(str(trade.get("target_net_profit_usd", "0")))
        if net_pnl + _PNL_EPSILON_USDT >= target:
            target_or_better += 1
    metrics["realized_target_or_better_count"] = target_or_better


def _pnl_class(value: Decimal) -> str:
    if value > _PNL_EPSILON_USDT:
        return "WIN"
    if value < -_PNL_EPSILON_USDT:
        return "LOSS"
    return "BREAKEVEN"
