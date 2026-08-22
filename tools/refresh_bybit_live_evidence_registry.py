from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from app.marketdata.bybit_derivatives_history import (
    BybitDerivativesHistory,
    BybitHistoricalDerivativesClient,
)
from app.marketdata.bybit_opportunity_postgres import PostgresBybitOpportunityStore
from app.marketdata.bybit_research_universe import BybitResearchUniverseClient
from app.marketdata.bybit_v5 import (
    BybitKlineAcquisition,
    BybitKlineBar,
    BybitKlineRequest,
    BybitPublicKlineClient,
    last_completed_kline_end_ms,
)
from app.strategy.crypto_live_evidence_postgres import (
    PostgresCryptoLiveEvidenceStore,
    extract_evidence_report,
)
from app.strategy.crypto_live_evidence_ranking import (
    CryptoLiveOpportunitySnapshot,
    build_crypto_live_opportunity_snapshot,
)
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, evaluate_crypto_signal
from tools.snapshot_bybit_opportunity_registry import run_public_opportunity_snapshot

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
_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"
_CURRENT_INTERVAL = "5"
_DERIVATIVES_INTERVAL = "5min"
_CURRENT_BAR_COUNT = 240
_DERIVATIVES_LOOKBACK = timedelta(hours=24)


class _KlineClient(Protocol):
    def fetch(self, request: BybitKlineRequest) -> BybitKlineAcquisition: ...


class _DerivativesClient(Protocol):
    def fetch_history(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str = "5min",
    ) -> BybitDerivativesHistory: ...


def run_live_evidence_refresh(
    *,
    evidence_report: Mapping[str, Any],
    observed_at: datetime | None = None,
    bybit_site: str = "global",
    equity_usdt: Decimal = Decimal("1000"),
    equity_source: str = "RESEARCH_REFERENCE",
    registry_limit: int = 50,
    universe_client: BybitResearchUniverseClient | None = None,
    kline_client: _KlineClient | None = None,
    derivatives_client: _DerivativesClient | None = None,
) -> tuple[Any, CryptoLiveOpportunitySnapshot]:
    """Capture market candidates and rank only current fixed-strategy signals by evidence."""

    if observed_at is None:
        cutoff = datetime.now(UTC)
    else:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("live evidence refresh observed_at must be timezone-aware")
        cutoff = observed_at.astimezone(UTC)
    if not equity_usdt.is_finite() or equity_usdt <= 0:
        raise ValueError("live evidence refresh equity must be positive and finite")
    if not equity_source.strip():
        raise ValueError("live evidence refresh equity source is required")
    host = _site_host(bybit_site)
    universe = (
        BybitResearchUniverseClient(host=host)
        if universe_client is None
        else universe_client
    )
    market_snapshot = run_public_opportunity_snapshot(
        observed_at=cutoff,
        bybit_site=bybit_site,
        registry_limit=registry_limit,
        universe_client=universe,
    )
    if not market_snapshot.candidates:
        raise RuntimeError("live evidence refresh has no eligible market candidates")

    symbols = tuple(item.symbol for item in market_snapshot.candidates)
    end_ms = last_completed_kline_end_ms(
        now_ms=market_snapshot.observed_at_ms,
        interval=_CURRENT_INTERVAL,
    )
    interval_ms = 5 * 60 * 1000
    start_ms = end_ms - _CURRENT_BAR_COUNT * interval_ms
    request = BybitKlineRequest(
        symbols=symbols,
        start_ms=start_ms,
        end_ms=end_ms,
        interval=_CURRENT_INTERVAL,
        maximum_pages_per_symbol=5,
    )
    public_klines = BybitPublicKlineClient() if kline_client is None else kline_client
    acquisition = public_klines.fetch(request)
    acquisition.validate(requested_symbols=symbols, minimum_bars=60)
    bars_by_symbol = _bars_by_symbol(acquisition)

    config = CryptoPerpStrategyConfig()
    signal_decision_ms: dict[str, int] = {}
    for symbol, bars in bars_by_symbol.items():
        evaluation = evaluate_crypto_signal(bars, config)
        if not evaluation.eligible or evaluation.signal is None:
            continue
        decision = datetime.fromisoformat(evaluation.signal.decision_time)
        signal_decision_ms[symbol] = int(decision.timestamp() * 1000)

    public_derivatives = (
        BybitHistoricalDerivativesClient(host=host)
        if derivatives_client is None
        else derivatives_client
    )
    derivatives_histories: dict[str, BybitDerivativesHistory] = {}
    for symbol, decision_ms in sorted(signal_decision_ms.items()):
        start_context_ms = int(
            (
                datetime.fromtimestamp(decision_ms / 1000, tz=UTC)
                - _DERIVATIVES_LOOKBACK
            ).timestamp()
            * 1000
        )
        history = public_derivatives.fetch_history(
            symbol=symbol,
            start_ms=start_context_ms,
            end_ms=decision_ms,
            interval=_DERIVATIVES_INTERVAL,
        )
        history.validate()
        if history.symbol != symbol:
            raise RuntimeError("live evidence derivatives history symbol mismatch")
        derivatives_histories[symbol] = history

    ranked = build_crypto_live_opportunity_snapshot(
        market_snapshot,
        bars_by_symbol=bars_by_symbol,
        derivatives_histories=derivatives_histories,
        evidence_report=evidence_report,
        equity_usdt=equity_usdt,
        equity_source=equity_source,
    )
    return market_snapshot, ranked


