from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.runtime.alpaca_external_probe_v101 import UrllibTransport
from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperAdapterV100,
    AlpacaPaperCredentialsV100,
    AlpacaPaperEndpointsV100,
)
from app.runtime.paper_broker_contract_v99 import OrderSide, PaperBrokerV99
from app.runtime.paper_broker_roundtrip_v99 import (
    AdmissionEvidenceV99,
    FileRoundTripJournalV99,
    PaperBrokerRoundTripServiceV99,
    RoundTripOutcome,
    RoundTripPlanV99,
    RoundTripPolicyV99,
)
from app.runtime.platform_common_v90 import sha256_digest

CONFIRMATION_PHRASE = "RUN_ALPACA_PAPER_MUTATION_DRILL"
MUTATION_ENABLE_VALUE = "ENABLED"
INSTRUMENT = "AAPL"
QUANTITY = Decimal("1")
MAXIMUM_NOTIONAL = Decimal("25")


class MutationDrillError(RuntimeError):
    pass


@dataclass(frozen=True)
class MutationDrillInputs:
    confirmation: str
    initial_limit_price: Decimal
    replacement_limit_price: Decimal
    github_actor: str
    github_run_id: str
    github_run_attempt: int
    generation: int

    def validate(self) -> None:
        if self.confirmation != CONFIRMATION_PHRASE:
            raise MutationDrillError("OPERATOR_CONFIRMATION_MISMATCH")
        if not self.github_actor.strip() or not self.github_run_id.strip():
            raise MutationDrillError("GITHUB_EXECUTION_IDENTITY_MISSING")
        if self.github_run_attempt <= 0 or self.generation <= 0:
            raise MutationDrillError("GITHUB_EXECUTION_GENERATION_INVALID")
        for field, value in (
            ("initial_limit_price", self.initial_limit_price),
            ("replacement_limit_price", self.replacement_limit_price),
        ):
            if not value.is_finite() or value <= 0:
                raise MutationDrillError(f"{field.upper()}_INVALID")
            if value.as_tuple().exponent < -2:
                raise MutationDrillError(f"{field.upper()}_PRECISION_EXCEEDED")
        if self.replacement_limit_price >= self.initial_limit_price:
            raise MutationDrillError("REPLACEMENT_MUST_REDUCE_BUY_MARKETABILITY")
        if QUANTITY * self.initial_limit_price > MAXIMUM_NOTIONAL:
            raise MutationDrillError("INITIAL_NOTIONAL_LIMIT_EXCEEDED")
        if QUANTITY * self.replacement_limit_price > MAXIMUM_NOTIONAL:
            raise MutationDrillError("REPLACEMENT_NOTIONAL_LIMIT_EXCEEDED")


