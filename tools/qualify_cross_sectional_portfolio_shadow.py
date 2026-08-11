from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
    CrossSectionalPortfolioResult,
    PortfolioExitReason,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy
from app.strategy.regime_momentum import RegimeAwareMomentumConfig

_SCHEMA = "cross-sectional-portfolio-shadow-v1"
_START = datetime(2026, 1, 2, tzinfo=UTC)


def _object(data: dict[str, Any], field: str) -> dict[str, Any]:
    value = data.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _decimal(data: dict[str, Any], field: str) -> Decimal:
    value = data.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a decimal string")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def _positive_int(data: dict[str, Any], field: str) -> int:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def load_policy(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA:
        raise ValueError("cross-sectional portfolio policy schema mismatch")
    if data.get("shadow_only") is not True:
        raise ValueError("portfolio policy must remain shadow-only")
    if data.get("strategy_promotion_allowed") is not False:
        raise ValueError("portfolio policy cannot allow strategy promotion")
    if _positive_int(data, "minimum_universe_symbols") < 3:
        raise ValueError("portfolio policy requires at least three research symbols")

    selection = _object(data, "selection")
    top_k = _positive_int(selection, "top_k")
    if selection.get("ranking") != [
        "momentum_desc",
        "trend_strength_desc",
        "realized_volatility_asc",
        "symbol_asc",
    ]:
        raise ValueError("portfolio selection ranking changed")
    signal = _object(selection, "signal")
    for field in ("fast_bars", "slow_bars", "momentum_lookback_bars", "volatility_bars"):
        _positive_int(signal, field)
    for field in (
        "minimum_momentum_return",
        "minimum_trend_strength",
        "maximum_realized_volatility",
    ):
        _decimal(signal, field)

    portfolio = _object(data, "portfolio")
    maximum = _decimal(portfolio, "maximum_gross_exposure_fraction")
    target = _decimal(portfolio, "new_position_target_equity_fraction")
    target_gross = _decimal(portfolio, "target_gross_exposure_fraction")
    headroom = _decimal(portfolio, "gross_exposure_headroom_fraction")
    if target * Decimal(top_k) != target_gross:
        raise ValueError("portfolio target gross exposure mismatch")
    if maximum - target_gross != headroom:
        raise ValueError("portfolio gross exposure headroom mismatch")
    if portfolio.get("allow_leverage") is not False:
        raise ValueError("portfolio leverage must remain disabled")
    if portfolio.get("rebalance_existing_positions") is not False:
        raise ValueError("portfolio existing positions cannot be rebalanced")
    if portfolio.get("exposure_cap_enforcement") != (
        "BLOCK_NEW_ENTRY_NO_FORCED_DELEVERAGING"
    ):
        raise ValueError("portfolio exposure cap enforcement changed")
    if portfolio.get("execute_selection_changes_at_next_open") is not True:
        raise ValueError("selection changes must execute at next open")
    if portfolio.get("process_open_exits_before_new_entries") is not True:
        raise ValueError("portfolio exits must process before entries")

    position = _object(data, "position_management")
    for field in (
        "stop_loss_fraction",
        "take_profit_fraction",
        "trailing_activation_fraction",
        "trailing_stop_fraction",
    ):
        _decimal(position, field)
    _positive_int(position, "maximum_holding_bars")
    if position.get("intrabar_path_policy") != "PROTECTIVE_EXIT_FIRST_ON_AMBIGUITY":
        raise ValueError("portfolio intrabar ambiguity policy changed")
    if position.get("trailing_peak_policy") != "COMPLETED_PRIOR_BARS_ONLY":
        raise ValueError("portfolio trailing peak policy changed")
    if position.get("gap_stop_policy") != "OPEN_IF_GAPPED_THROUGH_STOP":
        raise ValueError("portfolio gap stop policy changed")

    reentry = _object(data, "reentry_confirmation")
    _positive_int(reentry, "minimum_consecutive_eligible_bars")
    if reentry.get("confirmation_basis") != "SELECTED_TOP_K":
        raise ValueError("portfolio re-entry confirmation basis changed")
    if reentry.get("initial_entry_requires_confirmation") is not False:
        raise ValueError("portfolio initial entry must remain immediate")
    if reentry.get("reset_streak_on_ineligible_signal") is not True:
        raise ValueError("portfolio re-entry streak must reset")
    if reentry.get("apply_after_any_exit") is not True:
        raise ValueError("portfolio re-entry must apply after any exit")

    required = data.get("required_contract_scenarios")
    if not isinstance(required, list) or set(required) != {
        "TOP_K_BOUNDED_EXPOSURE",
        "SYMBOL_SPECIFIC_INTRABAR_STOP_REENTRY_DELAY",
        "NEXT_OPEN_SELECTION_ROTATION",
    }:
        raise ValueError("portfolio contract scenario set changed")
    blockers = data.get("promotion_blockers")
    if not isinstance(blockers, list) or not all(isinstance(item, str) for item in blockers):
        raise ValueError("portfolio promotion blockers must be strings")
    return data


