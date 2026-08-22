from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from app.marketdata.bybit_public_archive import (
    BybitPublicTradeArchiveClient,
    completed_archive_dates,
)
from app.marketdata.bybit_research_universe import (
    BybitResearchUniverseClient,
    BybitResearchUniversePolicy,
    BybitResearchUniverseSelection,
    select_bybit_research_universe,
)
from app.marketdata.bybit_v5 import (
    BybitKlineAcquisition,
    BybitKlineRequest,
    BybitPublicKlineClient,
    last_completed_kline_end_ms,
)
from app.strategy.crypto_historical_diagnostics import (
    CryptoHistoricalDiagnosticsPolicy,
    diagnose_crypto_historical_conditions,
)
from app.strategy.crypto_market_history_profile import (
    CryptoMarketHistoryPolicy,
    profile_crypto_market_history,
)
from tools.qualify_bybit_crypto_walk_forward import (
    CryptoWalkForwardPolicy,
    run_crypto_walk_forward,
)
from tools.research_bybit_crypto_strategy_v2 import (
    compact_candidate_comparison,
    run_crypto_strategy_v2_suite,
)

_SITE_HOSTS = {
    "global": "api.bybit.com",
    "global-alt": "api.bytick.com",
    "nl": "api.bybit.nl",
    "tr": "api.bybit.tr",
    "kz": "api.bybit.kz",
    "georgia": "api.bybitgeorgia.ge",
    "ae": "api.bybit.ae",
    "eu": "api.bybit.eu",
    "id": "api.bybit.id",
    "jp": "api.manepa.jp",
    "hk": "api-spark-fintech.com",
}


class _ArchiveAcquisition(Protocol):
    klines: BybitKlineAcquisition

    def validate(
        self,
        *,
        requested_symbols: tuple[str, ...],
        minimum_bars: int,
    ) -> None: ...


class _ArchiveClient(Protocol):
    def fetch_klines(
        self,
        *,
        symbols: tuple[str, ...],
        dates: tuple[date, ...],
        interval_minutes: int,
    ) -> _ArchiveAcquisition: ...


class _KlineClient(Protocol):
    def fetch(self, request: BybitKlineRequest) -> BybitKlineAcquisition: ...