def load_readonly_evidence(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MutationDrillError("READONLY_EVIDENCE_NOT_OBJECT")
    if data.get("provider") != "alpaca":
        raise MutationDrillError("READONLY_PROVIDER_MISMATCH")
    if data.get("environment") != "paper":
        raise MutationDrillError("READONLY_ENVIRONMENT_MISMATCH")
    if data.get("paper_order_writes_enabled") is not False:
        raise MutationDrillError("READONLY_WRITES_MUST_BE_DISABLED")
    if data.get("external_order_routing_allowed") is not False:
        raise MutationDrillError("READONLY_EXTERNAL_ROUTING_MUST_BE_DISABLED")
    if data.get("live_trading_allowed") is not False:
        raise MutationDrillError("READONLY_LIVE_TRADING_MUST_BE_DISABLED")
    reasons = data.get("reasons")
    if reasons is None:
        reasons = []
    if not isinstance(reasons, list):
        raise MutationDrillError("READONLY_EVIDENCE_REASONS_INVALID")
    if str(data.get("account_status", "")).upper() != "ACTIVE":
        raise MutationDrillError("READONLY_ACCOUNT_NOT_ACTIVE")
    if data.get("trading_blocked") is True:
        raise MutationDrillError("READONLY_ACCOUNT_TRADING_BLOCKED")
    if data.get("stream_authenticated") is not True:
        raise MutationDrillError("READONLY_STREAM_NOT_AUTHENTICATED")
    if data.get("stream_listening") is not True:
        raise MutationDrillError("READONLY_STREAM_NOT_LISTENING")
    if reasons:
        raise MutationDrillError("READONLY_PROBE_HAS_REASONS")
    return data


def inputs_from_environment(
    *,
    confirmation: str,
    initial_limit_price: Decimal,
    replacement_limit_price: Decimal,
) -> MutationDrillInputs:
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise MutationDrillError("MANUAL_WORKFLOW_DISPATCH_REQUIRED")
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise MutationDrillError("MAIN_BRANCH_REQUIRED")
    if os.environ.get("ASTRA_ALPACA_PAPER_MUTATION_ENABLED") != MUTATION_ENABLE_VALUE:
        raise MutationDrillError("PAPER_MUTATION_KILL_SWITCH_DISABLED")
    return MutationDrillInputs(
        confirmation=confirmation,
        initial_limit_price=initial_limit_price,
        replacement_limit_price=replacement_limit_price,
        github_actor=os.environ.get("GITHUB_ACTOR", ""),
        github_run_id=os.environ.get("GITHUB_RUN_ID", ""),
        github_run_attempt=int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")),
        generation=int(os.environ.get("GITHUB_RUN_NUMBER", "0")),
    )


def execute_drill(
    *,
    broker: PaperBrokerV99,
    inputs: MutationDrillInputs,
    readonly_evidence: dict[str, Any],
    output_directory: Path,
    now: datetime | None = None,
) -> dict[str, object]:
    inputs.validate()
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise MutationDrillError("CURRENT_TIME_MUST_BE_AWARE")

    account = broker.get_account()
    account.validate()
    if account.status.upper() != "ACTIVE":
        raise MutationDrillError("PAPER_ACCOUNT_NOT_ACTIVE")
    if account.currency.upper() != "USD":
        raise MutationDrillError("PAPER_ACCOUNT_CURRENCY_MISMATCH")
    if account.trading_blocked:
        raise MutationDrillError("PAPER_ACCOUNT_TRADING_BLOCKED")
    if account.buying_power < MAXIMUM_NOTIONAL:
        raise MutationDrillError("PAPER_ACCOUNT_BUYING_POWER_TOO_LOW")

    session_id = f"github-run-{inputs.github_run_id}"
    round_trip_id = f"alpaca-paper-drill-{inputs.github_run_id}-{inputs.github_run_attempt}"
    client_order_id = f"astra-drill-{inputs.github_run_id}-{inputs.github_run_attempt}"
    evidence_digest = sha256_digest(
        {
            "readonly": readonly_evidence,
            "github_run_id": inputs.github_run_id,
            "github_run_attempt": inputs.github_run_attempt,
            "generation": inputs.generation,
        }
    )
    decision_digest = sha256_digest(
        {
            "confirmation": CONFIRMATION_PHRASE,
            "instrument": INSTRUMENT,
            "side": OrderSide.BUY.value,
            "quantity": str(QUANTITY),
            "initial_limit_price": str(inputs.initial_limit_price),
            "replacement_limit_price": str(inputs.replacement_limit_price),
            "actor": inputs.github_actor,
            "run_id": inputs.github_run_id,
            "run_attempt": inputs.github_run_attempt,
        }
    )
    plan = RoundTripPlanV99(
        round_trip_id=round_trip_id,
        session_id=session_id,
        account_id=account.account_id,
        generation=inputs.generation,
        client_order_id=client_order_id,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=QUANTITY,
        limit_price=inputs.initial_limit_price,
        replacement_limit_price=inputs.replacement_limit_price,
        created_at=current,
        expires_at=current + timedelta(minutes=4),
        operator_approval_id=(
            f"github:{inputs.github_actor}:{inputs.github_run_id}:{inputs.github_run_attempt}"
        ),
        approval_expires_at=current + timedelta(minutes=5),
        decision_digest=decision_digest,
        external_order_routing_allowed=False,
        live_trading_allowed=False,
    ).sealed()
    evidence = AdmissionEvidenceV99(
        session_id=session_id,
        generation=inputs.generation,
        captured_at=current,
        session_running=True,
        paper_order_submission_allowed=True,
        platform_ready=True,
        broker_reliability_ready=True,
        qualification_ready=True,
        kill_switch_engaged=False,
        digest=evidence_digest,
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    journal_path = output_directory / "roundtrip-journal.jsonl"
    journal = FileRoundTripJournalV99(journal_path)
    service = PaperBrokerRoundTripServiceV99(
        broker=broker,
        plan=plan,
        evidence=evidence,
        journal=journal,
        policy=RoundTripPolicyV99(
            maximum_plan_age=timedelta(minutes=5),
            maximum_evidence_age=timedelta(seconds=45),
            maximum_quantity=QUANTITY,
            maximum_notional=MAXIMUM_NOTIONAL,
            required_account_status="ACTIVE",
            required_currency="USD",
            require_replace=True,
            allowed_instruments=frozenset({INSTRUMENT}),
        ),
    )
    result = service.execute(now=current, expected_generation=inputs.generation)
    journal_events = journal.load()
    journal_states = [event.state.value for event in journal_events]
    account_fingerprint = sha256_digest({"account_id": account.account_id})[:16]
    return {
        "qualification": "PASS" if result.success else "FAIL",
        "provider": "alpaca",
        "environment": "paper",
        "instrument": INSTRUMENT,
        "side": OrderSide.BUY.value,
        "quantity": str(QUANTITY),
        "initial_limit_price": str(inputs.initial_limit_price),
        "replacement_limit_price": str(inputs.replacement_limit_price),
        "maximum_notional": str(MAXIMUM_NOTIONAL),
        "round_trip_id": result.round_trip_id,
        "client_order_id": client_order_id,
        "account_fingerprint": account_fingerprint,
        "generation": inputs.generation,
        "state": result.state.value,
        "outcome": result.outcome.value,
        "reasons": list(result.reasons),
        "filled_quantity": str(result.filled_quantity),
        "paper_broker_mutation_verified": result.paper_broker_mutation_verified,
        "journal_tail_digest": result.tail_digest,
        "plan_digest": plan.digest,
        "admission_evidence_digest": evidence.digest,
        "readonly_evidence_digest": sha256_digest(readonly_evidence),
        "credentials_configured": True,
        "probe_executed": True,
        "mutation_kill_switch_enabled": True,
        "operator_confirmation_valid": True,
        "mutation_executed": True,
        "journal_event_count": len(journal_events),
        "journal_states": journal_states,
        "paper_order_writes_enabled_for_drill": True,
        "external_order_routing_allowed": result.external_order_routing_allowed,
        "live_trading_allowed": result.live_trading_allowed,
        "residual_paper_exposure": (
            result.outcome is RoundTripOutcome.RESIDUAL_PAPER_EXPOSURE
            or result.filled_quantity > 0
        ),
        "github_actor": inputs.github_actor,
        "github_run_id": inputs.github_run_id,
        "github_run_attempt": inputs.github_run_attempt,
    }


def build_real_broker() -> AlpacaPaperAdapterV100:
    credentials = AlpacaPaperCredentialsV100.from_environment()
    transport = UrllibTransport(frozenset({"paper-api.alpaca.markets"}))
    return AlpacaPaperAdapterV100(
        credentials=credentials,
        transport=transport,
        endpoints=AlpacaPaperEndpointsV100(),
        paper_order_writes_enabled=True,
    )


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def failure_report(*, error_type: str) -> dict[str, object]:
    return {
        "qualification": "FAIL",
        "provider": "alpaca",
        "environment": "paper",
        "error_type": error_type,
        "mutation_executed": False,
        "paper_order_writes_enabled_for_drill": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one manual bounded Alpaca Paper submit/replace/cancel drill"
    )
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--initial-limit-price", type=Decimal, required=True)
    parser.add_argument("--replacement-limit-price", type=Decimal, required=True)
    parser.add_argument("--readonly-evidence", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary_path = args.output_directory / "mutation-drill-summary.json"
    try:
        inputs = inputs_from_environment(
            confirmation=args.confirmation,
            initial_limit_price=args.initial_limit_price,
            replacement_limit_price=args.replacement_limit_price,
        )
        inputs.validate()
        readonly = load_readonly_evidence(args.readonly_evidence)
        report = execute_drill(
            broker=build_real_broker(),
            inputs=inputs,
            readonly_evidence=readonly,
            output_directory=args.output_directory,
        )
    except Exception as exc:
        report = failure_report(error_type=type(exc).__name__)
        write_json(summary_path, report)
        print(json.dumps(report, sort_keys=True))
        return 1
    write_json(summary_path, report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["qualification"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