def _selector(policy: dict[str, Any]) -> CrossSectionalSelector:
    selection = _object(policy, "selection")
    signal = _object(selection, "signal")
    return CrossSectionalSelector(
        top_k=_positive_int(selection, "top_k"),
        signal_config=RegimeAwareMomentumConfig(
            fast_bars=_positive_int(signal, "fast_bars"),
            slow_bars=_positive_int(signal, "slow_bars"),
            momentum_lookback_bars=_positive_int(signal, "momentum_lookback_bars"),
            volatility_bars=_positive_int(signal, "volatility_bars"),
            minimum_momentum_return=_decimal(signal, "minimum_momentum_return"),
            minimum_trend_strength=_decimal(signal, "minimum_trend_strength"),
            maximum_realized_volatility=_decimal(signal, "maximum_realized_volatility"),
        ),
    )


def _portfolio_policy(policy: dict[str, Any]) -> CrossSectionalPortfolioPolicy:
    portfolio = _object(policy, "portfolio")
    return CrossSectionalPortfolioPolicy(
        opening_cash=_decimal(policy, "opening_cash"),
        fee_per_fill=_decimal(policy, "fee_per_fill"),
        slippage_bps=_decimal(policy, "slippage_bps"),
        maximum_gross_exposure_fraction=_decimal(
            portfolio, "maximum_gross_exposure_fraction"
        ),
        new_position_target_equity_fraction=_decimal(
            portfolio, "new_position_target_equity_fraction"
        ),
        allow_leverage=False,
        rebalance_existing_positions=False,
    )


def _position_policy(policy: dict[str, Any]) -> PositionManagementPolicy:
    position = _object(policy, "position_management")
    return PositionManagementPolicy(
        stop_loss_fraction=_decimal(position, "stop_loss_fraction"),
        take_profit_fraction=_decimal(position, "take_profit_fraction"),
        trailing_activation_fraction=_decimal(position, "trailing_activation_fraction"),
        trailing_stop_fraction=_decimal(position, "trailing_stop_fraction"),
        maximum_holding_bars=_positive_int(position, "maximum_holding_bars"),
    )


def _reentry_policy(policy: dict[str, Any]) -> ReentryConfirmationPolicy:
    reentry = _object(policy, "reentry_confirmation")
    return ReentryConfirmationPolicy(
        minimum_consecutive_eligible_bars=_positive_int(
            reentry, "minimum_consecutive_eligible_bars"
        ),
        initial_entry_requires_confirmation=False,
        reset_streak_on_ineligible_signal=True,
        apply_after_any_exit=True,
    )


def _series(
    symbol: str,
    closes: list[str],
    *,
    overrides: dict[int, tuple[str, str, str, str]] | None = None,
) -> list[OhlcvBar]:
    overrides = {} if overrides is None else overrides
    result: list[OhlcvBar] = []
    for index, close in enumerate(closes):
        if index in overrides:
            open_value, high_value, low_value, close_value = overrides[index]
        else:
            close_value = close
            open_value = close
            high_value = str(Decimal(close) + Decimal("0.2"))
            low_value = str(Decimal(close) - Decimal("0.2"))
        result.append(
            OhlcvBar(
                symbol=symbol,
                timestamp=_START + timedelta(days=index),
                open=Decimal(open_value),
                high=Decimal(high_value),
                low=Decimal(low_value),
                close=Decimal(close_value),
                volume=1000 + index,
                trade_count=100 + index,
                vwap=Decimal(close_value),
            )
        )
    return result


