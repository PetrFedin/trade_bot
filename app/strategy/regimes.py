from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.marketdata.historical import HistoricalDataset
from app.strategy.qualification import StrategyQualification, WalkForwardQualifier


@dataclass(frozen=True)
class HistoricalRegime:
    name: str
    start: datetime
    end: datetime

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("regime name is required")
        for field, value in (("start", self.start), ("end", self.end)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"regime {field} must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("regime start must precede end")


@dataclass(frozen=True)
class RegimeQualificationResult:
    regime: HistoricalRegime
    bars: int
    qualification: StrategyQualification


@dataclass(frozen=True)
class MultiRegimeQualification:
    qualified: bool
    dataset_id: str
    dataset_sha256: str
    results: tuple[RegimeQualificationResult, ...]
    reasons: tuple[str, ...]


class MultiRegimeQualifier:
    """Require a fixed strategy implementation to qualify across named OOS regimes."""

    def __init__(
        self,
        *,
        qualifier: WalkForwardQualifier,
        minimum_regimes: int = 3,
    ) -> None:
        if minimum_regimes < 2:
            raise ValueError("minimum_regimes must be at least two")
        self.qualifier = qualifier
        self.minimum_regimes = minimum_regimes

    def qualify(
        self,
        dataset: HistoricalDataset,
        regimes: tuple[HistoricalRegime, ...],
    ) -> MultiRegimeQualification:
        reasons: set[str] = set()
        if len(regimes) < self.minimum_regimes:
            reasons.add("INSUFFICIENT_REGIMES")
        self._validate_regimes(regimes)

        results: list[RegimeQualificationResult] = []
        for regime in regimes:
            bars = tuple(
                bar for bar in dataset.bars if regime.start <= bar.timestamp < regime.end
            )
            qualification = self.qualifier.qualify(bars)
            results.append(
                RegimeQualificationResult(
                    regime=regime,
                    bars=len(bars),
                    qualification=qualification,
                )
            )
            if not qualification.qualified:
                reasons.add(f"REGIME_NOT_QUALIFIED:{regime.name}")

        return MultiRegimeQualification(
            qualified=not reasons,
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset.canonical_sha256,
            results=tuple(results),
            reasons=tuple(sorted(reasons)),
        )

    @staticmethod
    def _validate_regimes(regimes: tuple[HistoricalRegime, ...]) -> None:
        ordered = sorted(regimes, key=lambda value: value.start)
        if tuple(ordered) != regimes:
            raise ValueError("regimes must be ordered by start time")
        names: set[str] = set()
        previous: HistoricalRegime | None = None
        for regime in regimes:
            regime.validate()
            if regime.name in names:
                raise ValueError("regime names must be unique")
            names.add(regime.name)
            if previous is not None and regime.start < previous.end:
                raise ValueError("regimes must not overlap")
            previous = regime
