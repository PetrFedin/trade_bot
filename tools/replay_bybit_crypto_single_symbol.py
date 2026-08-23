from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import FunctionType
from typing import Any

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from tools import replay_bybit_crypto_runner as runner_module


def replay_open_ended_crypto_runner_single_symbol(
    acquisition: BybitKlineAcquisition,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run the canonical runner for exactly one symbol without changing its control flow.

    The canonical runner historically obtains its timeline through a portfolio-only helper that
    rejects fewer than two symbols. Full-period research needs an independent per-symbol replay,
    but duplicating the runner or inventing a synthetic second symbol would change semantics.

    This adapter executes the same runner function code object with an isolated globals mapping in
    which only the common-timestamp resolver is replaced by a one-symbol resolver. The canonical
    module globals are never mutated, so normal portfolio replays keep their original contract.
    """

    symbols = tuple(sorted({bar.symbol for bar in acquisition.bars}))
    if len(symbols) != 1:
        raise ValueError("single-symbol crypto replay requires exactly one symbol")
    symbol = symbols[0]

    original = runner_module.replay_open_ended_crypto_runner
    isolated_globals = dict(original.__globals__)
    isolated_globals["_common_timestamps"] = _single_symbol_timestamps
    isolated = FunctionType(
        original.__code__,
        isolated_globals,
        name=original.__name__,
        argdefs=original.__defaults__,
        closure=original.__closure__,
    )
    isolated.__kwdefaults__ = original.__kwdefaults__
    result = isolated(acquisition, **kwargs)
    if not isinstance(result, dict):
        raise TypeError("single-symbol crypto replay returned an invalid payload")
    if _payload_symbols(result) - {symbol}:
        raise ValueError("single-symbol crypto replay emitted another symbol")
    return result


def _single_symbol_timestamps(
    bars_by_symbol: dict[str, dict[datetime, BybitKlineBar]],
) -> tuple[datetime, ...]:
    if len(bars_by_symbol) != 1:
        raise ValueError("single-symbol timeline requires exactly one symbol")
    rows = next(iter(bars_by_symbol.values()))
    if not rows:
        return ()
    return tuple(sorted(rows))


def _payload_symbols(payload: Mapping[str, Any]) -> set[str]:
    symbols: set[str] = set()
    for collection_name in ("closed_trades", "decision_events"):
        collection = payload.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, Mapping):
                continue
            symbol = item.get("symbol")
            if isinstance(symbol, str) and symbol:
                symbols.add(symbol)
    return symbols
