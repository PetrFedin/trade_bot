"""Synthetic safety fixtures are not observed market or fill evidence."""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
    ResearchCollisionContext,
    _validated_collision_refinement,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.ohlcv_exit import (
    IntrabarExitReason,
    IntrabarPositionState,
    evaluate_long_intrabar_exit,
)
from app.strategy.position_management import PositionManagementPolicy
from tools.portfolio_hourly_replay import (
    SOURCE_ROOTS,
    HourlyCase,
    HourlyCollisionResolver,
    load_hourly_cases,
    replay,
)

D = Decimal
DAY = datetime(2025, 1, 1, tzinfo=UTC)
SNAP = Path("research/snapshots")


def bar(*, symbol="BTCUSDT", day=DAY, opening="100", high="101", low="99", close="100"):
    return OhlcvBar(symbol, day, D(opening), D(high), D(low), D(close), 0, 0)


def context(day_bar=None, *, prior_peak="100"):
    daily = bar(high="105", low="97") if day_bar is None else day_bar
    policy = PositionManagementPolicy()
    state = IntrabarPositionState(D(prior_peak))
    original = evaluate_long_intrabar_exit(
        average_cost=D("100"), bar=daily, state=state, policy=policy,
    )
    assert original.ambiguous_bar
    return ResearchCollisionContext(D("100"), daily, state, policy, original)


def resolve(ctx, overrides):
    hours = tuple(
        bar(symbol=ctx.bar.symbol, day=ctx.bar.timestamp + timedelta(hours=i),
            **overrides.get(i, {}))
        for i in range(24)
    )
    case = HourlyCase(
        hours, (ctx.bar.open, ctx.bar.high, ctx.bar.low, ctx.bar.close, D("0")),
        "SYNTHETIC_NOT_REAL_MARKET", "TEST_MANIFEST",
    )
    resolver = HourlyCollisionResolver({(ctx.bar.symbol, ctx.bar.timestamp): case})
    refined = resolver(ctx)
    _validated_collision_refinement(ctx, refined)
    return refined, resolver.audit


def test_both_target_first_and_protective_first_are_preserved():
    target, audit = resolve(context(), {1: {"high": "105"}, 20: {"low": "97"}})
    assert target.reason is IntrabarExitReason.TAKE_PROFIT
    assert target.exit_price_before_costs == D("104")
    assert audit[0]["status"] == "TARGET_FIRST_HOURLY"
    stop, audit = resolve(context(), {1: {"low": "97"}, 20: {"high": "105"}})
    assert stop.reason is IntrabarExitReason.HARD_STOP
    assert stop.exit_price_before_costs == D("98")
    assert audit[0]["status"] == "PROTECTIVE_FIRST_HOURLY"


def test_same_hour_collision_remains_ambiguous_stop_first():
    result, audit = resolve(context(), {1: {"high": "105", "low": "97"}})
    assert result.reason is IntrabarExitReason.HARD_STOP
    assert result.ambiguous_bar is True
    assert audit[0]["status"] == "UNRESOLVED_SAME_HOUR_STOP_FIRST"


def test_hourly_high_cannot_activate_daily_trailing_early():
    result, _ = resolve(context(), {
        0: {"high": "103"}, 1: {"low": "99"},
        10: {"high": "105"}, 20: {"low": "97"},
    })
    assert result.reason is IntrabarExitReason.TAKE_PROFIT
    assert result.trailing_stop_price is None
    assert result.state.peak_completed_price == D("100")


def test_preexisting_prior_day_trailing_is_preserved():
    daily = bar(opening="102", high="105", low="100", close="102")
    ctx = context(daily, prior_peak="103")
    hours = {i: {"opening": "102", "low": "102", "high": "103", "close": "102"}
             for i in range(24)}
    hours[2] = {"opening": "102", "high": "103", "low": "100", "close": "102"}
    hours[20] = {"opening": "102", "high": "105", "low": "102", "close": "102"}
    result, _ = resolve(ctx, hours)
    assert result.reason is IntrabarExitReason.TRAILING_STOP
    assert result.exit_price_before_costs == D("101.455")


def test_gap_stop_cannot_get_better_stop_price():
    result, _ = resolve(context(bar(high="105", low="94")), {
        1: {"opening": "95", "high": "96", "low": "94", "close": "95"},
        20: {"high": "105"},
    })
    assert result.exit_price_before_costs == D("95")
    assert result.gap_through_protective_stop is True


def test_target_gap_gets_target_not_favorable_hourly_open():
    result, _ = resolve(context(bar(high="106", low="97")), {
        1: {"opening": "105", "high": "106", "low": "105", "close": "105"},
        20: {"low": "97"},
    })
    assert result.reason is IntrabarExitReason.TAKE_PROFIT
    assert result.exit_price_before_costs == D("104")


def test_new_missing_collision_is_logged_and_strict_mode_refuses_it():
    ctx = context()
    resolver = HourlyCollisionResolver({})
    assert resolver(ctx) == ctx.daily_decision
    assert resolver.audit[0]["status"] == "MISSING_SOURCE_STOP_FIRST"
    with pytest.raises(ValueError, match="no hourly source"):
        HourlyCollisionResolver({}, require_source=True)(ctx)


