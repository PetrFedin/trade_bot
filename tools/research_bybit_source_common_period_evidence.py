from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

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
    BybitResearchInstrument,
    BybitResearchUniverseClient,
    BybitResearchUniversePolicy,
    select_bybit_research_universe,
)
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_live_evidence_postgres import PostgresCryptoLiveEvidenceStore
from app.strategy.crypto_source_common_period_evidence import (
    build_source_common_period_symbol_evidence_rows,
)
from app.strategy.crypto_strategy_evidence_matrix import (
    CryptoStrategyEvidenceRow,
    diagnose_crypto_strategy_evidence_matrix,
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
_PRICE_DSN_ENV = "BYBIT_FULL_PERIOD_DATABASE_DSN"
_DERIVATIVES_DSN_ENV = "BYBIT_FULL_PERIOD_DATABASE_DSN"
_EVIDENCE_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"


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

    def load_open_interest(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ): ...

    def load_account_ratio(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ): ...

    def load_funding(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ): ...


class _EvidenceStore(Protocol):
    def persist_evidence_report(
        self,
        report: dict[str, Any],
        *,
        observed_at: datetime,
    ) -> str: ...


def run_source_common_period_evidence_research(
    price_store: _PriceStore,
    derivatives_store: _DerivativesStore,
    *,
    observed_at: datetime | None = None,
    bybit_site: str = "global",
    opening_equity_usdt: Decimal = Decimal("1000"),
    universe_client: _UniverseClient | None = None,
    evidence_store: _EvidenceStore | None = None,
) -> dict[str, Any]:
    cutoff = _utc(datetime.now(UTC) if observed_at is None else observed_at)
    if not opening_equity_usdt.is_finite() or opening_equity_usdt <= 0:
        raise ValueError("source-common research opening equity must be positive and finite")
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
            "source-common evidence refused incomplete dynamic Top-10:"
            + ",".join(selection.blockers)
        )
    ordered_symbols = tuple(item.symbol for item in selection.selected)
    symbols = tuple(sorted(ordered_symbols))
    instrument_map = {item.symbol: item for item in instruments}
    if any(symbol not in instrument_map for symbol in symbols):
        raise RuntimeError("source-common evidence lost instrument metadata")

    price_stored = price_store.coverage_state(symbols)
    price_plan = build_bybit_full_period_5m_plan(
        instruments,
        symbols=symbols,
        observed_at=cutoff,
        completed_by_symbol=price_stored.completed_by_symbol,
        unavailable_retry_after_by_symbol=price_stored.unavailable_retry_after_by_symbol,
    )
    if not price_plan.full_period_complete:
        raise RuntimeError("source-common evidence requires complete v113 price history")

    derivatives_stored = derivatives_store.coverage_state(symbols)
    derivatives_plan = build_bybit_full_period_derivatives_plan(
        instruments,
        symbols=symbols,
        observed_at=cutoff,
        completed_by_source_symbol=(
            derivatives_stored.completed_by_source_symbol
        ),
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
            "source-common evidence requires complete v114 source-available history:"
            + ",".join(incomplete)
        )

    coverage_by_symbol = {item.symbol: item for item in derivatives_plan.coverage}
    end_exclusive = datetime.combine(
        derivatives_plan.last_archive_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=UTC,
    )
    all_rows: list[CryptoStrategyEvidenceRow] = []
    symbol_summaries: list[dict[str, Any]] = []
    for rank, symbol in enumerate(ordered_symbols, start=1):
        instrument = instrument_map[symbol]
        coverage = coverage_by_symbol[symbol]
        common_start = datetime.fromisoformat(coverage.source_available_common_start_at)
        common_start = _utc(common_start)
        bars = price_store.load_bars(
            symbols=(symbol,),
            start_at=common_start,
            end_at=end_exclusive,
        )
        oi = derivatives_store.load_open_interest(
            symbol=symbol,
            start_at=common_start,
            end_at=end_exclusive,
        )
        ratio = derivatives_store.load_account_ratio(
            symbol=symbol,
            start_at=common_start,
            end_at=end_exclusive,
        )
        funding = derivatives_store.load_funding(
            symbol=symbol,
            start_at=common_start,
            end_at=end_exclusive,
        )
        rows, summary = build_source_common_period_symbol_evidence_rows(
            instrument,
            bars=bars,
            open_interest=oi,
            account_ratio=ratio,
            funding=funding,
            common_start_at=common_start,
            end_exclusive_at=end_exclusive,
            opening_equity_usdt=opening_equity_usdt,
        )
        all_rows.extend(rows)
        summary["market_rank_at_research_time"] = rank
        summary["instrument_lifetime_derivatives_complete"] = (
            coverage.instrument_lifetime_derivatives_complete
        )
        summary["lifetime_truncated_by_source_floor"] = any(
            source.lifetime_truncated_by_source_floor for source in coverage.sources
        )
        symbol_summaries.append(summary)

    if not all_rows:
        raise RuntimeError("source-common evidence produced no closed strategy trades")
    report = diagnose_crypto_strategy_evidence_matrix(all_rows)
    if report.get("turnover_reference_usdt") is None:
        raise RuntimeError("source-common evidence turnover reference is unavailable")
    report["evidence_scope"] = "PER_SYMBOL_MAX_SOURCE_AVAILABLE_COMMON_PERIOD"
    report["observed_at"] = cutoff.isoformat()
    report["universe_host"] = host
    report["top10_symbols"] = list(ordered_symbols)
    report["price_history_full_period_complete"] = True
    report["derivatives_source_available_period_complete"] = True
    report["instrument_lifetime_derivatives_complete"] = (
        derivatives_plan.instrument_lifetime_derivatives_complete
    )
    report["instrument_lifetime_combined_matrix_claim_allowed"] = (
        derivatives_plan.instrument_lifetime_derivatives_complete
    )
    report["source_available_common_period_matrix"] = True
    report["symbol_coverage"] = symbol_summaries
    report["account_ratio_documented_source_floor_at"] = (
        derivatives_plan.to_payload()["account_ratio_documented_source_floor_at"]
    )
    report["portfolio_competition_modeled"] = False
    report["opening_equity_usdt_per_symbol_diagnostic"] = str(opening_equity_usdt)
    report["trade_actionable"] = False
    report["operator_review_required"] = True
    report["strategy_parameters_changed"] = False
    report["parameter_retuning_performed"] = False
    report["strategy_selection_allowed"] = False
    report["strategy_promotion_allowed"] = False
    report["demo_activation_allowed"] = False
    report["live_activation_allowed"] = False
    report["bybit_live_order_routing_allowed"] = False
    report["causal_claim_allowed"] = False
    report["predictive_guarantee_allowed"] = False

    evidence_id = None
    if evidence_store is not None:
        evidence_id = evidence_store.persist_evidence_report(
            report,
            observed_at=cutoff,
        )
    report["persisted_evidence_snapshot_id"] = evidence_id
    return report


