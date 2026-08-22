from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchTicker,
    BybitResearchUniversePolicy,
    select_bybit_research_universe,
    validate_bybit_public_research_host,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_SCORE_COMPONENTS = (
    "TURNOVER_24H",
    "OPEN_INTEREST_VALUE",
    "SPREAD_QUALITY",
    "LISTING_HISTORY",
)


@dataclass(frozen=True)
class BybitOpportunityCandidate:
    rank: int
    symbol: str
    is_top10: bool
    universe_score: Decimal
    listing_days: int
    turnover_24h_usdt: Decimal
    open_interest_value_usdt: Decimal
    spread_bps: Decimal
    funding_rate: Decimal
    price_24h_fraction: Decimal
    turnover_percentile: Decimal
    open_interest_percentile: Decimal
    spread_quality_percentile: Decimal
    history_percentile: Decimal
    rank_drivers: tuple[str, ...]
    signal_side: str = "UNASSIGNED"
    trade_actionable: bool = False
    strategy_promotion_allowed: bool = False
    live_activation_allowed: bool = False
    bybit_live_order_routing_allowed: bool = False

    def validate(self) -> None:
        if not 1 <= self.rank <= 50:
            raise ValueError("Bybit opportunity candidate rank must be within [1, 50]")
        if self.symbol != self.symbol.strip().upper() or not self.symbol:
            raise ValueError("Bybit opportunity candidate symbol is invalid")
        if self.is_top10 != (self.rank <= 10):
            raise ValueError("Bybit opportunity candidate Top-10 flag is inconsistent")
        if not self.universe_score.is_finite() or not _ZERO <= self.universe_score <= _ONE:
            raise ValueError("Bybit opportunity candidate universe score must be within [0, 1]")
        if self.listing_days < 0:
            raise ValueError("Bybit opportunity candidate listing age cannot be negative")
        for name, value in (
            ("turnover_24h_usdt", self.turnover_24h_usdt),
            ("open_interest_value_usdt", self.open_interest_value_usdt),
            ("spread_bps", self.spread_bps),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(
                    f"Bybit opportunity candidate {name} must be finite and non-negative"
                )
        for name, value in (
            ("funding_rate", self.funding_rate),
            ("price_24h_fraction", self.price_24h_fraction),
        ):
            if not value.is_finite():
                raise ValueError(f"Bybit opportunity candidate {name} must be finite")
        for name, value in (
            ("turnover_percentile", self.turnover_percentile),
            ("open_interest_percentile", self.open_interest_percentile),
            ("spread_quality_percentile", self.spread_quality_percentile),
            ("history_percentile", self.history_percentile),
        ):
            if not value.is_finite() or not _ZERO <= value <= _ONE:
                raise ValueError(
                    f"Bybit opportunity candidate {name} must be within [0, 1]"
                )
        if len(self.rank_drivers) != len(_SCORE_COMPONENTS):
            raise ValueError("Bybit opportunity candidate rank drivers are incomplete")
        if set(self.rank_drivers) != set(_SCORE_COMPONENTS):
            raise ValueError("Bybit opportunity candidate rank drivers are invalid")
        if self.signal_side != "UNASSIGNED":
            raise ValueError("market-universe opportunity cannot pre-assign LONG/SHORT side")
        if (
            self.trade_actionable
            or self.strategy_promotion_allowed
            or self.live_activation_allowed
            or self.bybit_live_order_routing_allowed
        ):
            raise ValueError("Bybit market-universe opportunity cannot activate trading")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "is_top10": self.is_top10,
            "universe_score": str(self.universe_score),
            "listing_days": self.listing_days,
            "turnover_24h_usdt": str(self.turnover_24h_usdt),
            "open_interest_value_usdt": str(self.open_interest_value_usdt),
            "spread_bps": str(self.spread_bps),
            "funding_rate": str(self.funding_rate),
            "price_24h_fraction": str(self.price_24h_fraction),
            "turnover_percentile": str(self.turnover_percentile),
            "open_interest_percentile": str(self.open_interest_percentile),
            "spread_quality_percentile": str(self.spread_quality_percentile),
            "history_percentile": str(self.history_percentile),
            "rank_drivers": list(self.rank_drivers),
            "signal_side": self.signal_side,
            "trade_actionable": self.trade_actionable,
            "strategy_promotion_allowed": self.strategy_promotion_allowed,
            "live_activation_allowed": self.live_activation_allowed,
            "bybit_live_order_routing_allowed": self.bybit_live_order_routing_allowed,
        }