def _stable_universe(*, aapl_stop_on_entry: bool = False) -> list[OhlcvBar]:
    aapl_overrides = (
        {8: ("108", "108.2", "104", "108")}
        if aapl_stop_on_entry
        else None
    )
    return [
        *_series(
            "AAPL",
            ["100", "101", "102", "103", "104", "105", "106", "108", "108", "109", "110"],
            overrides=aapl_overrides,
        ),
        *_series(
            "MSFT",
            ["100", "100.5", "101", "101.5", "102", "102.5", "103", "104", "104.5", "105", "105.5"],
        ),
        *_series(
            "NVDA",
            ["107", "106", "105", "104", "103", "102", "101", "100", "99", "98", "97"],
        ),
    ]


def _rotation_universe() -> list[OhlcvBar]:
    return [
        *_series(
            "AAPL",
            ["100", "101", "102", "103", "104", "105", "106", "108", "106.5", "107"],
            overrides={8: ("108", "108.2", "106.4", "106.5")},
        ),
        *_series(
            "MSFT",
            ["100", "100.5", "101", "101.5", "102", "102.5", "103", "104", "105.5", "106"],
        ),
        *_series(
            "NVDA",
            ["100", "100.2", "100.4", "100.6", "100.8", "101", "101.2", "101.4", "102.8", "103.5"],
        ),
    ]


def _result_evidence(result: CrossSectionalPortfolioResult) -> dict[str, object]:
    return {
        "fill_count": result.fill_count,
        "closed_trade_count": result.closed_trade_count,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "gross_profit": str(result.gross_profit),
        "gross_loss": str(result.gross_loss),
        "profit_factor": None if result.profit_factor is None else str(result.profit_factor),
        "total_pnl": str(result.total_pnl),
        "total_return": str(result.total_return),
        "max_drawdown": str(result.max_drawdown),
        "max_drawdown_fraction": str(result.max_drawdown_fraction),
        "turnover_fraction": str(result.turnover_fraction),
        "fees_paid": str(result.fees_paid),
        "maximum_gross_exposure_fraction_observed": str(
            result.maximum_gross_exposure_fraction_observed
        ),
        "maximum_concurrent_positions": result.maximum_concurrent_positions,
        "one_bar_reentry_count": result.one_bar_reentry_count,
        "selection_counts": result.selection_counts,
        "realized_pnl_by_symbol": {
            symbol: str(value) for symbol, value in result.realized_pnl_by_symbol.items()
        },
        "intrabar_exit_counts": result.intrabar_exit_counts,
        "entry_block_counts": result.entry_block_counts,
        "final_quantities": {
            symbol: str(value) for symbol, value in result.final_quantities.items()
        },
        "closed_trades": [
            {
                "symbol": trade.symbol,
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": trade.exit_time.isoformat(),
                "entry_execution_price": str(trade.entry_execution_price),
                "exit_execution_price": str(trade.exit_execution_price),
                "quantity": str(trade.quantity),
                "net_pnl": str(trade.net_pnl),
                "holding_bars": trade.holding_bars,
                "exit_reason": trade.exit_reason.value,
                "ambiguous_intrabar_exit": trade.ambiguous_intrabar_exit,
                "gap_through_stop": trade.gap_through_stop,
            }
            for trade in result.closed_trades
        ],
        "decision_trace": [
            {
                "execution_index": item.execution_index,
                "decision_time": item.decision_time.isoformat(),
                "execution_time": item.execution_time.isoformat(),
                "selected_symbols": list(item.selected_symbols),
                "entered_symbols": list(item.entered_symbols),
                "open_exit_symbols": list(item.open_exit_symbols),
                "intrabar_exit_symbols": list(item.intrabar_exit_symbols),
                "blocked_entries": [
                    [symbol, reason.value] for symbol, reason in item.blocked_entries
                ],
                "equity_at_prior_close": str(item.equity_at_prior_close),
                "closing_equity": str(item.closing_equity),
                "closing_gross_exposure_fraction": str(
                    item.closing_gross_exposure_fraction
                ),
                "concurrent_positions": item.concurrent_positions,
            }
            for item in result.decision_trace
        ],
    }


