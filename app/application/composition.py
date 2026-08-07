from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.application.order_lifecycle import PaperOrderLifecycle
from app.application.paper_pipeline import PaperTradingPipeline
from app.observability.readiness import OperationalReadinessEvaluator, OperationalSloPolicy
from app.oms.postgres import PostgresOmsStore
from app.oms.protocols import OmsStore
from app.oms.reconciliation import OmsReconciler
from app.oms.store import DurableOmsStore
from app.portfolio.ledger import PortfolioLedger
from app.portfolio.postgres import PostgresPortfolioEventStore
from app.portfolio.store import PortfolioEventStore
from app.risk.evidence import RiskAdmissionService, RiskEvidenceJournal, SQLiteRiskEvidenceJournal
from app.risk.postgres import PostgresRiskEvidenceJournal
from app.risk.pretrade import PreTradeRiskEngine, RiskLimits
from app.strategy.momentum import LongOnlyMomentumStrategy


@dataclass(frozen=True)
class ProductConfig:
    opening_cash: Decimal
    target_quantity: Decimal
    risk_limits: RiskLimits
    operational_slo: OperationalSloPolicy = OperationalSloPolicy()

    def validate(self) -> None:
        if not self.opening_cash.is_finite() or self.opening_cash <= 0:
            raise ValueError("opening_cash must be positive and finite")
        if not self.target_quantity.is_finite() or self.target_quantity < 0:
            raise ValueError("target_quantity must be finite and non-negative")
        self.risk_limits.validate()
        self.operational_slo.validate()


@dataclass(frozen=True)
class ProductRuntime:
    """Stable product graph; legacy schema modules sit outside this composition root."""

    config: ProductConfig
    strategy: LongOnlyMomentumStrategy
    risk_engine: PreTradeRiskEngine
    risk_admission: RiskAdmissionService
    portfolio: PortfolioLedger
    portfolio_store: object
    oms_store: OmsStore
    order_lifecycle: PaperOrderLifecycle
    reconciler: OmsReconciler
    paper_pipeline: PaperTradingPipeline
    operational_readiness: OperationalReadinessEvaluator



def _compose(
    *,
    config: ProductConfig,
    oms_store: OmsStore,
    risk_journal: RiskEvidenceJournal,
    portfolio_store: object,
) -> ProductRuntime:
    config.validate()
    strategy = LongOnlyMomentumStrategy(target_quantity=config.target_quantity)
    risk_engine = PreTradeRiskEngine(config.risk_limits)
    risk_admission = RiskAdmissionService(engine=risk_engine, journal=risk_journal)
    portfolio = PortfolioLedger(opening_cash=config.opening_cash)
    lifecycle = PaperOrderLifecycle(oms_store)
    reconciler = OmsReconciler(oms_store)
    pipeline = PaperTradingPipeline(
        strategy=strategy,
        ledger=portfolio,
        risk=risk_engine,
        risk_admission=risk_admission,
    )
    readiness = OperationalReadinessEvaluator(config.operational_slo)
    return ProductRuntime(
        config=config,
        strategy=strategy,
        risk_engine=risk_engine,
        risk_admission=risk_admission,
        portfolio=portfolio,
        portfolio_store=portfolio_store,
        oms_store=oms_store,
        order_lifecycle=lifecycle,
        reconciler=reconciler,
        paper_pipeline=pipeline,
        operational_readiness=readiness,
    )


def build_local_product(*, config: ProductConfig, state_directory: str | Path) -> ProductRuntime:
    """Build a deterministic local product graph with durable SQLite state."""

    directory = Path(state_directory)
    directory.mkdir(parents=True, exist_ok=True)
    return _compose(
        config=config,
        oms_store=DurableOmsStore(directory / "oms.sqlite"),
        risk_journal=SQLiteRiskEvidenceJournal(directory / "risk.sqlite"),
        portfolio_store=PortfolioEventStore(directory / "portfolio.sqlite"),
    )


def build_postgres_product(
    *,
    config: ProductConfig,
    dsn: str,
    migrate: bool = False,
) -> ProductRuntime:
    """Build the production-style product graph on a shared PostgreSQL database."""

    oms_store = PostgresOmsStore(dsn)
    risk_journal = PostgresRiskEvidenceJournal(dsn)
    portfolio_store = PostgresPortfolioEventStore(dsn)
    if migrate:
        oms_store.migrate()
        risk_journal.migrate()
        portfolio_store.migrate()
    return _compose(
        config=config,
        oms_store=oms_store,
        risk_journal=risk_journal,
        portfolio_store=portfolio_store,
    )