@dataclass(frozen=True)
class BybitOpportunitySnapshot:
    observed_at_ms: int
    host: str
    registry_limit: int
    candidates: tuple[BybitOpportunityCandidate, ...]
    eligible_symbol_count: int
    source_instrument_count: int
    source_ticker_count: int
    excluded_reasons: Mapping[str, tuple[str, ...]]
    universe_policy: BybitResearchUniversePolicy
    top10_complete: bool
    top10_symbols: tuple[str, ...]
    registry_population_complete: bool
    blockers: tuple[str, ...]
    research_only: bool = True
    trade_actionable: bool = False
    strategy_parameters_changed: bool = False
    strategy_promotion_allowed: bool = False
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False
    bybit_live_order_routing_allowed: bool = False

    def validate(self) -> None:
        validate_bybit_public_research_host(self.host)
        self.universe_policy.validate()
        if self.universe_policy.top_n != 10:
            raise ValueError("Bybit opportunity snapshot requires universe policy top_n=10")
        if isinstance(self.observed_at_ms, bool) or not isinstance(self.observed_at_ms, int):
            raise ValueError("Bybit opportunity snapshot observed_at_ms must be integer")
        if self.observed_at_ms < 0:
            raise ValueError("Bybit opportunity snapshot observed_at_ms cannot be negative")
        if not 10 <= self.registry_limit <= 50:
            raise ValueError("Bybit opportunity registry limit must be within [10, 50]")
        if len(self.candidates) > self.registry_limit:
            raise ValueError("Bybit opportunity snapshot exceeds registry limit")
        if self.eligible_symbol_count < len(self.candidates):
            raise ValueError("Bybit opportunity eligible count is inconsistent")
        if self.source_instrument_count < self.eligible_symbol_count:
            raise ValueError("Bybit opportunity instrument count is inconsistent")
        if self.source_ticker_count < self.eligible_symbol_count:
            raise ValueError("Bybit opportunity ticker count is inconsistent")
        expected_complete = self.eligible_symbol_count >= 10 and len(self.candidates) >= 10
        if self.top10_complete != expected_complete:
            raise ValueError("Bybit opportunity Top-10 completeness is inconsistent")
        expected_top10 = tuple(item.symbol for item in self.candidates[:10])
        if self.top10_symbols != expected_top10:
            raise ValueError("Bybit opportunity Top-10 symbols are inconsistent")
        expected_population_complete = self.eligible_symbol_count <= self.registry_limit
        if self.registry_population_complete != expected_population_complete:
            raise ValueError("Bybit opportunity registry population completeness is inconsistent")
        if self.top10_complete and self.blockers:
            raise ValueError("complete Bybit opportunity Top-10 cannot carry blockers")
        if not self.top10_complete and self.blockers != (
            "INSUFFICIENT_ELIGIBLE_SYMBOLS_FOR_TOP10",
        ):
            raise ValueError("incomplete Bybit opportunity Top-10 must carry explicit blocker")
        previous_score: Decimal | None = None
        seen_symbols: set[str] = set()
        for expected_rank, candidate in enumerate(self.candidates, start=1):
            candidate.validate()
            if candidate.rank != expected_rank:
                raise ValueError("Bybit opportunity ranks must be contiguous")
            if candidate.symbol in seen_symbols:
                raise ValueError("Bybit opportunity snapshot contains duplicate symbols")
            if previous_score is not None and candidate.universe_score > previous_score:
                raise ValueError("Bybit opportunity candidates must be score-descending")
            previous_score = candidate.universe_score
            seen_symbols.add(candidate.symbol)
        if (
            not self.research_only
            or self.trade_actionable
            or self.strategy_parameters_changed
            or self.strategy_promotion_allowed
            or self.demo_activation_allowed
            or self.live_activation_allowed
            or self.bybit_live_order_routing_allowed
        ):
            raise ValueError("Bybit opportunity snapshot cannot grant trading activation")

    @property
    def snapshot_id(self) -> str:
        canonical = json.dumps(
            self.to_payload(include_snapshot_id=False),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_payload(self, *, include_snapshot_id: bool = True) -> dict[str, Any]:
        self.validate()
        policy = self.universe_policy
        payload: dict[str, Any] = {
            "schema": "BYBIT_OPPORTUNITY_REGISTRY_V110",
            "observed_at_ms": self.observed_at_ms,
            "host": self.host,
            "registry_limit": self.registry_limit,
            "eligible_symbol_count": self.eligible_symbol_count,
            "source_instrument_count": self.source_instrument_count,
            "source_ticker_count": self.source_ticker_count,
            "top10_complete": self.top10_complete,
            "top10_symbols": list(self.top10_symbols),
            "registry_population_complete": self.registry_population_complete,
            "blockers": list(self.blockers),
            "universe_policy": {
                "top_n": policy.top_n,
                "minimum_listing_days": policy.minimum_listing_days,
                "minimum_turnover_24h_usdt": str(policy.minimum_turnover_24h_usdt),
                "minimum_open_interest_value_usdt": str(
                    policy.minimum_open_interest_value_usdt
                ),
                "maximum_spread_bps": str(policy.maximum_spread_bps),
                "maximum_abs_funding_rate": str(policy.maximum_abs_funding_rate),
                "turnover_weight": str(policy.turnover_weight),
                "open_interest_weight": str(policy.open_interest_weight),
                "spread_weight": str(policy.spread_weight),
                "history_weight": str(policy.history_weight),
            },
            "candidates": [candidate.to_payload() for candidate in self.candidates],
            "excluded_reasons": {
                symbol: list(reasons)
                for symbol, reasons in sorted(self.excluded_reasons.items())
            },
            "ranking_semantics": (
                "market suitability only: liquidity, open interest, spread quality and listing "
                "history; LONG/SHORT strategy signal is intentionally unassigned"
            ),
            "research_only": self.research_only,
            "trade_actionable": self.trade_actionable,
            "strategy_parameters_changed": self.strategy_parameters_changed,
            "strategy_promotion_allowed": self.strategy_promotion_allowed,
            "demo_activation_allowed": self.demo_activation_allowed,
            "live_activation_allowed": self.live_activation_allowed,
            "bybit_live_order_routing_allowed": self.bybit_live_order_routing_allowed,
            "causal_claim_allowed": False,
            "predictive_guarantee_allowed": False,
        }
        if include_snapshot_id:
            payload["snapshot_id"] = self.snapshot_id
        return payload


def build_bybit_opportunity_snapshot(
    instruments: Sequence[BybitResearchInstrument],
    tickers: Sequence[BybitResearchTicker],
    *,
    observed_at_ms: int,
    host: str = "api.bybit.com",
    universe_policy: BybitResearchUniversePolicy | None = None,
    registry_limit: int = 50,
) -> BybitOpportunitySnapshot:
    active = BybitResearchUniversePolicy() if universe_policy is None else universe_policy
    active.validate()
    if active.top_n != 10:
        raise ValueError("Bybit opportunity registry requires universe_policy.top_n=10")
    if isinstance(registry_limit, bool) or not 10 <= registry_limit <= 50:
        raise ValueError("Bybit opportunity registry limit must be within [10, 50]")
    expanded_policy = replace(active, top_n=registry_limit)
    selection = select_bybit_research_universe(
        instruments,
        tickers,
        observed_at_ms=observed_at_ms,
        host=host,
        policy=expanded_policy,
    )
    candidates = tuple(
        _opportunity_candidate(item, active)
        for item in selection.selected
    )
    top10_complete = selection.eligible_symbol_count >= 10 and len(candidates) >= 10
    blockers = () if top10_complete else ("INSUFFICIENT_ELIGIBLE_SYMBOLS_FOR_TOP10",)
    snapshot = BybitOpportunitySnapshot(
        observed_at_ms=observed_at_ms,
        host=selection.host,
        registry_limit=registry_limit,
        candidates=candidates,
        eligible_symbol_count=selection.eligible_symbol_count,
        source_instrument_count=selection.source_instrument_count,
        source_ticker_count=selection.source_ticker_count,
        excluded_reasons=selection.excluded_reasons,
        universe_policy=active,
        top10_complete=top10_complete,
        top10_symbols=tuple(item.symbol for item in candidates[:10]),
        registry_population_complete=selection.eligible_symbol_count <= registry_limit,
        blockers=blockers,
    )
    snapshot.validate()
    return snapshot


def _opportunity_candidate(
    item: Any,
    policy: BybitResearchUniversePolicy,
) -> BybitOpportunityCandidate:
    contributions = {
        "TURNOVER_24H": item.turnover_percentile * policy.turnover_weight,
        "OPEN_INTEREST_VALUE": item.open_interest_percentile * policy.open_interest_weight,
        "SPREAD_QUALITY": item.spread_quality_percentile * policy.spread_weight,
        "LISTING_HISTORY": item.history_percentile * policy.history_weight,
    }
    rank_drivers = tuple(
        name
        for name, _value in sorted(
            contributions.items(),
            key=lambda pair: (-pair[1], pair[0]),
        )
    )
    candidate = BybitOpportunityCandidate(
        rank=item.rank,
        symbol=item.symbol,
        is_top10=item.rank <= 10,
        universe_score=item.score,
        listing_days=item.listing_days,
        turnover_24h_usdt=item.turnover_24h_usdt,
        open_interest_value_usdt=item.open_interest_value_usdt,
        spread_bps=item.spread_bps,
        funding_rate=item.funding_rate,
        price_24h_fraction=item.price_24h_fraction,
        turnover_percentile=item.turnover_percentile,
        open_interest_percentile=item.open_interest_percentile,
        spread_quality_percentile=item.spread_quality_percentile,
        history_percentile=item.history_percentile,
        rank_drivers=rank_drivers,
    )
    candidate.validate()
    return candidate
