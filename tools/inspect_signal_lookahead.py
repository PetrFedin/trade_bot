"""Offline, fail-closed incremental signal consistency audit.

This is a diagnostic inspired by public look-ahead-analysis concepts.
A clean result DOES NOT prove no look-ahead across other settings, symbols,
runtime components or different strategy implementations.

Research only; no signal routing, broker access, credentials or deployment.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta, datetime
from decimal import Decimal

MAX_BARS = 512
VALID_SIGNALS = frozenset({"BUY", "SELL", "HOLD", "NO_SIGNAL"})


class LookaheadAuditError(ValueError):
    """Reject invalid research inputs or nondeterministic strategy behavior."""


@dataclass(frozen=True)
class CloseBar:
    timestamp: datetime
    close: Decimal


def _checked_output(raw: object, count: int) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or len(raw) != count:
        raise LookaheadAuditError("signal callback must return one result per input bar")
    if any(type(item) is not str or item not in VALID_SIGNALS for item in raw):
        raise LookaheadAuditError("signal callback returned unsupported signal")
    return tuple(raw)


def _evaluate(
    signal_function: Callable[[tuple[CloseBar, ...]], Sequence[str]],
    bars: tuple[CloseBar, ...],
) -> tuple[str, ...]:
    try:
        raw = signal_function(bars)
    except Exception as exc:
        raise LookaheadAuditError("signal callback failed during research audit") from exc
    return _checked_output(raw, len(bars))


def inspect_lookahead(
    bars: Sequence[CloseBar],
    signal_function: Callable[[tuple[CloseBar, ...]], Sequence[str]],
    *,
    warmup_bars: int = 0,
    max_examples: int = 20,
) -> dict:
    """Compare full-history output with every causally available prefix.

    Every prefix is freshly evaluated using only bars at or before that bar.
    Output changes are evidence of history-length sensitivity, which may come
    from future leakage OR an unstable initialization/warmup procedure.
    This diagnostic must never automatically select or promote a strategy.
    """
    if isinstance(bars, (str, bytes)) or not isinstance(bars, (list, tuple)):
        raise LookaheadAuditError("bars must be a bounded sequence")
    frozen = tuple(bars)
    if not 3 <= len(frozen) <= MAX_BARS:
        raise LookaheadAuditError("signal sample outside research size bounds")
    if type(warmup_bars) is not int or not 0 <= warmup_bars < len(frozen):
        raise LookaheadAuditError("invalid warmup")
    if type(max_examples) is not int or not 0 <= max_examples <= 100:
        raise LookaheadAuditError("invalid example cap")

    previous: datetime | None = None
    for bar in frozen:
        if not isinstance(bar, CloseBar):
            raise LookaheadAuditError("expected immutable CloseBar records")
        when = bar.timestamp
        if (
            not isinstance(when, datetime)
            or when.tzinfo is None
            or when.utcoffset() != timedelta(0)
            or (previous is not None and when <= previous)
        ):
            raise LookaheadAuditError("bar chronology must be strictly ascending UTC")
        if type(bar.close) is not Decimal or not bar.close.is_finite() or bar.close <= 0:
            raise LookaheadAuditError("close must be a finite positive Decimal")
        previous = when

    full = _evaluate(signal_function, frozen)
    if _evaluate(signal_function, frozen) != full:
        raise LookaheadAuditError("nondeterministic signal callback")
    mismatch_count = 0
    changed_bars: set[str] = set()
    examples: list[dict] = []
    for end in range(1, len(frozen) + 1):
        prefix = _evaluate(signal_function, frozen[:end])
        for i in range(warmup_bars, end):
            if prefix[i] != full[i]:
                mismatch_count += 1
                stamp = frozen[i].timestamp.isoformat()
                changed_bars.add(stamp)
                if len(examples) < max_examples:
                    examples.append({
                        "bar_time": stamp,
                        "prefix_length": end,
                        "full_signal": full[i],
                        "prefix_signal": prefix[i],
                    })

    canonical_bars = [
        [bar.timestamp.isoformat(), str(bar.close)] for bar in frozen
    ]
    dataset_digest = hashlib.sha256(
        json.dumps(canonical_bars, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "astra-incremental-lookahead-audit-v1",
        "evaluation_class": "OFFLINE_RESEARCH_SIGNAL_HORIZON_SENSITIVITY_ONLY",
        "signal_observations": len(frozen),
        "warmup_bars": warmup_bars,
        "evaluations": len(frozen) + 2,
        "dataset_sha256": dataset_digest,
        "mismatch_count": mismatch_count,
        "affected_bar_count": len(changed_bars),
        "examples": examples,
        "verdict": (
            "HISTORY_HORIZON_SENSITIVITY_DETECTED"
            if mismatch_count else "NO_HORIZON_DIFFERENCES_IN_THIS_SAMPLE"
        ),
        "signal_implementation_identity_verified": False,
        "no_lookahead_proven": False,
        "historical_profitability_proven": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
    }
