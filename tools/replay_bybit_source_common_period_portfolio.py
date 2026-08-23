from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from app.marketdata.bybit_full_period_5m import build_bybit_full_period_5m_plan
from app.marketdata.bybit_full_period_5m_postgres import (
    BybitFullPeriod5mStoredCoverage,
    PostgresBybitFullPeriod5mStore,
)
from app.marketdata.bybit_full_period_derivatives import (
    build_bybit_full_period_derivatives_plan,
)
from app.marketdata.bybit_full_period_derivatives_postgres import (
    BybitFullPeriodDerivativesStoredCoverage,
    PostgresBybitFullPeriodDerivativesStore,
)
from app.marketdata.bybit_research_universe import (
    BybitResearchUniverseClient,
    BybitResearchUniversePolicy,
    select_bybit_research_universe,
)
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_source_common_period_evidence import ArchivedBybitDerivativesHistoryView
from app.strategy.crypto_source_common_period_portfolio import (
    run_source_common_period_portfolio_replay,
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
_FULL_PERIOD_DSN_ENV = "BYBIT_FULL_PERIOD_DATABASE_DSN"


class _UniverseClient(Protocol):
    def fetch_instruments(self): ...

    def fetch_tickers(self): ...


class _PriceStore(Protocol):
    def coverage_state(
        self,
        symbols: Sequence[str],
    ) -> BybitFullPeriod5mStoredCoverage: ...

    def load_bars(
        self,
        *,
        symbols: Sequence[str],
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> tuple[BybitKlineBar, ...]: ...


class _DerivativesStore(Protocol):
    def coverage_state(
        self,
        symbols: Sequence[str],
    ) -> BybitFullPeriodDerivativesStoredCoverage: ...

    def load_open_interest(self, *, symbol: str, start_at: datetime, end_at: datetime): ...

    def load_account_ratio(self, *, symbol: str, start_at: datetime, end_at: datetime): ...

    def load_funding(self, *, symbol: str, start_at: datetime, end_at: datetime): ...


def run_source_common_period_portfolio_research(
    price_store: _PriceStore,
    derivatives_store: _DerivativesStore,
    *,
    observed_at: datetime | None = None,
    bybit_site: str = "global",
    opening_equity_usdt: Decimal = Decimal("1000"),
    universe_client: _UniverseClient | None = None,
) -> dict[str, object]:
    cutoff = _utc(datetime.now(UTC) if observed_at is None else observed_at)
    if not opening_equity_usdt.is_finite() or opening_equity_usdt <= 0:
        raise ValueError("portfolio research opening equity must be positive and finite")
    host = _site_host(bybit_site)
    universe = (
        BybitResearchUniverseClient(host=host)
        if universe_client is None
        else universe_client
    )
    instruments = tuple(universe.fetch_instruments())
    tickers = tuple(universe.fetch_tickers())
    selection = select_bybit_research_universe(
        instruments,
        tickers,
        observed_at_ms=int(cutoff.timestamp() * 1000),
        host=host,
        policy=BybitResearchUniversePolicy(top_n=10),
    )
    if not selection.complete_top_n or len(selection.selected) != 10:
        raise RuntimeError(
            "source-common portfolio refused incomplete dynamic Top-10:"
            + ",".join(selection.blockers)
        )
    ordered_symbols = tuple(item.symbol for item in selection.selected)
    symbols = tuple(sorted(ordered_symbols))
    instrument_map = {item.symbol: item for item in instruments}
    if any(symbol not in instrument_map for symbol in symbols):
        raise RuntimeError("source-common portfolio lost instrument metadata")

    price_stored = price_store.coverage_state(symbols)
    price_plan = build_bybit_full_period_5m_plan(
        instruments,
        symbols=symbols,
        observed_at=cutoff,
        completed_by_symbol=price_stored.completed_by_symbol,
        unavailable_retry_after_by_symbol=price_stored.unavailable_retry_after_by_symbol,
    )
    if not price_plan.full_period_complete:
        raise RuntimeError("source-common portfolio requires complete v113 price history")

    derivatives_stored = derivatives_store.coverage_state(symbols)
    derivatives_plan = build_bybit_full_period_derivatives_plan(
        instruments,
        symbols=symbols,
        observed_at=cutoff,
        completed_by_source_symbol=derivatives_stored.completed_by_source_symbol,
        unavailable_retry_after_by_source_symbol=(
            derivatives_stored.unavailable_retry_after_by_source_symbol
        ),
    )
    incomplete = tuple(
        item.symbol
        for item in derivatives_plan.coverage
        if not item.source_available_period_complete
    )
    if incomplete:
        raise RuntimeError(
            "source-common portfolio requires complete v114 source-available history:"
            + ",".join(incomplete)
        )
    if price_plan.last_archive_date != derivatives_plan.last_archive_date:
        raise RuntimeError("source-common portfolio price/derivatives cutoffs differ")

    coverage_by_symbol = {item.symbol: item for item in derivatives_plan.coverage}
    symbol_common_starts = {
        symbol: _utc(
            datetime.fromisoformat(
                coverage_by_symbol[symbol].source_available_common_start_at
            )
        )
        for symbol in symbols
    }
    portfolio_common_start = max(symbol_common_starts.values())
    end_exclusive = datetime.combine(
        derivatives_plan.last_archive_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=UTC,
    )
    if portfolio_common_start >= end_exclusive:
        raise RuntimeError("source-common portfolio has no shared completed interval")

    raw_bars = price_store.load_bars(
        symbols=symbols,
        start_at=portfolio_common_start,
        end_at=end_exclusive,
    )
    bars_by_symbol: dict[str, list[BybitKlineBar]] = defaultdict(list)
    for bar in raw_bars:
        bars_by_symbol[bar.symbol].append(bar)

    histories: dict[str, ArchivedBybitDerivativesHistoryView] = {}
    for symbol in symbols:
        oi = derivatives_store.load_open_interest(
            symbol=symbol,
            start_at=portfolio_common_start,
            end_at=end_exclusive,
        )
        ratio = derivatives_store.load_account_ratio(
            symbol=symbol,
            start_at=portfolio_common_start,
            end_at=end_exclusive,
        )
        funding = derivatives_store.load_funding(
            symbol=symbol,
            start_at=portfolio_common_start,
            end_at=end_exclusive,
        )
        history = ArchivedBybitDerivativesHistoryView(
            symbol=symbol,
            start_ms=int(portfolio_common_start.timestamp() * 1000),
            end_ms=int(end_exclusive.timestamp() * 1000) - 1,
            interval="5min",
            open_interest=tuple(oi),
            account_ratio=tuple(ratio),
            funding=tuple(funding),
        )
        history.validate()
        histories[symbol] = history

    report = run_source_common_period_portfolio_replay(
        ordered_symbols=ordered_symbols,
        bars_by_symbol=bars_by_symbol,
        common_start_at=portfolio_common_start,
        end_exclusive_at=end_exclusive,
        opening_equity_usdt=opening_equity_usdt,
        derivatives_history_by_symbol=histories,
    )
    report["observed_at"] = cutoff.isoformat()
    report["universe_host"] = host
    report["top10"] = [
        {
            "rank": item.rank,
            "symbol": item.symbol,
            "universe_score": str(item.score),
            "turnover_24h_usdt": str(item.turnover_24h_usdt),
            "open_interest_value_usdt": str(item.open_interest_value_usdt),
            "spread_bps": str(item.spread_bps),
            "funding_rate": str(item.funding_rate),
        }
        for item in selection.selected
    ]
    report["per_symbol_source_common_start_at"] = {
        symbol: symbol_common_starts[symbol].isoformat() for symbol in ordered_symbols
    }
    report["portfolio_common_start_rule"] = (
        "max(per-symbol price/OI/account-ratio/funding source-available start)"
    )
    report["price_history_full_period_complete"] = True
    report["derivatives_source_available_period_complete"] = True
    report["instrument_lifetime_derivatives_complete"] = (
        derivatives_plan.instrument_lifetime_derivatives_complete
    )
    report["account_ratio_documented_source_floor_at"] = (
        derivatives_plan.to_payload()["account_ratio_documented_source_floor_at"]
    )
    report["independent_per_symbol_evidence_remains_live_ranking_source"] = True
    report["portfolio_trade_evidence_is_diagnostic_only"] = True
    report["portfolio_trade_evidence_persisted_as_live_rank_source"] = False
    return report


def _site_host(site: str) -> str:
    normalized = site.strip().lower()
    if normalized != site or normalized not in _SITE_HOSTS:
        raise ValueError("Bybit site must be one of " + ",".join(sorted(_SITE_HOSTS)))
    return _SITE_HOSTS[normalized]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("portfolio research timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay one shared-capital fixed strategy across the current dynamic Bybit Top-10 "
            "over their maximum synchronized source-common historical period."
        )
    )
    parser.add_argument(
        "--site",
        default=os.environ.get("BYBIT_MAINNET_READONLY_SITE", "global"),
        choices=sorted(_SITE_HOSTS),
    )
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dsn-env", default=_FULL_PERIOD_DSN_ENV)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    dsn = os.getenv(args.dsn_env, "").strip()
    if not dsn:
        raise RuntimeError("v113/v114 PostgreSQL DSN environment is missing")
    price_store = PostgresBybitFullPeriod5mStore(dsn)
    derivatives_store = PostgresBybitFullPeriodDerivativesStore(dsn)
    report = run_source_common_period_portfolio_research(
        price_store,
        derivatives_store,
        bybit_site=args.site,
        opening_equity_usdt=Decimal(args.opening_equity),
    )
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output is None:
        print(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
