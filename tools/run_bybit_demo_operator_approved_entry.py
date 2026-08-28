from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_account_reader import BybitDemoAccountingClient
from app.execution.bybit_demo_connected_preflight import (
    PostgresBybitDemoOperationalStateReader,
)
from app.execution.bybit_demo_cycle import BybitDemoCyclePolicy
from app.execution.bybit_demo_fixed_egress import (
    BybitDemoFixedEgressPreflightAccountClient,
    FixedEgressPostgresBybitDemoControlPlane,
    require_fixed_egress_ready_for_arm,
    run_bybit_demo_fixed_egress_connected_preflight,
)
from app.execution.bybit_demo_managed_trade_poll import BybitDemoManagedTradePollPolicy
from app.execution.bybit_demo_max_hold_close import BybitDemoMaxHoldClosePolicy
from app.execution.bybit_demo_operational_entry import (
    BybitDemoOperationalEntryEvidence,
    BybitDemoOperationalEntryStatus,
    PinnedBybitDemoControlPlane,
    run_protected_bybit_demo_operational_entry,
)
from app.execution.bybit_demo_operator_approval import create_bybit_demo_operator_approval
from app.execution.bybit_demo_postgres_approval_lineage_store import (
    PostgresBybitDemoApprovedEntryAuthorizationStore,
)
from app.execution.bybit_demo_postgres_bootstrap import verify_bybit_demo_postgres_schema
from app.execution.bybit_demo_postgres_entry_provenance_store import (
    PostgresBybitDemoEntryProvenanceStore,
)
from app.execution.bybit_demo_postgres_excursion_store import PostgresBybitDemoExcursionStore
from app.execution.bybit_demo_postgres_runtime_lease import PostgresBybitDemoRuntimeLease
from app.execution.bybit_demo_postgres_session_risk_store import (
    PostgresBybitDemoSessionRiskLedgerStore,
)
from app.execution.bybit_demo_postgres_terminal_evidence_store import (
    PostgresBybitDemoTerminalEvidenceStore,
)
from app.execution.bybit_demo_session_risk_flatten import BybitDemoSessionRiskFlattenPolicy
from app.execution.bybit_demo_session_risk_runtime import (
    PostgresBybitDemoSessionRiskCommitter,
)
from app.execution.bybit_demo_trade_management_runtime import (
    BybitDemoTradeManagementRuntimePolicy,
)
from app.execution.bybit_demo_trading_credential_preflight import (
    BybitDemoTradingCredentialReadOnlyInspector,
    run_bybit_demo_trading_credential_preflight,
)
from app.execution.bybit_oms_entry_client import OmsAwareBybitDemoStopRatchetClient
from app.execution.bybit_postgres_entry_recovery import PostgresBybitEntryRecoveryStore
from app.marketdata.bybit_demo_completed_bars import BybitDemoCompletedBarClient
from app.marketdata.bybit_demo_instruments import BybitDemoInstrumentClient
from app.marketdata.bybit_entry_reference import (
    BybitEntryReferenceQuoteClient,
    BybitEntryReferenceStore,
)
from app.marketdata.bybit_v5 import BybitPublicKlineClient
from app.observability.bybit_runtime_health import BybitRestHealthRecorder
from app.oms.bybit_entry import PostgresBybitEntryOms
from app.strategy.crypto_live_opportunity_reader import PostgresCryptoLiveOpportunityReader
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from tools.prepare_bybit_demo_operator_approval import (
    _site_host,
    resolve_bybit_demo_operator_approval_source,
)

_EXIT_OK = 0
_EXIT_BLOCKED = 2
_APPROVAL_TTL_SECONDS = 120


@dataclass(frozen=True)
class _OperationalInputs:
    evidence_rank: int
    symbol: str
    confirmation_phrase: str
    research_site: str


@dataclass(frozen=True)
class _OperationalEnvironment:
    demo_database_dsn: str
    opportunity_database_dsn: str
    trading_api_key: str
    trading_api_secret: str
    readonly_api_key: str
    readonly_api_secret: str
    mainnet_readonly_api_key_sha256: str


