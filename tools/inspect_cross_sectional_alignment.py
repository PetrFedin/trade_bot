"""Fail-closed OHLCV alignment preflight for cross-sectional research selection.

ASTRA's shipped selector checks the latest symbol timestamp. This independent
research preflight additionally requires identical *entire indicator lookback*
timestamp sets and optionally a known cadence. No missing bars are fabricated.
It does not alter strategy ranking, trading authority or historical provenance.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import timedelta

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_selection import CrossSectionalSelector

MAX_ROWS = 50000
MAX_SYMBOLS = 50


class ResearchAlignmentError(ValueError):
    """Reject out-of-sync symbol snapshots before offline shadow selection."""


def inspect_alignment(
    bars: Iterable[OhlcvBar], *, indicator_lookback: int,
    expected_interval: timedelta | None = None,
) -> dict:
    """Verify every symbol shares the same latest indicator decision window."""
    if type(indicator_lookback) is not int or not 2 <= indicator_lookback <= 512:
        raise ResearchAlignmentError("invalid indicator lookback")
    if expected_interval is not None and (
        not isinstance(expected_interval, timedelta)
        or expected_interval <= timedelta(0)
    ):
        raise ResearchAlignmentError("invalid expected interval")

    rows = tuple(bars)
    if not 2 * indicator_lookback <= len(rows) <= MAX_ROWS:
        raise ResearchAlignmentError("invalid or incomplete research dataset size")
    grouped: dict[str, list[OhlcvBar]] = defaultdict(list)
    identity = set()
    for row in rows:
        if not isinstance(row, OhlcvBar):
            raise ResearchAlignmentError("expected OhlcvBar source evidence")
        row.validate()
        if row.timestamp.utcoffset() != timedelta(0):
            raise ResearchAlignmentError("research snapshot timestamp must be UTC")
        key = (row.symbol, row.timestamp)
        if key in identity:
            raise ResearchAlignmentError("duplicate instrument timestamp")
        identity.add(key)
        grouped[row.symbol].append(row)
    if not 2 <= len(grouped) <= MAX_SYMBOLS:
        raise ResearchAlignmentError("unsupported symbol cohort size")

    window_by_symbol = {}
    for symbol, history in sorted(grouped.items()):
        history.sort(key=lambda row: row.timestamp)
        if len(history) < indicator_lookback:
            raise ResearchAlignmentError(f"{symbol}: insufficient indicator history")
        stamps = tuple(row.timestamp for row in history[-indicator_lookback:])
        if expected_interval is not None and any(
            next_time - prior_time != expected_interval
            for prior_time, next_time in zip(stamps, stamps[1:], strict=False)
        ):
            raise ResearchAlignmentError(f"{symbol}: source cadence gap")
        window_by_symbol[symbol] = stamps

    canonical = next(iter(window_by_symbol.values()))
    if any(stamps != canonical for stamps in window_by_symbol.values()):
        raise ResearchAlignmentError("cross-sectional indicator windows not synchronized")
    trace = [
        {"symbol": symbol, "timestamps": [when.isoformat() for when in timestamps]}
        for symbol, timestamps in sorted(window_by_symbol.items())
    ]
    digest = hashlib.sha256(
        json.dumps(trace, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "astra-research-cross-sectional-alignment-v1",
        "result": "SHADOW_LOOKBACK_TIMES_ALIGNED",
        "symbols": sorted(grouped),
        "lookback_bars": indicator_lookback,
        "checked_rows": len(rows),
        "decision_time": canonical[-1].isoformat(),
        "lookback_timestamps_sha256": digest,
        "source_origin_verified": False,
        "full_history_continuity_verified": False,
        "real_execution_verified": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "profitability_proven": False,
    }


def shadow_select_if_aligned(
    bars: Iterable[OhlcvBar], *, selector: CrossSectionalSelector | None = None,
    expected_interval: timedelta | None = None,
) -> tuple[dict, object]:
    """Run the actual ASTRA selector only after isolated research alignment."""
    frozen = tuple(bars)
    actual = selector if selector is not None else CrossSectionalSelector()
    if not isinstance(actual, CrossSectionalSelector):
        raise ResearchAlignmentError("unsupported shadow selector")
    gate = inspect_alignment(
        frozen,
        indicator_lookback=actual.signal_config.minimum_history_bars,
        expected_interval=expected_interval,
    )
    selection = actual.select(frozen)
    if selection.decision_time.isoformat() != gate["decision_time"]:
        raise ResearchAlignmentError("selector decision drift after alignment gate")
    return gate, selection