def run_dynamic_top10_research(
    *,
    observed_at: datetime | None = None,
    bybit_site: str = "global",
    opening_equity_usdt: Decimal = Decimal("1000"),
    micro_lookback_days: int = 28,
    universe_policy: BybitResearchUniversePolicy | None = None,
    walk_forward_policy: CryptoWalkForwardPolicy | None = None,
    history_policy: CryptoMarketHistoryPolicy | None = None,
    condition_policy: CryptoHistoricalDiagnosticsPolicy | None = None,
    universe_client: BybitResearchUniverseClient | None = None,
    archive_client: _ArchiveClient | None = None,
    kline_client: _KlineClient | None = None,
) -> dict[str, Any]:
    """Run the complete research-only Top-10 -> history -> strategy evidence pipeline."""

    if not opening_equity_usdt.is_finite() or opening_equity_usdt <= 0:
        raise ValueError("dynamic Top-10 research opening equity must be positive and finite")
    if isinstance(micro_lookback_days, bool) or micro_lookback_days < 1:
        raise ValueError("dynamic Top-10 research micro lookback must be a positive integer")
    active_walk = CryptoWalkForwardPolicy() if walk_forward_policy is None else walk_forward_policy
    active_walk.validate()
    minimum_micro_days = active_walk.fold_days * active_walk.minimum_folds
    if micro_lookback_days < minimum_micro_days:
        raise ValueError(
            f"dynamic Top-10 research needs at least {minimum_micro_days} micro-history days"
        )
    if observed_at is None:
        cutoff = datetime.now(UTC)
    else:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("dynamic Top-10 research observed_at must be timezone-aware")
        cutoff = observed_at.astimezone(UTC)
    observed_at_ms = int(cutoff.timestamp() * 1000)
    host = _site_host(bybit_site)
    active_universe = BybitResearchUniversePolicy() if universe_policy is None else universe_policy
    active_universe.validate()
    if active_universe.top_n != 10:
        raise ValueError("dynamic Top-10 research requires universe_policy.top_n=10")

    universe = (
        BybitResearchUniverseClient(host=host)
        if universe_client is None
        else universe_client
    )
    instruments = universe.fetch_instruments()
    tickers = universe.fetch_tickers()
    selection = select_bybit_research_universe(
        instruments,
        tickers,
        observed_at_ms=observed_at_ms,
        host=host,
        policy=active_universe,
    )
    if not selection.complete_top_n:
        raise RuntimeError(
            "dynamic Top-10 research refused incomplete universe:"
            + ",".join(selection.blockers)
        )
    symbols = tuple(item.symbol for item in selection.selected)
    if len(symbols) != 10:
        raise AssertionError("dynamic Top-10 selection must contain exactly ten symbols")

    instrument_by_symbol = {item.symbol: item for item in instruments}
    if any(symbol not in instrument_by_symbol for symbol in symbols):
        raise RuntimeError("dynamic Top-10 selected symbol is missing instrument metadata")
    earliest_launch_ms = min(
        instrument_by_symbol[symbol].launch_time_ms for symbol in symbols
    )
    macro_end_ms = last_completed_kline_end_ms(now_ms=observed_at_ms, interval="60")
    if earliest_launch_ms >= macro_end_ms:
        raise RuntimeError("dynamic Top-10 macro-history interval is empty")
    macro_request = BybitKlineRequest(
        symbols=symbols,
        start_ms=earliest_launch_ms,
        end_ms=macro_end_ms,
        interval="60",
        maximum_pages_per_symbol=100,
    )
    macro_client = BybitPublicKlineClient() if kline_client is None else kline_client
    macro_acquisition = macro_client.fetch(macro_request)
    macro_acquisition.validate(requested_symbols=symbols, minimum_bars=100)
    full_history = profile_crypto_market_history(
        macro_acquisition,
        policy=history_policy,
        interval="60",
    )

    archive_dates = completed_archive_dates(
        now=cutoff,
        lookback_days=micro_lookback_days,
    )
    archive = BybitPublicTradeArchiveClient() if archive_client is None else archive_client
    micro = archive.fetch_klines(
        symbols=symbols,
        dates=archive_dates,
        interval_minutes=5,
    )
    micro.validate(requested_symbols=symbols, minimum_bars=25)
    walk_forward = run_crypto_walk_forward(
        micro.klines,
        opening_equity_usdt=opening_equity_usdt,
        policy=active_walk,
    )
    suite = run_crypto_strategy_v2_suite(
        micro.klines,
        opening_equity_usdt=opening_equity_usdt,
    )
    candidates = suite.get("candidates")
    if not isinstance(candidates, Mapping):
        raise RuntimeError("dynamic Top-10 research strategy candidates are missing")
    combined = candidates.get("CONDITIONAL_COMBINED_RISK")
    if not isinstance(combined, Mapping):
        raise RuntimeError("dynamic Top-10 research combined-risk candidate is missing")
    trade_conditions = diagnose_crypto_historical_conditions(
        micro.klines,
        combined,
        policy=condition_policy,
    )

    result = {
        "research": "BYBIT_DYNAMIC_TOP10_FULL_HISTORY_AND_STRATEGY_DIAGNOSTICS",
        "observed_at": cutoff.isoformat(),
        "bybit_site": bybit_site,
        "public_universe_host": host,
        "top10_symbols": list(symbols),
        "universe": _selection_payload(selection),
        "full_history_hourly": full_history,
        "micro_execution_history": {
            "source": "BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
            "requested_archive_dates": [value.isoformat() for value in archive_dates],
            "lookback_days": micro_lookback_days,
            "raw_trade_archive_committed_to_repository": False,
            "counts_by_symbol": micro.klines.counts_by_symbol(),
        },
        "strategy_walk_forward": walk_forward,
        "strategy_candidate_comparison": compact_candidate_comparison(suite),
        "combined_risk_trade_conditions": trade_conditions,
        "current_derivatives_snapshot": {
            item.symbol: {
                "turnover_24h_usdt": float(item.turnover_24h_usdt),
                "open_interest_value_usdt": float(item.open_interest_value_usdt),
                "spread_bps": float(item.spread_bps),
                "funding_rate": float(item.funding_rate),
                "price_24h_fraction": float(item.price_24h_fraction),
            }
            for item in selection.selected
        },
        "evidence_layers": [
            "CURRENT_BYBIT_LINEAR_UNIVERSE_AND_LIQUIDITY",
            "FULL_AVAILABLE_HOURLY_PRICE_TURNOVER_HISTORY_WITHIN_V5_PAGINATION_BOUND",
            "RECENT_OFFICIAL_TRADE_ARCHIVE_AGGREGATED_5M",
            "FIXED_PARAMETER_NON_OVERLAPPING_WALK_FORWARD",
            "TRADE_ENTRY_CONDITION_ASSOCIATIONS_WITH_REALIZED_PNL_MFE_MAE",
        ],
        "known_next_evidence_gaps": [
            "HISTORICAL_OPEN_INTEREST_NOT_YET_JOINED_TO_EACH_SIGNAL",
            "HISTORICAL_LONG_SHORT_ACCOUNT_RATIO_NOT_YET_JOINED_TO_EACH_SIGNAL",
            "HISTORICAL_FUNDING_NOT_YET_CHARGED_TO_EACH_REPLAY_TRADE",
            "ORDER_BOOK_DEPTH_HISTORY_NOT_AVAILABLE_FROM_STANDARD_V5_KLINE_HISTORY",
        ],
        "parameter_retuning_performed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "real_money_order_submission_supported": False,
        "interpretation_contract": (
            "Top-10 is a current research universe, not a buy/sell list. Full-history patterns, "
            "correlations and indicator buckets are evidence to validate the fixed strategy and do "
            "not guarantee future profit."
        ),
    }
    _validate_final_boundary(result)
    return result


