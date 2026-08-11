from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.backtest import BacktestConfig
from app.strategy.ohlcv_managed_backtest import (
    OhlcvExitReason,
    OhlcvManagedBacktestResult,
    OhlcvManagedHistoricalBacktester,
)
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.regime_momentum import RegimeAwareMomentumStrategy

_SCHEMA = "ohlcv-exit-contract-v1"
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


def load_contract(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA:
        raise ValueError("OHLCV exit contract schema mismatch")
    if data.get("shadow_only") is not True:
        raise ValueError("OHLCV exit contract must remain shadow-only")
    if data.get("strategy_promotion_allowed") is not False:
        raise ValueError("OHLCV exit contract cannot allow strategy promotion")
    if data.get("decision_execution") != "PRIOR_COMPLETED_CLOSE_TO_NEXT_OPEN":
        raise ValueError("OHLCV decision execution semantics changed")
    if data.get("intrabar_path_policy") != "PROTECTIVE_EXIT_FIRST_ON_AMBIGUITY":
        raise ValueError("OHLCV ambiguity policy changed")
    if data.get("trailing_peak_policy") != "COMPLETED_PRIOR_BARS_ONLY":
        raise ValueError("OHLCV trailing peak policy changed")
    if data.get("gap_stop_policy") != "OPEN_IF_GAPPED_THROUGH_STOP":
        raise ValueError("OHLCV gap stop policy changed")
    if data.get("take_profit_gap_policy") != "CONSERVATIVE_TARGET_PRICE":
        raise ValueError("OHLCV take-profit gap policy changed")
    required = data.get("required_contract_scenarios")
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ValueError("required_contract_scenarios must be a string list")
    blockers = data.get("promotion_blockers")
    if not isinstance(blockers, list) or not all(isinstance(item, str) for item in blockers):
        raise ValueError("promotion_blockers must be a string list")
    policy = _object(data, "position_management")
    _decimal(policy, "stop_loss_fraction")
    _decimal(policy, "take_profit_fraction")
    _decimal(policy, "trailing_activation_fraction")
    _decimal(policy, "trailing_stop_fraction")
    _positive_int(policy, "maximum_holding_bars")
    return data


def _policy(contract: dict[str, Any]) -> PositionManagementPolicy:
    data = _object(contract, "position_management")
    return PositionManagementPolicy(
        stop_loss_fraction=_decimal(data, "stop_loss_fraction"),
        take_profit_fraction=_decimal(data, "take_profit_fraction"),
        trailing_activation_fraction=_decimal(data, "trailing_activation_fraction"),
        trailing_stop_fraction=_decimal(data, "trailing_stop_fraction"),
        maximum_holding_bars=_positive_int(data, "maximum_holding_bars"),
    )


def _bar(
    index: int,
    close: str,
    *,
    open: str | None = None,
    high: str | None = None,
    low: str | None = None,
) -> OhlcvBar:
    close_value = Decimal(close)
    open_value = close_value if open is None else Decimal(open)
    high_value = max(open_value, close_value) + Decimal("0.1") if high is None else Decimal(high)
    low_value = min(open_value, close_value) - Decimal("0.1") if low is None else Decimal(low)
    return OhlcvBar(
        symbol="AAPL",
        timestamp=_START + timedelta(days=index),
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=1000 + index,
        trade_count=100 + index,
        vwap=close_value,
    )


def _history() -> list[OhlcvBar]:
    return [_bar(index, str(100 + index)) for index in range(8)]


def _backtester(contract: dict[str, Any]) -> OhlcvManagedHistoricalBacktester:
    return OhlcvManagedHistoricalBacktester(
        strategy=RegimeAwareMomentumStrategy(),
        position_policy=_policy(contract),
        config=BacktestConfig(
            opening_cash=Decimal("10000"),
            fee_per_fill=Decimal("0"),
            slippage_bps=Decimal("0"),
        ),
    )


def _evidence(result: OhlcvManagedBacktestResult) -> dict[str, object]:
    if result.closed_trade_count != 1:
        raise ValueError("contract scenario must produce exactly one closed trade")
    trade = result.closed_trades[0]
    return {
        "fill_count": result.fill_count,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "total_pnl": str(result.total_pnl),
        "intrabar_exit_count": result.intrabar_exit_count,
        "ambiguous_intrabar_exit_count": result.ambiguous_intrabar_exit_count,
        "gap_stop_exit_count": result.gap_stop_exit_count,
        "entry_execution_price": str(trade.entry_execution_price),
        "exit_execution_price": str(trade.exit_execution_price),
        "net_pnl": str(trade.net_pnl),
        "holding_bars": trade.holding_bars,
        "exit_reason": trade.exit_reason.value,
        "ambiguous_intrabar_exit": trade.ambiguous_intrabar_exit,
        "gap_through_stop": trade.gap_through_stop,
    }


def qualify(contract_path: Path) -> dict[str, object]:
    contract = load_contract(contract_path)
    tester = _backtester(contract)
    scenarios = {
        "INTRABAR_TAKE_PROFIT": [
            *_history(),
            _bar(8, "112", open="108", high="113", low="107"),
        ],
        "AMBIGUOUS_STOP_AND_TAKE_PROTECTIVE_FIRST": [
            *_history(),
            _bar(8, "110", open="108", high="113", low="105"),
        ],
        "GAP_THROUGH_HARD_STOP": [
            *_history(),
            _bar(8, "108.5", open="108", high="109", low="107"),
            _bar(9, "101", open="100", high="102", low="99"),
        ],
        "TRAILING_FROM_COMPLETED_PRIOR_PEAK": [
            *_history(),
            _bar(8, "108.5", open="108", high="111", low="107"),
            _bar(9, "109.5", open="110", high="111", low="109"),
        ],
        "CURRENT_BAR_CLOSE_CANNOT_CHANGE_ENTRY_BEFORE_OPEN": [
            *_history(),
            _bar(8, "106", open="108", high="108.2", low="105"),
        ],
    }
    expected = {
        "INTRABAR_TAKE_PROFIT": OhlcvExitReason.INTRABAR_TAKE_PROFIT,
        "AMBIGUOUS_STOP_AND_TAKE_PROTECTIVE_FIRST": OhlcvExitReason.INTRABAR_HARD_STOP,
        "GAP_THROUGH_HARD_STOP": OhlcvExitReason.INTRABAR_HARD_STOP,
        "TRAILING_FROM_COMPLETED_PRIOR_PEAK": OhlcvExitReason.INTRABAR_TRAILING_STOP,
        "CURRENT_BAR_CLOSE_CANNOT_CHANGE_ENTRY_BEFORE_OPEN": OhlcvExitReason.INTRABAR_HARD_STOP,
    }
    required = set(contract["required_contract_scenarios"])
    if required != set(scenarios):
        raise ValueError("predeclared OHLCV scenario set mismatch")

    evidence: dict[str, dict[str, object]] = {}
    for name, bars in scenarios.items():
        result = tester.run(bars)
        item = _evidence(result)
        if item["exit_reason"] != expected[name].value:
            raise ValueError(f"OHLCV contract exit mismatch:{name}")
        evidence[name] = item

    if evidence["AMBIGUOUS_STOP_AND_TAKE_PROTECTIVE_FIRST"]["ambiguous_intrabar_exit"] is not True:
        raise ValueError("ambiguous OHLCV contract scenario was not marked ambiguous")
    if evidence["GAP_THROUGH_HARD_STOP"]["gap_through_stop"] is not True:
        raise ValueError("gap OHLCV contract scenario did not use gap semantics")
    if evidence["CURRENT_BAR_CLOSE_CANNOT_CHANGE_ENTRY_BEFORE_OPEN"]["entry_execution_price"] != "108":
        raise ValueError("current bar changed entry before next-open execution")

    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_SYNTHETIC_CONTRACT",
        "shadow_only": True,
        "strategy_promotion_allowed": False,
        "real_ohlcv_evidence": False,
        "multisymbol_portfolio_evidence": False,
        "contract": contract,
        "promotion_blockers": list(contract["promotion_blockers"]),
        "scenarios": evidence,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qualify conservative OHLCV exit semantics")
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    evidence = qualify(args.contract)
    payload = json.dumps(evidence, indent=2, sort_keys=True)
    print(payload)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