def persist_live_refresh(
    market_snapshot: Any,
    ranked: CryptoLiveOpportunitySnapshot,
    *,
    evidence_report: Mapping[str, Any],
    evidence_observed_at: datetime,
    dsn: str,
    migrate: bool = False,
) -> tuple[str, str, str]:
    market_store = PostgresBybitOpportunityStore(dsn)
    evidence_store = PostgresCryptoLiveEvidenceStore(dsn)
    if migrate:
        market_store.migrate()
        evidence_store.migrate()
    market_id = market_store.persist(market_snapshot)
    evidence_id = evidence_store.persist_evidence_report(
        evidence_report,
        observed_at=evidence_observed_at,
    )
    if evidence_id != ranked.evidence_snapshot_id:
        raise ValueError("live evidence persistence id differs from ranked evidence id")
    ranked_id = evidence_store.persist_opportunity_snapshot(ranked)
    return market_id, evidence_id, ranked_id


def _bars_by_symbol(acquisition: BybitKlineAcquisition) -> dict[str, tuple[BybitKlineBar, ...]]:
    grouped: dict[str, list[BybitKlineBar]] = defaultdict(list)
    for bar in acquisition.bars:
        bar.validate()
        grouped[bar.symbol].append(bar)
    result: dict[str, tuple[BybitKlineBar, ...]] = {}
    for symbol, rows in grouped.items():
        ordered = tuple(sorted(rows, key=lambda item: item.start_time))
        if len({item.start_time for item in ordered}) != len(ordered):
            raise ValueError("live evidence kline acquisition has duplicate symbol/timestamp")
        result[symbol] = ordered
    return result


def _load_json(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("live evidence JSON must be an object")
    return payload


def _load_evidence(
    *,
    evidence_json: Path | None,
    dsn: str | None,
    evidence_observed_at: str | None,
) -> tuple[Mapping[str, Any], datetime | None]:
    if evidence_json is not None:
        report, embedded_observed = extract_evidence_report(_load_json(evidence_json))
        if embedded_observed is not None:
            return report, embedded_observed
        if evidence_observed_at is None:
            return report, None
        parsed = datetime.fromisoformat(evidence_observed_at)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("--evidence-observed-at must be timezone-aware")
        return report, parsed.astimezone(UTC)
    if dsn is None:
        raise RuntimeError("live evidence refresh needs --evidence-json or PostgreSQL DSN")
    report = PostgresCryptoLiveEvidenceStore(dsn).latest_evidence_report()
    if report is None:
        raise RuntimeError("live evidence refresh PostgreSQL has no evidence snapshot")
    return report, None


def _site_host(site: str) -> str:
    normalized = site.strip().lower()
    if normalized != site or normalized not in _SITE_HOSTS:
        raise ValueError("Bybit site must be one of " + ",".join(sorted(_SITE_HOSTS)))
    return _SITE_HOSTS[normalized]


def _write_snapshot(snapshot: CryptoLiveOpportunitySnapshot, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(snapshot.to_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh the current Bybit fixed-strategy evidence registry without enabling order "
            "routing."
        )
    )
    parser.add_argument(
        "--site",
        default=os.environ.get("BYBIT_MAINNET_READONLY_SITE", "global"),
        choices=sorted(_SITE_HOSTS),
    )
    parser.add_argument("--registry-limit", type=int, default=50)
    parser.add_argument("--equity", default="1000")
    parser.add_argument("--equity-source", default="RESEARCH_REFERENCE")
    parser.add_argument("--evidence-json", type=Path)
    parser.add_argument("--evidence-observed-at")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--persist-postgres", action="store_true")
    parser.add_argument("--migrate-postgres", action="store_true")
    parser.add_argument("--database-dsn-env", default=_DSN_ENV)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.migrate_postgres and not args.persist_postgres:
        raise SystemExit("--migrate-postgres requires --persist-postgres")
    dsn = os.environ.get(args.database_dsn_env, "").strip() or None
    report, evidence_observed_at = _load_evidence(
        evidence_json=args.evidence_json,
        dsn=dsn,
        evidence_observed_at=args.evidence_observed_at,
    )
    market_snapshot, ranked = run_live_evidence_refresh(
        evidence_report=report,
        bybit_site=args.site,
        equity_usdt=Decimal(args.equity),
        equity_source=args.equity_source,
        registry_limit=args.registry_limit,
    )
    _write_snapshot(ranked, args.output)
    persisted = False
    if args.persist_postgres:
        if dsn is None:
            raise RuntimeError(
                f"required PostgreSQL DSN environment variable is missing:{args.database_dsn_env}"
            )
        if evidence_observed_at is None and args.evidence_json is not None:
            raise RuntimeError(
                "direct evidence matrix persistence requires --evidence-observed-at"
            )
        evidence_time = (
            datetime.now(UTC) if evidence_observed_at is None else evidence_observed_at
        )
        persist_live_refresh(
            market_snapshot,
            ranked,
            evidence_report=report,
            evidence_observed_at=evidence_time,
            dsn=dsn,
            migrate=args.migrate_postgres,
        )
        persisted = True
    summary = {
        "snapshot_id": ranked.snapshot_id,
        "market_snapshot_id": ranked.market_snapshot_id,
        "evidence_snapshot_id": ranked.evidence_snapshot_id,
        "qualified_positive_count": ranked.qualified_positive_count,
        "qualified_mixed_count": ranked.qualified_mixed_count,
        "candidate_count": len(ranked.opportunities),
        "postgres_persisted": persisted,
        "operator_review_required": True,
        "trade_actionable": False,
        "bybit_live_order_routing_allowed": False,
    }
    print("BYBIT_LIVE_EVIDENCE_REGISTRY=" + json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
