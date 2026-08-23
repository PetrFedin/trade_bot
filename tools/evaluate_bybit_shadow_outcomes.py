from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.marketdata.bybit_v5 import (
    BybitKlineAcquisition,
    BybitKlineRequest,
    BybitPublicKlineClient,
    last_completed_kline_end_ms,
)
from app.strategy.crypto_shadow_outcome_postgres import PostgresCryptoShadowOutcomeStore
from app.strategy.crypto_shadow_outcomes import (
    CryptoShadowOutcome,
    CryptoShadowSeed,
    CryptoShadowSourceCandidate,
    evaluate_crypto_shadow_outcome,
    reconstruct_crypto_shadow_seed,
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
_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"
_INTERVAL = "5"
_INTERVAL_MS = 5 * 60 * 1000
_DECISION_HISTORY_BARS = 240
_FINAL_HORIZON = timedelta(minutes=240)


class _KlineClient(Protocol):
    def fetch(self, request: BybitKlineRequest) -> BybitKlineAcquisition: ...


class _ShadowStore(Protocol):
    def unseeded_sources(
        self,
        *,
        limit: int = 200,
    ) -> tuple[CryptoShadowSourceCandidate, ...]: ...

    def persist_seed(self, seed: CryptoShadowSeed) -> str: ...

    def active_seeds(self, *, limit: int = 500) -> tuple[CryptoShadowSeed, ...]: ...

    def persist_outcome(self, outcome: CryptoShadowOutcome) -> str: ...


@dataclass(frozen=True)
class CryptoShadowCycleSummary:
    observed_at: str
    host: str
    source_count: int
    seeds_created: int
    active_seed_count: int
    outcomes_persisted: int
    final_outcomes_persisted: int
    prospective: bool = True
    operator_review_required: bool = True
    trade_actionable: bool = False
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False
    bybit_live_order_routing_allowed: bool = False



def run_shadow_outcome_cycle(
    store: _ShadowStore,
    *,
    observed_at: datetime | None = None,
    bybit_site: str = "global",
    kline_client: _KlineClient | None = None,
    source_limit: int = 200,
    active_limit: int = 500,
) -> CryptoShadowCycleSummary:
    cutoff = _utc(datetime.now(UTC) if observed_at is None else observed_at)
    host = _site_host(bybit_site)
    public_klines = (
        BybitPublicKlineClient(host=host) if kline_client is None else kline_client
    )

    sources = store.unseeded_sources(limit=source_limit)
    seeds_created = 0
    for source in sources:
        history = _fetch_decision_history(public_klines, source)
        seed = reconstruct_crypto_shadow_seed(source, history.bars)
        store.persist_seed(seed)
        seeds_created += 1

    active = store.active_seeds(limit=active_limit)
    outcomes_persisted = 0
    final_outcomes = 0
    for seed in active:
        window = _outcome_request(seed, cutoff=cutoff)
        if window is None:
            continue
        request, observed_through = window
        acquisition = public_klines.fetch(request)
        acquisition.validate(requested_symbols=(seed.symbol,), minimum_bars=1)
        outcome = evaluate_crypto_shadow_outcome(
            seed,
            acquisition.bars,
            observed_through=observed_through,
        )
        store.persist_outcome(outcome)
        outcomes_persisted += 1
        if outcome.final:
            final_outcomes += 1

    return CryptoShadowCycleSummary(
        observed_at=cutoff.isoformat(),
        host=host,
        source_count=len(sources),
        seeds_created=seeds_created,
        active_seed_count=len(active),
        outcomes_persisted=outcomes_persisted,
        final_outcomes_persisted=final_outcomes,
    )


def _fetch_decision_history(
    client: _KlineClient,
    source: CryptoShadowSourceCandidate,
) -> BybitKlineAcquisition:
    source.validate()
    decision = datetime.fromisoformat(source.decision_time).astimezone(UTC)
    decision_ms = int(decision.timestamp() * 1000)
    start_ms = decision_ms - (_DECISION_HISTORY_BARS - 1) * _INTERVAL_MS
    request = BybitKlineRequest(
        symbols=(source.symbol,),
        start_ms=start_ms,
        end_ms=decision_ms + _INTERVAL_MS - 1,
        interval=_INTERVAL,
        maximum_pages_per_symbol=5,
    )
    acquisition = client.fetch(request)
    acquisition.validate(requested_symbols=(source.symbol,), minimum_bars=60)
    return acquisition


def _outcome_request(
    seed: CryptoShadowSeed,
    *,
    cutoff: datetime,
) -> tuple[BybitKlineRequest, datetime] | None:
    seed.validate()
    available = datetime.fromisoformat(seed.signal_available_at).astimezone(UTC)
    available_ms = int(available.timestamp() * 1000)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    completed_end_ms = last_completed_kline_end_ms(
        now_ms=cutoff_ms,
        interval=_INTERVAL,
    )
    final_end = available + _FINAL_HORIZON
    final_end_ms = int(final_end.timestamp() * 1000)
    end_ms = min(completed_end_ms, final_end_ms - 1)
    if end_ms < available_ms + _INTERVAL_MS - 1:
        return None
    observed_through = datetime.fromtimestamp((end_ms + 1) / 1000, tz=UTC)
    request = BybitKlineRequest(
        symbols=(seed.symbol,),
        start_ms=available_ms,
        end_ms=end_ms,
        interval=_INTERVAL,
        maximum_pages_per_symbol=5,
    )
    return request, observed_through


def _site_host(site: str) -> str:
    normalized = site.strip().lower()
    if normalized != site or normalized not in _SITE_HOSTS:
        raise ValueError("Bybit site must be one of " + ",".join(sorted(_SITE_HOSTS)))
    return _SITE_HOSTS[normalized]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("shadow cycle timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate prospective Bybit opportunity outcomes from completed 5-minute bars. "
            "This command never creates, amends, or cancels orders."
        )
    )
    parser.add_argument(
        "--site",
        default=os.environ.get("BYBIT_MAINNET_READONLY_SITE", "global"),
        choices=sorted(_SITE_HOSTS),
    )
    parser.add_argument("--source-limit", type=int, default=200)
    parser.add_argument("--active-limit", type=int, default=500)
    parser.add_argument("--migrate-postgres", action="store_true")
    parser.add_argument("--database-dsn-env", default=_DSN_ENV)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    dsn = os.environ.get(args.database_dsn_env, "").strip()
    if not dsn:
        raise RuntimeError(
            f"required PostgreSQL DSN environment variable is missing:{args.database_dsn_env}"
        )
    store = PostgresCryptoShadowOutcomeStore(dsn)
    if args.migrate_postgres:
        store.migrate()
    summary = run_shadow_outcome_cycle(
        store,
        bybit_site=args.site,
        source_limit=args.source_limit,
        active_limit=args.active_limit,
    )
    print("BYBIT_PROSPECTIVE_SHADOW_OUTCOMES=" + json.dumps(asdict(summary), sort_keys=True))


if __name__ == "__main__":
    main()
