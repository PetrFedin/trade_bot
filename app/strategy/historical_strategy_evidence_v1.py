from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.strategy.cross_sectional_portfolio import CrossSectionalPortfolioResult


_SCHEMA = "astra-historical-strategy-evidence-v1"


class StrategyProfitabilityVerdict(StrEnum):
    PROFITABILITY_NOT_PROVEN = "PROFITABILITY_NOT_PROVEN"
    RESEARCH_EDGE_CANDIDATE = "RESEARCH_EDGE_CANDIDATE"
    OOS_EDGE_CONFIRMED = "OOS_EDGE_CONFIRMED"


@dataclass(frozen=True)
class HistoricalCostCoverageV1:
    fixed_fees_modelled: bool
    proportional_fees_modelled: bool
    slippage_modelled: bool
    funding_modelled: bool
    queue_position_modelled: bool
    partial_fills_modelled: bool

    @property
    def institutional_cost_complete(self) -> bool:
        return (
            self.proportional_fees_modelled
            and self.slippage_modelled
            and self.funding_modelled
        )

    def payload(self) -> dict[str, bool]:
        return {
            "fixed_fees_modelled": self.fixed_fees_modelled,
            "proportional_fees_modelled": self.proportional_fees_modelled,
            "slippage_modelled": self.slippage_modelled,
            "funding_modelled": self.funding_modelled,
            "queue_position_modelled": self.queue_position_modelled,
            "partial_fills_modelled": self.partial_fills_modelled,
            "institutional_cost_complete": self.institutional_cost_complete,
        }


@dataclass(frozen=True)
class HistoricalBenchmarkV1:
    benchmark_id: str
    total_return: Decimal

    def validate(self) -> None:
        if not self.benchmark_id.strip():
            raise ValueError("benchmark_id is required")
        if not self.total_return.is_finite():
            raise ValueError("benchmark total_return must be finite")

    def payload(self) -> dict[str, str]:
        self.validate()
        return {
            "benchmark_id": self.benchmark_id,
            "total_return": str(self.total_return),
        }