@dataclass(frozen=True)
class _OperationalDependencies:
    accounting_client: BybitDemoAccountingClient
    fixed_egress_preflight_client: BybitDemoFixedEgressPreflightAccountClient
    control_plane: FixedEgressPostgresBybitDemoControlPlane
    authorization_store: PostgresBybitDemoApprovedEntryAuthorizationStore
    provenance_store: PostgresBybitDemoEntryProvenanceStore
    excursion_store: PostgresBybitDemoExcursionStore
    runtime_lease: PostgresBybitDemoRuntimeLease
    terminal_evidence_store: PostgresBybitDemoTerminalEvidenceStore
    session_store: PostgresBybitDemoSessionRiskLedgerStore
    session_risk_committer: PostgresBybitDemoSessionRiskCommitter
    instrument_client: BybitDemoInstrumentClient
    completed_bar_client: BybitDemoCompletedBarClient
    quote_client: BybitEntryReferenceQuoteClient
    order_client: OmsAwareBybitDemoStopRatchetClient
    managed_policy: BybitDemoManagedTradePollPolicy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run exactly one operator-approved Bybit Demo operational entry invocation. "
            "This command never arms v121 and has no mainnet route."
        )
    )
    parser.add_argument("--evidence-rank", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument(
        "--site",
        default=(os.environ.get("BYBIT_RESEARCH_SITE") or "global"),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        inputs = _OperationalInputs(
            evidence_rank=_evidence_rank(args.evidence_rank),
            symbol=_normalized_symbol(args.symbol),
            confirmation_phrase=args.confirm,
            research_site=args.site,
        )
        environment = _environment_from_process()
        dependencies = _build_dependencies(environment)
        evidence = _run_once(inputs, environment, dependencies)
        payload = evidence.to_payload()
    except Exception as exc:  # noqa: BLE001 - output is intentionally allowlisted and sanitized.
        payload = _failure_payload(exc)
        _emit(payload, output=args.output)
        return _EXIT_BLOCKED

    _emit(payload, output=args.output)
    if evidence.status is BybitDemoOperationalEntryStatus.ENTRY_CYCLE_COMPLETE:
        return _EXIT_OK
    return _EXIT_BLOCKED


def _build_dependencies(environment: _OperationalEnvironment) -> _OperationalDependencies:
    schema = verify_bybit_demo_postgres_schema(environment.demo_database_dsn)
    if not schema.passed:
        raise RuntimeError("Bybit Demo PostgreSQL operational schema is not verified")

    credential_inspector = BybitDemoTradingCredentialReadOnlyInspector(
        api_key=environment.trading_api_key,
        api_secret=environment.trading_api_secret,
    )
    credential = run_bybit_demo_trading_credential_preflight(
        credential_inspector,
        demo_readonly_api_key_sha256=hashlib.sha256(
            environment.readonly_api_key.encode("utf-8")
        ).hexdigest(),
        mainnet_readonly_api_key_sha256=environment.mainnet_readonly_api_key_sha256,
    )
    if not credential.passed:
        raise RuntimeError("Bybit Demo trading credential preflight is blocked")

    session_store = PostgresBybitDemoSessionRiskLedgerStore(environment.demo_database_dsn)
    session_store.load_active()

    entry_reference_store = BybitEntryReferenceStore()
    rest_health = BybitRestHealthRecorder()
    entry_oms = PostgresBybitEntryOms(environment.demo_database_dsn)
    recovery_store = PostgresBybitEntryRecoveryStore(environment.demo_database_dsn)
    order_client = OmsAwareBybitDemoStopRatchetClient(
        entry_oms=entry_oms,
        entry_reference_store=entry_reference_store,
        entry_recovery_store=recovery_store,
        rest_health_sink=rest_health,
        api_key=environment.trading_api_key,
        api_secret=environment.trading_api_secret,
    )

    return _OperationalDependencies(
        accounting_client=BybitDemoAccountingClient(
            api_key=environment.readonly_api_key,
            api_secret=environment.readonly_api_secret,
        ),
        fixed_egress_preflight_client=BybitDemoFixedEgressPreflightAccountClient(
            api_key=environment.readonly_api_key,
            api_secret=environment.readonly_api_secret,
        ),
        control_plane=FixedEgressPostgresBybitDemoControlPlane(
            environment.demo_database_dsn
        ),
        authorization_store=PostgresBybitDemoApprovedEntryAuthorizationStore(
            environment.demo_database_dsn
        ),
        provenance_store=PostgresBybitDemoEntryProvenanceStore(
            environment.demo_database_dsn
        ),
        excursion_store=PostgresBybitDemoExcursionStore(environment.demo_database_dsn),
        runtime_lease=PostgresBybitDemoRuntimeLease(environment.demo_database_dsn),
        terminal_evidence_store=PostgresBybitDemoTerminalEvidenceStore(
            environment.demo_database_dsn
        ),
        session_store=session_store,
        session_risk_committer=PostgresBybitDemoSessionRiskCommitter(session_store),
        instrument_client=BybitDemoInstrumentClient(),
        completed_bar_client=BybitDemoCompletedBarClient(),
        quote_client=BybitEntryReferenceQuoteClient(
            reference_store=entry_reference_store,
        ),
        order_client=order_client,
        managed_policy=_managed_policy(),
    )


def _run_once(
    inputs: _OperationalInputs,
    environment: _OperationalEnvironment,
    dependencies: _OperationalDependencies,
) -> BybitDemoOperationalEntryEvidence:
    source = resolve_bybit_demo_operator_approval_source(
        PostgresCryptoLiveOpportunityReader(environment.opportunity_database_dsn),
        BybitPublicKlineClient(host=_site_host(inputs.research_site)),
        evidence_rank=inputs.evidence_rank,
        expected_symbol=inputs.symbol,
    )
    symbol = str(source.review_row["symbol"])
    if symbol != inputs.symbol:
        raise RuntimeError("operator-approved source symbol changed")

    instruments = dependencies.instrument_client.fetch_symbols((symbol,))
    if tuple(instruments) != (symbol,):
        raise RuntimeError("Bybit Demo exact instrument resolution failed")

    fixed_egress = run_bybit_demo_fixed_egress_connected_preflight(
        dependencies.fixed_egress_preflight_client,
        PostgresBybitDemoOperationalStateReader(environment.demo_database_dsn),
    )
    require_fixed_egress_ready_for_arm(fixed_egress)

    arm_observed_at = datetime.now(UTC)
    arm_decision = dependencies.control_plane.read_decision(now=arm_observed_at)
    pinned_arm = PinnedBybitDemoControlPlane(dependencies.control_plane, arm_decision)

    wallet = dependencies.accounting_client.get_wallet_balance()
    checkpoint = dependencies.session_store.load_active()
    session_state = checkpoint.ledger.to_session_risk_state(
        current_equity_usdt=wallet.total_equity_usd,
    )

    approved_at = datetime.now(UTC)
    approval = create_bybit_demo_operator_approval(
        source.review_row,
        source.bars,
        approved_at=approved_at,
        confirmation_phrase=inputs.confirmation_phrase,
        ttl_seconds=_APPROVAL_TTL_SECONDS,
    )
    if approval.source_evidence_rank != inputs.evidence_rank or approval.symbol != inputs.symbol:
        raise RuntimeError("operator approval identity changed during preparation")

    return run_protected_bybit_demo_operational_entry(
        approval,
        source.review_row,
        {symbol: source.bars},
        fixed_egress_preflight=fixed_egress,
        new_entry_control_plane=pinned_arm,
        now=approved_at,
        instruments=instruments,
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=session_state,
        client=dependencies.order_client,
        accounting_client=dependencies.accounting_client,
        excursion_store=dependencies.excursion_store,
        completed_bar_client=dependencies.completed_bar_client,
        quote_client=dependencies.quote_client,
        runtime_lease=dependencies.runtime_lease,
        approval_authorization_store=dependencies.authorization_store,
        session_risk_committer=dependencies.session_risk_committer,
        terminal_evidence_store=dependencies.terminal_evidence_store,
        entry_provenance_store=dependencies.provenance_store,
        managed_policy=dependencies.managed_policy,
        cycle_policy=BybitDemoCyclePolicy(
            writes_enabled=True,
            require_entry_recovery_envelope=True,
        ),
        session_ledger=checkpoint.ledger,
        now_ms=int(approved_at.timestamp() * 1000),
    )


def _managed_policy() -> BybitDemoManagedTradePollPolicy:
    return BybitDemoManagedTradePollPolicy(
        trade_management=BybitDemoTradeManagementRuntimePolicy(
            stop_ratchet_writes_enabled=True,
        ),
        max_hold_close=BybitDemoMaxHoldClosePolicy(writes_enabled=True),
        session_risk_flatten=BybitDemoSessionRiskFlattenPolicy(writes_enabled=True),
    )


def _environment_from_process() -> _OperationalEnvironment:
    return _OperationalEnvironment(
        demo_database_dsn=_required_env("BYBIT_DEMO_DATABASE_DSN"),
        opportunity_database_dsn=_required_env("BYBIT_OPPORTUNITY_DATABASE_DSN"),
        trading_api_key=_required_env("BYBIT_DEMO_TRADING_API_KEY"),
        trading_api_secret=_required_env("BYBIT_DEMO_TRADING_API_SECRET"),
        readonly_api_key=_required_env("BYBIT_DEMO_READONLY_API_KEY"),
        readonly_api_secret=_required_env("BYBIT_DEMO_READONLY_API_SECRET"),
        mainnet_readonly_api_key_sha256=_required_env(
            "BYBIT_MAINNET_READONLY_API_KEY_SHA256"
        ),
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing:{name}")
    return value


def _evidence_rank(value: str) -> int:
    if not value or value.strip() != value:
        raise ValueError("operator evidence rank must be an integer within [1, 50]")
    try:
        rank = int(value)
    except ValueError as exc:
        raise ValueError("operator evidence rank must be an integer within [1, 50]") from exc
    if not 1 <= rank <= 50:
        raise ValueError("operator evidence rank must be an integer within [1, 50]")
    return rank


def _normalized_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if symbol != value or not symbol.endswith("USDT") or not symbol.isalnum():
        raise ValueError("operator symbol must be normalized uppercase USDT")
    return symbol


def _failure_payload(exc: Exception) -> dict[str, Any]:
    return {
        "schema": "BYBIT_DEMO_OPERATIONAL_ENTRY_RUNNER_V1",
        "status": "STARTUP_BLOCKED",
        "blocked": True,
        "error_type": type(exc).__name__,
        "same_invocation_additional_entry_allowed": False,
        "automatic_arm_allowed": False,
        "ranked_fallback_allowed": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _artifact_git_sha() -> str | None:
    value = os.environ.get("GITHUB_SHA", "").strip()
    return value or None


def _emit(payload: dict[str, Any], *, output: Path) -> None:
    bound_payload = dict(payload)
    bound_payload["git_sha"] = _artifact_git_sha()
    text = json.dumps(
        bound_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, output)
    print(text, flush=True)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