def _site_host(site: str) -> str:
    normalized = site.strip().lower()
    if normalized != site or normalized not in _SITE_HOSTS:
        raise ValueError("Bybit site must be one of " + ",".join(sorted(_SITE_HOSTS)))
    return _SITE_HOSTS[normalized]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source-common research timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the fixed-strategy Bybit evidence matrix over each Top-10 symbol's "
            "longest source-available common price/OI/crowding/funding period."
        )
    )
    parser.add_argument(
        "--site",
        default=os.environ.get("BYBIT_MAINNET_READONLY_SITE", "global"),
        choices=sorted(_SITE_HOSTS),
    )
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--price-dsn-env", default=_PRICE_DSN_ENV)
    parser.add_argument("--derivatives-dsn-env", default=_DERIVATIVES_DSN_ENV)
    parser.add_argument("--evidence-dsn-env", default=_EVIDENCE_DSN_ENV)
    parser.add_argument("--persist-evidence", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    price_dsn = os.getenv(args.price_dsn_env, "").strip()
    derivatives_dsn = os.getenv(args.derivatives_dsn_env, "").strip()
    if not price_dsn or not derivatives_dsn:
        raise RuntimeError("v113/v114 PostgreSQL DSN environment is missing")
    price_store = PostgresBybitFullPeriod5mStore(price_dsn)
    derivatives_store = PostgresBybitFullPeriodDerivativesStore(derivatives_dsn)
    evidence_store = None
    if args.persist_evidence:
        evidence_dsn = os.getenv(args.evidence_dsn_env, "").strip()
        if not evidence_dsn:
            raise RuntimeError("evidence PostgreSQL DSN environment is missing")
        evidence_store = PostgresCryptoLiveEvidenceStore(evidence_dsn)
    report = run_source_common_period_evidence_research(
        price_store,
        derivatives_store,
        bybit_site=args.site,
        opening_equity_usdt=Decimal(args.opening_equity),
        evidence_store=evidence_store,
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