@pytest.mark.parametrize("change", [
    {"exit_now": False}, {"take_profit_price": D("500")},
    {"exit_price_before_costs": D("NaN")}, {"exit_price_before_costs": D("200")},
    {"state": IntrabarPositionState(D("999"))},
    {"reason": IntrabarExitReason.TAKE_PROFIT, "exit_price_before_costs": D("105")},
    {"reason": IntrabarExitReason.TAKE_PROFIT, "exit_price_before_costs": D("104")},
    {"gap_through_protective_stop": True}, {"reason": "TAKE_PROFIT"},
])
def test_refinement_cannot_remove_protection_or_invent_favorable_prices(change):
    ctx = context()
    with pytest.raises(ValueError):
        _validated_collision_refinement(ctx, replace(ctx.daily_decision, **change))


def market():
    result = []
    for symbol, increment in (("BTCUSDT", D("1")), ("ETHUSDT", D("0.5"))):
        for i in range(20):
            price = D("100") + increment * i
            high, low = price + D("0.2"), price - D("0.2")
            if symbol == "BTCUSDT" and i == 8:
                high, low = price * D("1.06"), price * D("0.96")
            result.append(OhlcvBar(
                symbol, DAY + timedelta(days=i), price, high, low, price, 1000, 1,
            ))
    return result


def test_cash_path_recalculates_later_sizes_and_fees_without_reranking():
    bars = market()
    original = replay(bars, fees=D("8"), slip=D("5"))

    def target_first(ctx):
        return replace(
            ctx.daily_decision, reason=IntrabarExitReason.TAKE_PROFIT,
            exit_price_before_costs=ctx.daily_decision.take_profit_price,
            ambiguous_bar=False, gap_through_protective_stop=False,
        )

    refined = replay(bars, fees=D("8"), slip=D("5"), resolver=target_first)
    assert refined.ending_equity > original.ending_equity
    assert refined.fees_paid != original.fees_paid
    assert any(a.quantity != b.quantity for a, b in zip(
        original.closed_trades[1:], refined.closed_trades[1:], strict=False,
    ))
    assert [d.selected_symbols for d in original.decision_trace] == [
        d.selected_symbols for d in refined.decision_trace
    ]
    assert refined.maximum_concurrent_positions <= 2
    assert replay(bars, fees=D("8"), slip=D("5"),
                  resolver=lambda ctx: ctx.daily_decision) == original


def test_default_core_matches_independent_unmodified_source_golden():
    # Computed with unmodified exported core 1447988d. Synthetic identity only.
    current = CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(), portfolio_policy=CrossSectionalPortfolioPolicy(),
    ).run(market())
    serialized = json.dumps(asdict(current), sort_keys=True, default=str, separators=(",", ":"))
    assert hashlib.sha256(serialized.encode()).hexdigest() == (
        "75e06dbfaa2723df7b984d9ed48296143f2db48b98e89ab1f04647af41e946ef"
    )


def test_real_hourly_sources_reconcile_including_all_stress_cases():
    cases = load_hourly_cases(
        SNAP / "bybit_3y_2023-10-09_2026-10-08", tuple(SNAP / r for r in SOURCE_ROOTS),
    )
    assert len(cases) == 40
    assert sum(len(c.bars) for c in cases.values()) == 960


def source_fixture(tmp_path):
    name = "BNBUSDT_2024-04-12"
    root, daily = tmp_path / "hourly", tmp_path / "daily"
    root.mkdir()
    daily.mkdir()
    shutil.copytree(SNAP / "bybit_intraday_trade_cases_60m" / name, root / name)
    shutil.copy(SNAP / "bybit_3y_2023-10-09_2026-10-08/BNBUSDT_D.csv", daily)
    return root, daily, root / name


def test_duplicate_and_changed_provider_bytes_are_rejected(tmp_path):
    root, daily, folder = source_fixture(tmp_path)
    with pytest.raises(ValueError, match="duplicate hourly source"):
        load_hourly_cases(daily, (root, root))
    csv_path = folder / "BNBUSDT_60m.csv"
    csv_path.write_text(csv_path.read_text() + "\n")
    with pytest.raises(ValueError, match="SHA mismatch"):
        load_hourly_cases(daily, (root,))


def test_fractional_volume_cannot_be_rounded_to_hide_mismatch(tmp_path):
    root, daily, folder = source_fixture(tmp_path)
    path = folder / "BNBUSDT_60m.csv"
    data = path.read_text().splitlines()
    fields = data[1].split(",")
    fields[-1] = str(D(fields[-1]) + D("0.000001"))
    data[1] = ",".join(fields)
    path.write_text("\n".join(data) + "\n")
    manifest = folder / "provenance.json"
    meta = json.loads(manifest.read_text())
    meta["sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="exact raw daily OHLCV"):
        load_hourly_cases(daily, (root,))
