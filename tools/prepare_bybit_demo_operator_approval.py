from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.execution.bybit_demo_operator_approval import (
    create_bybit_demo_operator_approval,
)
from app.marketdata.bybit_v5 import (
    BybitKlineBar,
    BybitKlineRequest,
    BybitPublicKlineClient,
)
from app.strategy.crypto_live_opportunity_reader import PostgresCryptoLiveOpportunityReader
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, minimum_history_bars

_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"
_INTERVAL_MS = 5 * 60 * 1000
_HISTORY_BAR_COUNT = 240
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


class _ReviewReader(Protocol):
    def latest_review_queue(
        self,
        *,
        limit: int = 10,
        include_mixed: bool = False,
    ) -> tuple[Mapping[str, Any], ...]: ...


class _KlineClient(Protocol):
    def fetch(self, request: BybitKlineRequest): ...


@dataclass(frozen=True)
class BybitDemoOperatorApprovalSourceContext:
    review_row: Mapping[str, Any]
    bars: tuple[BybitKlineBar, ...]


def resolve_bybit_demo_operator_approval_source(
    reader: _ReviewReader,
    kline_client: _KlineClient,
    *,
    evidence_rank: int,
    expected_symbol: str | None = None,
) -> BybitDemoOperatorApprovalSourceContext:
    """Resolve the exact latest review row and fixed decision history without mutation."""

    if isinstance(evidence_rank, bool) or not 1 <= evidence_rank <= 50:
        raise ValueError("demo approval evidence rank must be within [1, 50]")
    rows = reader.latest_review_queue(limit=50, include_mixed=False)
    matches = [row for row in rows if row.get("evidence_rank") == evidence_rank]
    if len(matches) != 1:
        raise RuntimeError(
            "demo approval requires exactly one latest positive-evidence row at rank "
            f"{evidence_rank}"
        )
    row = matches[0]
    symbol = row.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("demo approval review row symbol is missing")
    if expected_symbol is not None and symbol != expected_symbol:
        raise ValueError("demo approval evidence rank no longer matches expected symbol")
    decision_text = row.get("decision_time")
    if not isinstance(decision_text, str):
        raise ValueError("demo approval review row decision time is missing")
    decision = datetime.fromisoformat(decision_text)
    if decision.tzinfo is None or decision.utcoffset() is None:
        raise ValueError("demo approval review decision time must be timezone-aware")
    decision_ms = int(decision.astimezone(UTC).timestamp() * 1000)
    start_ms = decision_ms - (_HISTORY_BAR_COUNT - 1) * _INTERVAL_MS
    request = BybitKlineRequest(
        symbols=(symbol,),
        start_ms=start_ms,
        end_ms=decision_ms,
        interval="5",
        limit=1000,
        maximum_pages_per_symbol=2,
    )
    acquisition = kline_client.fetch(request)
    acquisition.validate(
        requested_symbols=(symbol,),
        minimum_bars=minimum_history_bars(CryptoPerpStrategyConfig()),
    )
    bars = tuple(acquisition.bars)
    if any(bar.symbol != symbol for bar in bars):
        raise ValueError("demo approval source acquisition returned another symbol")
    return BybitDemoOperatorApprovalSourceContext(
        review_row=dict(row),
        bars=bars,
    )


def prepare_bybit_demo_operator_approval(
    reader: _ReviewReader,
    kline_client: _KlineClient,
    *,
    evidence_rank: int,
    approved_at: datetime,
    confirmation_phrase: str,
    expected_symbol: str | None = None,
    ttl_seconds: int = 120,
) -> dict[str, Any]:
    """Prepare one ephemeral demo approval without performing any order mutation."""

    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise ValueError("demo approval preparation time must be timezone-aware")
    source = resolve_bybit_demo_operator_approval_source(
        reader,
        kline_client,
        evidence_rank=evidence_rank,
        expected_symbol=expected_symbol,
    )
    approval = create_bybit_demo_operator_approval(
        source.review_row,
        source.bars,
        approved_at=approved_at.astimezone(UTC),
        confirmation_phrase=confirmation_phrase,
        ttl_seconds=ttl_seconds,
    )
    return {
        "report": "BYBIT_OPERATOR_APPROVED_DEMO_PREPARATION",
        "prepared_at": approved_at.astimezone(UTC).isoformat(),
        "source_evidence_rank": evidence_rank,
        "source_symbol": approval.symbol,
        "approval": approval.to_payload(),
        "order_write_performed": False,
        "prepared_only": True,
        "environment": "BYBIT_DEMO",
        "live_mainnet_order_routing_allowed": False,
    }


def _site_host(site: str) -> str:
    normalized = site.strip().lower()
    if normalized != site or normalized not in _SITE_HOSTS:
        raise ValueError("unsupported Bybit research site")
    return _SITE_HOSTS[normalized]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare a short-lived operator approval for one latest positive-evidence Bybit "
            "Demo candidate. This command never sends an order."
        )
    )
    parser.add_argument("--evidence-rank", type=int, required=True)
    parser.add_argument("--symbol")
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=120)
    parser.add_argument(
        "--site",
        default=os.environ.get("BYBIT_RESEARCH_SITE", "global"),
        choices=sorted(_SITE_HOSTS),
    )
    parser.add_argument("--dsn-env", default=_DSN_ENV)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    dsn = os.getenv(args.dsn_env, "").strip()
    if not dsn:
        raise RuntimeError("demo approval PostgreSQL DSN environment is missing")
    host = _site_host(args.site)
    report = prepare_bybit_demo_operator_approval(
        PostgresCryptoLiveOpportunityReader(dsn),
        BybitPublicKlineClient(host=host),
        evidence_rank=args.evidence_rank,
        expected_symbol=args.symbol,
        approved_at=datetime.now(UTC),
        confirmation_phrase=args.confirm,
        ttl_seconds=args.ttl_seconds,
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