@dataclass(frozen=True)
class HistoricalStrategyEvidenceV1:
    strategy_id: str
    dataset_id: str
    dataset_sha256: str
    start: str
    end: str
    symbols: tuple[str, ...]
    opening_cash: Decimal
    ending_equity: Decimal
    total_pnl: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    max_drawdown_fraction: Decimal
    turnover_fraction: Decimal
    fees_paid: Decimal
    closed_trade_count: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal | None
    profit_factor: Decimal | None
    maximum_gross_exposure_fraction: Decimal
    maximum_concurrent_positions: int
    gross_closed_trade_pnl_before_costs: Decimal
    closed_trade_slippage_cost: Decimal
    closed_trade_entry_fees: Decimal
    closed_trade_exit_fees: Decimal
    funding_cost_lower_bound: Decimal | None
    funding_cost_upper_bound: Decimal | None
    cost_coverage: HistoricalCostCoverageV1
    benchmarks: tuple[HistoricalBenchmarkV1, ...]
    out_of_sample: bool
    walk_forward: bool
    strategy_config_sha256: str
    schema_version: str = _SCHEMA

    def validate(self) -> None:
        if self.schema_version != _SCHEMA:
            raise ValueError("historical strategy evidence schema mismatch")
        if not self.strategy_id.strip():
            raise ValueError("strategy_id is required")
        if not self.dataset_id.strip():
            raise ValueError("dataset_id is required")
        _digest(self.dataset_sha256, "dataset_sha256")
        _digest(self.strategy_config_sha256, "strategy_config_sha256")
        if not self.start.strip() or not self.end.strip() or self.start >= self.end:
            raise ValueError("historical evidence window is invalid")
        if not self.symbols or tuple(sorted(set(self.symbols))) != self.symbols:
            raise ValueError("symbols must be unique and canonically sorted")
        for name, value in (
            ("opening_cash", self.opening_cash),
            ("ending_equity", self.ending_equity),
            ("total_pnl", self.total_pnl),
            ("total_return", self.total_return),
            ("max_drawdown", self.max_drawdown),
            ("max_drawdown_fraction", self.max_drawdown_fraction),
            ("turnover_fraction", self.turnover_fraction),
            ("fees_paid", self.fees_paid),
            (
                "maximum_gross_exposure_fraction",
                self.maximum_gross_exposure_fraction,
            ),
            (
                "gross_closed_trade_pnl_before_costs",
                self.gross_closed_trade_pnl_before_costs,
            ),
            ("closed_trade_slippage_cost", self.closed_trade_slippage_cost),
            ("closed_trade_entry_fees", self.closed_trade_entry_fees),
            ("closed_trade_exit_fees", self.closed_trade_exit_fees),
        ):
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
        if self.opening_cash <= 0 or self.ending_equity <= 0:
            raise ValueError("historical evidence equity must remain positive")
        if self.closed_trade_count < 0:
            raise ValueError("closed_trade_count must be non-negative")
        if self.winning_trades < 0 or self.losing_trades < 0:
            raise ValueError("trade counts must be non-negative")
        if self.winning_trades + self.losing_trades > self.closed_trade_count:
            raise ValueError("winning/losing trades exceed closed trade count")
        if self.maximum_concurrent_positions < 0:
            raise ValueError("maximum_concurrent_positions must be non-negative")
        if self.funding_cost_lower_bound is not None:
            if not self.funding_cost_lower_bound.is_finite():
                raise ValueError("funding lower bound must be finite")
            if self.funding_cost_lower_bound < 0:
                raise ValueError("funding lower bound must be non-negative")
        if self.funding_cost_upper_bound is not None:
            if not self.funding_cost_upper_bound.is_finite():
                raise ValueError("funding upper bound must be finite")
            if self.funding_cost_upper_bound < 0:
                raise ValueError("funding upper bound must be non-negative")
        if (
            self.funding_cost_lower_bound is not None
            and self.funding_cost_upper_bound is not None
            and self.funding_cost_lower_bound > self.funding_cost_upper_bound
        ):
            raise ValueError("funding lower bound cannot exceed upper bound")
        for benchmark in self.benchmarks:
            benchmark.validate()
        ids = tuple(item.benchmark_id for item in self.benchmarks)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise ValueError("benchmarks must be unique and canonically sorted")

    @property
    def cost_adjusted_return_lower_bound(self) -> Decimal | None:
        if self.funding_cost_upper_bound is None:
            return None
        return (
            self.total_pnl - self.funding_cost_upper_bound
        ) / self.opening_cash

    @property
    def cost_adjusted_return_upper_bound(self) -> Decimal | None:
        if self.funding_cost_lower_bound is None:
            return None
        return (
            self.total_pnl - self.funding_cost_lower_bound
        ) / self.opening_cash

    @property
    def verdict(self) -> StrategyProfitabilityVerdict:
        if not self.cost_coverage.institutional_cost_complete:
            return StrategyProfitabilityVerdict.PROFITABILITY_NOT_PROVEN
        lower = self.cost_adjusted_return_lower_bound
        if lower is None or lower <= 0:
            return StrategyProfitabilityVerdict.PROFITABILITY_NOT_PROVEN
        if not self.out_of_sample or not self.walk_forward:
            return StrategyProfitabilityVerdict.RESEARCH_EDGE_CANDIDATE
        benchmark_returns = [item.total_return for item in self.benchmarks]
        if benchmark_returns and lower <= max(benchmark_returns):
            return StrategyProfitabilityVerdict.RESEARCH_EDGE_CANDIDATE
        return StrategyProfitabilityVerdict.OOS_EDGE_CONFIRMED

    def unsigned_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "strategy_config_sha256": self.strategy_config_sha256,
            "dataset_id": self.dataset_id,
            "dataset_sha256": self.dataset_sha256,
            "start": self.start,
            "end": self.end,
            "symbols": list(self.symbols),
            "opening_cash": str(self.opening_cash),
            "ending_equity": str(self.ending_equity),
            "total_pnl": str(self.total_pnl),
            "total_return": str(self.total_return),
            "max_drawdown": str(self.max_drawdown),
            "max_drawdown_fraction": str(self.max_drawdown_fraction),
            "turnover_fraction": str(self.turnover_fraction),
            "fees_paid": str(self.fees_paid),
            "closed_trade_count": self.closed_trade_count,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": None if self.win_rate is None else str(self.win_rate),
            "profit_factor": (
                None if self.profit_factor is None else str(self.profit_factor)
            ),
            "maximum_gross_exposure_fraction": str(
                self.maximum_gross_exposure_fraction
            ),
            "maximum_concurrent_positions": self.maximum_concurrent_positions,
            "gross_closed_trade_pnl_before_costs": str(
                self.gross_closed_trade_pnl_before_costs
            ),
            "closed_trade_slippage_cost": str(self.closed_trade_slippage_cost),
            "closed_trade_entry_fees": str(self.closed_trade_entry_fees),
            "closed_trade_exit_fees": str(self.closed_trade_exit_fees),
            "funding_cost_lower_bound": (
                None
                if self.funding_cost_lower_bound is None
                else str(self.funding_cost_lower_bound)
            ),
            "funding_cost_upper_bound": (
                None
                if self.funding_cost_upper_bound is None
                else str(self.funding_cost_upper_bound)
            ),
            "cost_adjusted_return_lower_bound": (
                None
                if self.cost_adjusted_return_lower_bound is None
                else str(self.cost_adjusted_return_lower_bound)
            ),
            "cost_adjusted_return_upper_bound": (
                None
                if self.cost_adjusted_return_upper_bound is None
                else str(self.cost_adjusted_return_upper_bound)
            ),
            "cost_coverage": self.cost_coverage.payload(),
            "benchmarks": [item.payload() for item in self.benchmarks],
            "out_of_sample": self.out_of_sample,
            "walk_forward": self.walk_forward,
            "verdict": self.verdict.value,
        }

    @property
    def evidence_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(self.unsigned_payload())
        ).hexdigest()

    def payload(self) -> dict[str, object]:
        return {
            **self.unsigned_payload(),
            "evidence_sha256": self.evidence_sha256,
        }