def qualify(path: Path) -> dict[str, object]:
    policy = load_policy(path)
    selector = _selector(policy)
    portfolio_policy = _portfolio_policy(policy)
    position_policy = _position_policy(policy)
    reentry_policy = _reentry_policy(policy)

    stable = CrossSectionalPortfolioBacktester(
        selector=selector,
        portfolio_policy=portfolio_policy,
        position_policy=position_policy,
        reentry_policy=reentry_policy,
    ).run(_stable_universe())
    stopped = CrossSectionalPortfolioBacktester(
        selector=selector,
        portfolio_policy=portfolio_policy,
        position_policy=position_policy,
        reentry_policy=reentry_policy,
    ).run(_stable_universe(aapl_stop_on_entry=True))
    rotated = CrossSectionalPortfolioBacktester(
        selector=selector,
        portfolio_policy=portfolio_policy,
        position_policy=position_policy,
        reentry_policy=reentry_policy,
    ).run(_rotation_universe())

    if stable.decision_trace[0].selected_symbols != ("AAPL", "MSFT"):
        raise ValueError("stable scenario top-K selection drifted")
    if stable.maximum_concurrent_positions > selector.top_k:
        raise ValueError("portfolio exceeded top-K concurrent positions")
    if stable.maximum_gross_exposure_fraction_observed > Decimal("0.60"):
        raise ValueError("stable scenario exceeded gross exposure contract")

    first_stop = stopped.decision_trace[0]
    second_stop = stopped.decision_trace[1]
    third_stop = stopped.decision_trace[2]
    if first_stop.intrabar_exit_symbols != ("AAPL",):
        raise ValueError("stop scenario did not isolate AAPL intrabar exit")
    if [symbol for symbol, _ in second_stop.blocked_entries] != ["AAPL"]:
        raise ValueError("stop scenario did not delay AAPL re-entry")
    if "AAPL" not in third_stop.entered_symbols:
        raise ValueError("stop scenario did not release confirmed AAPL re-entry")
    if stopped.one_bar_reentry_count != 0:
        raise ValueError("stop scenario allowed one-bar re-entry")

    first_rotation = rotated.decision_trace[0]
    second_rotation = rotated.decision_trace[1]
    if first_rotation.selected_symbols != ("AAPL", "MSFT"):
        raise ValueError("rotation scenario initial top-K drifted")
    if second_rotation.selected_symbols != ("MSFT", "NVDA"):
        raise ValueError("rotation scenario next top-K drifted")
    if second_rotation.open_exit_symbols != ("AAPL",):
        raise ValueError("rotation scenario did not exit dropped AAPL")
    if second_rotation.entered_symbols != ("NVDA",):
        raise ValueError("rotation scenario did not enter promoted NVDA")
    if not any(
        trade.symbol == "AAPL" and trade.exit_reason is PortfolioExitReason.SELECTION_EXIT
        for trade in rotated.closed_trades
    ):
        raise ValueError("rotation scenario missing selection-exit trade")

    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_SYNTHETIC_PORTFOLIO_CONTRACT",
        "shadow_only": True,
        "strategy_promotion_allowed": False,
        "real_multisymbol_ohlcv_evidence": False,
        "walk_forward_portfolio_benchmark_evidence": False,
        "external_paper_portfolio_evidence": False,
        "predeclared_policy": policy,
        "promotion_blockers": list(policy["promotion_blockers"]),
        "scenarios": {
            "TOP_K_BOUNDED_EXPOSURE": _result_evidence(stable),
            "SYMBOL_SPECIFIC_INTRABAR_STOP_REENTRY_DELAY": _result_evidence(stopped),
            "NEXT_OPEN_SELECTION_ROTATION": _result_evidence(rotated),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Qualify the cross-sectional portfolio shadow contract"
    )
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    evidence = qualify(args.policy)
    payload = json.dumps(evidence, indent=2, sort_keys=True)
    print(payload)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