def _selection_payload(selection: BybitResearchUniverseSelection) -> dict[str, Any]:
    selection.validate()
    return {
        "observed_at_ms": selection.observed_at_ms,
        "host": selection.host,
        "complete_top_n": selection.complete_top_n,
        "eligible_symbol_count": selection.eligible_symbol_count,
        "source_instrument_count": selection.source_instrument_count,
        "source_ticker_count": selection.source_ticker_count,
        "blockers": list(selection.blockers),
        "selected": [
            {
                "rank": item.rank,
                "symbol": item.symbol,
                "score": float(item.score),
                "listing_days": item.listing_days,
                "turnover_24h_usdt": float(item.turnover_24h_usdt),
                "open_interest_value_usdt": float(item.open_interest_value_usdt),
                "spread_bps": float(item.spread_bps),
                "funding_rate": float(item.funding_rate),
                "price_24h_fraction": float(item.price_24h_fraction),
                "turnover_percentile": float(item.turnover_percentile),
                "open_interest_percentile": float(item.open_interest_percentile),
                "spread_quality_percentile": float(item.spread_quality_percentile),
                "history_percentile": float(item.history_percentile),
            }
            for item in selection.selected
        ],
        "excluded_reasons": {
            symbol: list(reasons)
            for symbol, reasons in sorted(selection.excluded_reasons.items())
        },
        "strategy_parameters_changed": False,
        "strategy_promotion_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def _site_host(site: str) -> str:
    normalized = site.strip().lower()
    if normalized != site or normalized not in _SITE_HOSTS:
        raise ValueError(
            "BYBIT research site must be one of " + ",".join(sorted(_SITE_HOSTS))
        )
    return _SITE_HOSTS[normalized]


def _validate_final_boundary(report: Mapping[str, Any]) -> None:
    for field in (
        "parameter_retuning_performed",
        "strategy_selection_allowed",
        "strategy_promotion_allowed",
        "demo_activation_allowed",
        "live_activation_allowed",
        "bybit_live_order_routing_allowed",
        "real_money_order_submission_supported",
    ):
        if report.get(field) is not False:
            raise ValueError(f"dynamic Top-10 research safety boundary violated:{field}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Select current Bybit Top-10 crypto research universe and run full-history plus "
            "fixed-strategy diagnostics without enabling order routing"
        )
    )
    parser.add_argument(
        "--site",
        default=os.environ.get("BYBIT_MAINNET_READONLY_SITE", "global"),
        choices=sorted(_SITE_HOSTS),
    )
    parser.add_argument("--micro-lookback-days", type=int, default=28)
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = run_dynamic_top10_research(
        bybit_site=args.site,
        opening_equity_usdt=Decimal(args.opening_equity),
        micro_lookback_days=args.micro_lookback_days,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("BYBIT_DYNAMIC_TOP10_RESEARCH=" + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
