from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from app.marketdata.bybit_full_period_5m import build_bybit_full_period_5m_plan
from app.marketdata.bybit_full_period_5m_postgres import (
    BybitFullPeriod5mStoredCoverage,
    PostgresBybitFullPeriod5mStore,
)
from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchUniverseClient,
    BybitResearchUniversePolicy,
    select_bybit_research_universe,
)
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_full_period_fixed_replay import (
    qualified_fixed_strategy_contract_fingerprint,
    run_qualified_full_period_symbol_replay,
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


class _UniverseClient(Protocol):
    def fetch_instruments(self): ...

    def fetch_tickers(self): ...


class _HistoryStore(Protocol):
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


def run_full_period_fixed_strategy_research(
    store: _HistoryStore,
    *,
    output_dir: Path,
    observed_at: datetime | None = None,
    bybit_site: str = "global",
    opening_equity_usdt: Decimal = Decimal("1000"),
    universe_client: _UniverseClient | None = None,
) -> dict[str, Any]:
    cutoff = _utc(datetime.now(UTC) if observed_at is None else observed_at)
    if not opening_equity_usdt.is_finite() or opening_equity_usdt <= 0:
        raise ValueError("full-period research opening equity must be positive and finite")
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
        policy=BybitResearchUniversePolicy(),
    )
    if not selection.complete_top_n or len(selection.selected) != 10:
        raise RuntimeError(
            "full-period fixed replay refused incomplete Top-10 universe:"
            + ",".join(selection.blockers)
        )
    ordered_symbols = tuple(item.symbol for item in selection.selected)
    coverage_symbols = tuple(sorted(ordered_symbols))
    by_symbol = {item.symbol: item for item in instruments}
    if any(symbol not in by_symbol for symbol in ordered_symbols):
        raise RuntimeError("full-period fixed replay lost selected instrument metadata")
    stored = store.coverage_state(coverage_symbols)
    plan = build_bybit_full_period_5m_plan(
        instruments,
        symbols=coverage_symbols,
        observed_at=cutoff,
        completed_by_symbol=stored.completed_by_symbol,
        unavailable_retry_after_by_symbol=stored.unavailable_retry_after_by_symbol,
    )
    if not plan.full_period_complete:
        raise RuntimeError(
            "full-period fixed replay refused incomplete archive-day coverage:"
            f"completed={plan.completed_day_count}:expected={plan.expected_day_count}:"
            f"blocked={plan.blocked_day_count}:pending={plan.pending_day_count}"
        )

    universe_fingerprint = _universe_fingerprint(
        ordered_symbols,
        by_symbol,
        observed_at=cutoff,
    )
    final_dir = output_dir
    if final_dir.exists():
        raise FileExistsError("full-period research output directory must be new")
    staging = final_dir.with_name(final_dir.name + f".tmp-{universe_fingerprint[:12]}")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=False)
    summaries: list[dict[str, Any]] = []
    try:
        for rank, symbol in enumerate(ordered_symbols, start=1):
            instrument = by_symbol[symbol]
            bars = store.load_bars(symbols=(symbol,))
            result = run_qualified_full_period_symbol_replay(
                instrument,
                bars,
                last_archive_date=plan.last_archive_date,
                opening_equity_usdt=opening_equity_usdt,
            )
            result["market_rank_at_research_time"] = rank
            result["universe_fingerprint"] = universe_fingerprint
            file_name = f"{rank:02d}-{symbol}.json"
            payload_bytes = _canonical_pretty_bytes(result)
            (staging / file_name).write_bytes(payload_bytes)
            replay = result["replay"]
            if not isinstance(replay, Mapping):
                raise ValueError("full-period symbol replay payload is invalid")
            closed = replay.get("closed_trades")
            metrics = replay.get("metrics")
            if not isinstance(closed, list) or not isinstance(metrics, Mapping):
                raise ValueError("full-period symbol replay summary fields are invalid")
            summaries.append(
                {
                    "market_rank": rank,
                    "symbol": symbol,
                    "artifact": file_name,
                    "artifact_sha256": hashlib.sha256(payload_bytes).hexdigest(),
                    "expected_bar_count": result["coverage"]["expected_bar_count"],
                    "closed_trade_count": len(closed),
                    "metrics": dict(metrics),
                }
            )
        manifest = {
            "diagnostic": "BYBIT_FULL_PERIOD_FIXED_STRATEGY_RESEARCH",
            "observed_at": cutoff.isoformat(),
            "bybit_site": bybit_site,
            "universe_host": host,
            "top10_symbols": list(ordered_symbols),
            "universe_fingerprint": universe_fingerprint,
            "strategy_contract_fingerprint": (
                qualified_fixed_strategy_contract_fingerprint()
            ),
            "opening_equity_usdt_per_symbol_diagnostic": str(opening_equity_usdt),
            "archive_day_coverage": plan.to_payload(),
            "symbols": summaries,
            "price_history_full_period": True,
            "price_grid_full_period": True,
            "per_symbol_replay_scope": (
                "INDEPENDENT_SINGLE_SYMBOL_DIAGNOSTIC_FROM_LISTING_BUCKET"
            ),
            "portfolio_competition_modeled": False,
            "derivatives_history_full_period": False,
            "full_period_evidence_matrix_allowed": False,
            "reason_full_evidence_matrix_blocked": (
                "FULL_PERIOD_DERIVATIVES_COVERAGE_NOT_VALIDATED"
            ),
            "strategy_parameters_changed": False,
            "parameter_retuning_performed": False,
            "strategy_promotion_allowed": False,
            "demo_activation_allowed": False,
            "live_activation_allowed": False,
            "bybit_live_order_routing_allowed": False,
            "causal_claim_allowed": False,
            "predictive_guarantee_allowed": False,
        }
        (staging / "manifest.json").write_bytes(_canonical_pretty_bytes(manifest))
        staging.replace(final_dir)
        return manifest
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _universe_fingerprint(
    ordered_symbols: Sequence[str],
    instruments: Mapping[str, BybitResearchInstrument],
    *,
    observed_at: datetime,
) -> str:
    payload = {
        "observed_at": observed_at.isoformat(),
        "symbols": [
            {
                "rank": rank,
                "symbol": symbol,
                "launch_time_ms": instruments[symbol].launch_time_ms,
                "contract_type": instruments[symbol].contract_type,
                "settle_coin": instruments[symbol].settle_coin,
            }
            for rank, symbol in enumerate(ordered_symbols, start=1)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()


def _canonical_pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _site_host(site: str) -> str:
    normalized = site.strip().lower()
    if normalized != site or normalized not in _SITE_HOSTS:
        raise ValueError("Bybit site must be one of " + ",".join(sorted(_SITE_HOSTS)))
    return _SITE_HOSTS[normalized]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("full-period research timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the unchanged qualified fixed strategy independently on complete v113 "
            "5-minute histories. No 28-day fallback or order routing is allowed."
        )
    )
    parser.add_argument(
        "--site",
        default=os.environ.get("BYBIT_MAINNET_READONLY_SITE", "global"),
        choices=sorted(_SITE_HOSTS),
    )
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--database-dsn-env", default=_DSN_ENV)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    dsn = os.environ.get(args.database_dsn_env, "").strip()
    if not dsn:
        raise RuntimeError(
            f"required PostgreSQL DSN environment variable is missing:{args.database_dsn_env}"
        )
    store = PostgresBybitFullPeriod5mStore(dsn)
    manifest = run_full_period_fixed_strategy_research(
        store,
        output_dir=args.output_dir,
        bybit_site=args.site,
        opening_equity_usdt=Decimal(args.opening_equity),
    )
    summary = {
        "observed_at": manifest["observed_at"],
        "top10_symbols": manifest["top10_symbols"],
        "universe_fingerprint": manifest["universe_fingerprint"],
        "strategy_contract_fingerprint": manifest["strategy_contract_fingerprint"],
        "price_history_full_period": True,
        "price_grid_full_period": True,
        "full_period_evidence_matrix_allowed": False,
        "trade_actionable": False,
        "bybit_live_order_routing_allowed": False,
    }
    print("BYBIT_FULL_PERIOD_FIXED_STRATEGY=" + json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