def build_historical_strategy_evidence(
    *,
    result: CrossSectionalPortfolioResult,
    strategy_id: str,
    strategy_config: dict[str, object],
    dataset_id: str,
    dataset_sha256: str,
    start: str,
    end: str,
    symbols: tuple[str, ...],
    cost_coverage: HistoricalCostCoverageV1,
    benchmarks: tuple[HistoricalBenchmarkV1, ...] = (),
    funding_cost_lower_bound: Decimal | None = None,
    funding_cost_upper_bound: Decimal | None = None,
    out_of_sample: bool = False,
    walk_forward: bool = False,
) -> HistoricalStrategyEvidenceV1:
    closed = result.closed_trades
    gross = sum(
        (trade.gross_pnl_before_costs for trade in closed),
        Decimal("0"),
    )
    slippage = sum(
        (trade.slippage_cost for trade in closed),
        Decimal("0"),
    )
    entry_fees = sum(
        (trade.entry_fee for trade in closed),
        Decimal("0"),
    )
    exit_fees = sum(
        (trade.exit_fee for trade in closed),
        Decimal("0"),
    )
    win_rate = (
        Decimal(result.winning_trades) / Decimal(result.closed_trade_count)
        if result.closed_trade_count
        else None
    )
    evidence = HistoricalStrategyEvidenceV1(
        strategy_id=strategy_id,
        strategy_config_sha256=hashlib.sha256(
            canonical_json_bytes(strategy_config)
        ).hexdigest(),
        dataset_id=dataset_id,
        dataset_sha256=dataset_sha256,
        start=start,
        end=end,
        symbols=tuple(sorted(symbols)),
        opening_cash=result.total_pnl * Decimal("0")
        + (result.total_pnl / result.total_return if result.total_return != 0 else Decimal("10000")),
        ending_equity=(
            result.total_pnl
            + (
                result.total_pnl / result.total_return
                if result.total_return != 0
                else Decimal("10000")
            )
        ),
        total_pnl=result.total_pnl,
        total_return=result.total_return,
        max_drawdown=result.max_drawdown,
        max_drawdown_fraction=result.max_drawdown_fraction,
        turnover_fraction=result.turnover_fraction,
        fees_paid=result.fees_paid,
        closed_trade_count=result.closed_trade_count,
        winning_trades=result.winning_trades,
        losing_trades=result.losing_trades,
        win_rate=win_rate,
        profit_factor=result.profit_factor,
        maximum_gross_exposure_fraction=(
            result.maximum_gross_exposure_fraction_observed
        ),
        maximum_concurrent_positions=result.maximum_concurrent_positions,
        gross_closed_trade_pnl_before_costs=gross,
        closed_trade_slippage_cost=slippage,
        closed_trade_entry_fees=entry_fees,
        closed_trade_exit_fees=exit_fees,
        funding_cost_lower_bound=funding_cost_lower_bound,
        funding_cost_upper_bound=funding_cost_upper_bound,
        cost_coverage=cost_coverage,
        benchmarks=tuple(sorted(benchmarks, key=lambda item: item.benchmark_id)),
        out_of_sample=out_of_sample,
        walk_forward=walk_forward,
    )
    evidence.validate()
    return evidence


def _digest(value: str, name: str) -> None:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
