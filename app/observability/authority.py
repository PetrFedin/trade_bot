from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.execution.execution_checkpoints import ExecutionCheckpointStore
from app.execution.execution_facts import ExecutionFactStore
from app.marketdata.continuity import OperationalContinuityStore
from app.marketdata.operational import OperationalMarketDataStore
from app.observability.readiness import OperationalSnapshot
from app.oms.protocols import OmsStore
from app.oms.portfolio_reconciliation import PortfolioReconciliationStore


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _age_seconds(now: datetime, observed_at: datetime) -> tuple[Decimal, bool]:
    current = _aware_utc(now, "now")
    observed = _aware_utc(observed_at, "observed_at")
    if observed > current:
        return Decimal("0"), True
    return Decimal(str((current - observed).total_seconds())), False


@dataclass(frozen=True)
class OperationalMarketScope:
    provider: str
    venue: str
    symbol: str
    interval_seconds: int

    def validate(self) -> None:
        for name, value in (
            ("provider", self.provider),
            ("venue", self.venue),
            ("symbol", self.symbol),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if self.interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")


@dataclass(frozen=True)
class RuntimeTelemetry:
    """Read-only ephemeral broker/stream facts sampled at dispatch time."""

    stream_ready: bool
    stream_last_message_at: datetime | None
    broker_connected: bool
    broker_latency_ms: Decimal
    broker_error_fraction: Decimal

    def validate(self) -> None:
        if not isinstance(self.stream_ready, bool):
            raise ValueError("stream_ready must be boolean")
        if not isinstance(self.broker_connected, bool):
            raise ValueError("broker_connected must be boolean")
        if self.stream_last_message_at is not None:
            _aware_utc(self.stream_last_message_at, "stream_last_message_at")
        if not self.broker_latency_ms.is_finite() or self.broker_latency_ms < 0:
            raise ValueError("broker_latency_ms must be finite and non-negative")
        if (
            not self.broker_error_fraction.is_finite()
            or self.broker_error_fraction < 0
            or self.broker_error_fraction > 1
        ):
            raise ValueError("broker_error_fraction must be within [0, 1]")


@dataclass(frozen=True)
class SessionRiskTruth:
    """Current session-level risk facts supplied by the durable risk authority."""

    daily_pnl: Decimal
    drawdown: Decimal
    kill_switch_engaged: bool

    def validate(self) -> None:
        if not self.daily_pnl.is_finite():
            raise ValueError("daily_pnl must be finite")
        if not self.drawdown.is_finite() or self.drawdown < 0:
            raise ValueError("drawdown must be finite and non-negative")
        if not isinstance(self.kill_switch_engaged, bool):
            raise ValueError("kill_switch_engaged must be boolean")


RuntimeTelemetryProvider = Callable[[], RuntimeTelemetry]
SessionRiskTruthProvider = Callable[[], SessionRiskTruth]
Clock = Callable[[], datetime]


class AuthoritativeOperationalSnapshotAssembler:
    """Build the dispatch snapshot from canonical read-only authorities.

    The assembler has no broker mutation, ARM/HALT, strategy or recovery capability.
    Missing, malformed or unreadable authority is represented as an explicit blocker
    and a fail-closed snapshot rather than optimistic default truth.
    """

    def __init__(
        self,
        *,
        market_scope: OperationalMarketScope,
        marketdata: OperationalMarketDataStore,
        continuity: OperationalContinuityStore,
        oms: OmsStore,
        reconciliation: PortfolioReconciliationStore,
        execution_facts: ExecutionFactStore,
        execution_checkpoints: ExecutionCheckpointStore,
        runtime_telemetry: RuntimeTelemetryProvider | None,
        session_risk: SessionRiskTruthProvider | None,
        clock: Clock = _utc_now,
    ) -> None:
        market_scope.validate()
        self.market_scope = market_scope
        self.marketdata = marketdata
        self.continuity = continuity
        self.oms = oms
        self.reconciliation = reconciliation
        self.execution_facts = execution_facts
        self.execution_checkpoints = execution_checkpoints
        self.runtime_telemetry = runtime_telemetry
        self.session_risk = session_risk
        self.clock = clock

    def __call__(self) -> OperationalSnapshot:
        return self.assemble(now=self.clock())

    def assemble(self, *, now: datetime) -> OperationalSnapshot:
        current = _aware_utc(now, "now")
        reasons: set[str] = set()

        (
            market_data_age_seconds,
            market_data_ready,
            market_data_conflicts,
        ) = self._market_state(current, reasons)

        uncertain_orders = self._count_or_block(
            self.oms.operational_blocking_count,
            "OMS_AUTHORITY_UNAVAILABLE",
            reasons,
        )
        unresolved_execution_facts = self._count_or_block(
            self.execution_facts.unresolved_count,
            "EXECUTION_FACT_AUTHORITY_UNAVAILABLE",
            reasons,
        )
        unresolved_execution_checkpoints = self._count_or_block(
            self.execution_checkpoints.unresolved_count,
            "EXECUTION_CHECKPOINT_AUTHORITY_UNAVAILABLE",
            reasons,
        )

        (
            reconciliation_age_seconds,
            cash_mismatch,
            position_mismatches,
            portfolio_reconciled,
        ) = self._reconciliation_state(current, reasons)

        (
            stream_silence_seconds,
            stream_ready,
            broker_connected,
            broker_latency_ms,
            broker_error_fraction,
        ) = self._runtime_state(current, reasons)

        daily_pnl, drawdown, kill_switch_engaged = self._session_state(reasons)

        snapshot = OperationalSnapshot(
            market_data_age_seconds=market_data_age_seconds,
            stream_silence_seconds=stream_silence_seconds,
            broker_latency_ms=broker_latency_ms,
            broker_error_fraction=broker_error_fraction,
            uncertain_orders=uncertain_orders,
            reconciliation_age_seconds=reconciliation_age_seconds,
            cash_mismatch=cash_mismatch,
            position_mismatches=position_mismatches,
            daily_pnl=daily_pnl,
            drawdown=drawdown,
            kill_switch_engaged=kill_switch_engaged,
            market_data_ready=market_data_ready,
            stream_ready=stream_ready,
            broker_connected=broker_connected,
            portfolio_reconciled=portfolio_reconciled,
            market_data_conflicts=market_data_conflicts,
            unresolved_execution_facts=unresolved_execution_facts,
            unresolved_execution_checkpoints=unresolved_execution_checkpoints,
            authority_reasons=tuple(sorted(reasons)),
        )
        snapshot.validate()
        return snapshot

    @staticmethod
    def _count_or_block(
        loader: Callable[[], int],
        reason: str,
        reasons: set[str],
    ) -> int:
        try:
            count = loader()
            if not isinstance(count, int) or count < 0:
                raise ValueError("authority count must be non-negative integer")
            return count
        except Exception:
            reasons.add(reason)
            return 1

    def _market_state(
        self,
        now: datetime,
        reasons: set[str],
    ) -> tuple[Decimal, bool, int]:
        scope = self.market_scope
        try:
            conflicts = self.marketdata.conflict_count()
            if not isinstance(conflicts, int) or conflicts < 0:
                raise ValueError("market conflict count is invalid")
            checkpoint = self.continuity.latest(
                provider=scope.provider,
                venue=scope.venue,
                symbol=scope.symbol,
                interval_seconds=scope.interval_seconds,
            )
            if checkpoint is None:
                reasons.add("MARKET_DATA_CONTINUITY_MISSING")
                return Decimal("0"), False, conflicts
            checkpoint.validate()
            age, future = _age_seconds(now, checkpoint.through_close_time)
            if future:
                reasons.add("MARKET_DATA_CLOCK_CONFLICT")
            bars = self.marketdata.recent_bars(
                provider=scope.provider,
                venue=scope.venue,
                symbol=scope.symbol,
                interval_seconds=scope.interval_seconds,
                through_close_time=checkpoint.through_close_time,
                limit=1,
            )
            if len(bars) != 1:
                reasons.add("MARKET_DATA_THROUGH_BAR_MISSING")
                return age, False, conflicts
            through = bars[0]
            through.validate()
            if (
                through.bar_id != checkpoint.through_bar_id
                or through.close_time != checkpoint.through_close_time
                or not through.is_final
            ):
                reasons.add("MARKET_DATA_CONTINUITY_MISMATCH")
                return age, False, conflicts
            if conflicts:
                reasons.add("MARKET_DATA_CONFLICT_PRESENT")
            return age, not future and conflicts == 0, conflicts
        except Exception:
            reasons.add("MARKET_DATA_AUTHORITY_UNAVAILABLE")
            return Decimal("0"), False, 1

    def _reconciliation_state(
        self,
        now: datetime,
        reasons: set[str],
    ) -> tuple[Decimal, Decimal, int, bool]:
        try:
            latest = self.reconciliation.latest()
            if latest is None:
                reasons.add("PORTFOLIO_RECONCILIATION_MISSING")
                return Decimal("0"), Decimal("0"), 0, False
            latest.validate()
            age, future = _age_seconds(now, latest.occurred_at)
            if future:
                reasons.add("PORTFOLIO_RECONCILIATION_CLOCK_CONFLICT")
            return (
                age,
                abs(latest.cash_delta),
                len(latest.position_deltas),
                latest.matched and not future,
            )
        except Exception:
            reasons.add("PORTFOLIO_RECONCILIATION_AUTHORITY_UNAVAILABLE")
            return Decimal("0"), Decimal("0"), 0, False

    def _runtime_state(
        self,
        now: datetime,
        reasons: set[str],
    ) -> tuple[Decimal, bool, bool, Decimal, Decimal]:
        if self.runtime_telemetry is None:
            reasons.add("RUNTIME_TELEMETRY_MISSING")
            return Decimal("0"), False, False, Decimal("0"), Decimal("0")
        try:
            telemetry = self.runtime_telemetry()
            telemetry.validate()
            stream_ready = telemetry.stream_ready
            if telemetry.stream_last_message_at is None:
                reasons.add("STREAM_LAST_MESSAGE_MISSING")
                silence = Decimal("0")
                stream_ready = False
            else:
                silence, future = _age_seconds(now, telemetry.stream_last_message_at)
                if future:
                    reasons.add("STREAM_CLOCK_CONFLICT")
                    stream_ready = False
            return (
                silence,
                stream_ready,
                telemetry.broker_connected,
                telemetry.broker_latency_ms,
                telemetry.broker_error_fraction,
            )
        except Exception:
            reasons.add("RUNTIME_TELEMETRY_INVALID")
            return Decimal("0"), False, False, Decimal("0"), Decimal("0")

    def _session_state(
        self,
        reasons: set[str],
    ) -> tuple[Decimal, Decimal, bool]:
        if self.session_risk is None:
            reasons.add("SESSION_RISK_AUTHORITY_MISSING")
            return Decimal("0"), Decimal("0"), True
        try:
            truth = self.session_risk()
            truth.validate()
            return truth.daily_pnl, truth.drawdown, truth.kill_switch_engaged
        except Exception:
            reasons.add("SESSION_RISK_AUTHORITY_INVALID")
            return Decimal("0"), Decimal("0"), True
