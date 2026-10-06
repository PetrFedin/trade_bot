from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_instruments import parse_response
from app.marketdata.bybit_public import BybitPublicLinearSubscription
from app.marketdata.bybit_repair import (
    BybitContinuityRepairService,
    BybitPublicKlineClient,
    HttpResponse,
)
from app.marketdata.continuity import (
    SQLiteOperationalContinuityStore,
    SQLiteOperationalRepairBarStore,
)
from app.marketdata.operational import SQLiteOperationalMarketDataStore
from app.qualification.adapter_conformance import ConformanceStatus
from app.qualification.bybit_adapter_conformance import (
    qualify_bybit_public_marketdata_adapter,
)
from app.qualification.bybit_provider_replay import (
    build_bybit_provider_complete_replay,
)
from app.qualification.marketdata_integrity import (
    MarketDataIntegrityPolicy,
    MarketDataIntegrityStatus,
    MarketDataSafetyAction,
    evaluate_bybit_marketdata_integrity,
)
from app.qualification.qualification_job import (
    QualificationJobRequest,
    QualificationJobStatus,
    evaluate_public_marketdata_qualification_job,
)
from app.qualification.profile_binding import bind_manifest_to_profile
from app.qualification.profile_registry import QualificationProfile
from app.qualification.qualification_manifest import (
    build_qualification_manifest,
)
from app.qualification.replay_evidence import (
    ReplayContinuity,
    ReplayEvidenceProfile,
)

BASE = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
OBSERVED = BASE + timedelta(minutes=15, seconds=2)
STRATEGY = "bybit-demo-momentum-v1"


def subscription() -> BybitPublicLinearSubscription:
    return BybitPublicLinearSubscription(
        symbol="BTCUSDT",
        interval="5",
        strategy_id=STRATEGY,
    )


def row(index: int, *, close: str | None = None) -> list[str]:
    start = BASE + timedelta(minutes=5 * index)
    price = Decimal(close if close is not None else str(100 + index))
    return [
        str(int(start.timestamp() * 1000)),
        str(price),
        str(price + Decimal("1")),
        str(price - Decimal("1")),
        str(price),
        "10",
        "1000",
    ]


def response_body(rows: list[list[str]]) -> bytes:
    return json.dumps(
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "symbol": "BTCUSDT",
                "category": "linear",
                "list": list(reversed(rows)),
            },
            "retExtInfo": {},
            "time": int(OBSERVED.timestamp() * 1000),
        },
        separators=(",", ":"),
    ).encode("utf-8")


INSTRUMENT = {
    "symbol": "BTCUSDT",
    "status": "Trading",
    "baseCoin": "BTC",
    "quoteCoin": "USDT",
    "settleCoin": "USDT",
    "contractType": "LinearPerpetual",
    "priceFilter": {
        "minPrice": "0.10",
        "maxPrice": "1999999.80",
        "tickSize": "0.10",
    },
    "lotSizeFilter": {
        "maxOrderQty": "1500.000",
        "minOrderQty": "0.001",
        "qtyStep": "0.001",
        "maxMktOrderQty": "150.000",
        "minNotionalValue": "5",
    },
    "leverageFilter": {
        "minLeverage": "1",
        "maxLeverage": "150.00",
        "leverageStep": "0.01",
    },
}


class FakeTransport:
    def __init__(self, body: bytes, *, received_at: datetime = OBSERVED) -> None:
        self.body = body
        self.received_at = received_at
        self.urls: list[str] = []

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> HttpResponse:
        assert timeout_seconds > 0
        assert len(self.body) <= maximum_response_bytes
        self.urls.append(url)
        return HttpResponse(
            status=200,
            body=self.body,
            received_at=self.received_at,
        )


def instrument_spec():
    envelope = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"list": [INSTRUMENT]},
        "time": int(OBSERVED.timestamp() * 1000),
    }
    return parse_response(
        envelope,
        category="linear",
        observed_at=OBSERVED,
    )[0]


def test_continuity_repair_retains_exact_provider_capture_and_builds_complete_replay(
    tmp_path,
) -> None:
    body = response_body([row(0), row(1), row(2)])
    transport = FakeTransport(body)
    path = tmp_path / "marketdata.sqlite"
    marketdata = SQLiteOperationalMarketDataStore(path)
    repair_store = SQLiteOperationalRepairBarStore(path)
    continuity = SQLiteOperationalContinuityStore(path)
    client = BybitPublicKlineClient(
        subscription=subscription(),
        transport=transport,
    )
    service = BybitContinuityRepairService(
        subscription=subscription(),
        marketdata=marketdata,
        repair_store=repair_store,
        continuity=continuity,
        client=client,
    )

    result = service.repair(
        observed_at=OBSERVED,
        bootstrap_open_time=BASE,
    )

    capture = result.provider_capture
    assert capture is not None
    assert capture.response_body == body
    assert capture.response_sha256 == hashlib.sha256(body).hexdigest()
    assert capture.response_received_at == OBSERVED
    assert capture.server_at == OBSERVED
    assert len(capture.raw_rows) == 3
    assert len(set(capture.raw_row_sha256)) == 3
    assert capture.bars[-1].bar_id == result.checkpoint.through_bar_id

    evidence = build_bybit_provider_complete_replay(
        capture=capture,
        instrument_spec=instrument_spec(),
        continuity_checkpoint=result.checkpoint,
        strategy_id=STRATEGY,
    )

    assert evidence.replay.profile is ReplayEvidenceProfile.PROVIDER_COMPLETE
    assert evidence.replay.limitations == ()
    assert evidence.raw_response_sha256 == hashlib.sha256(body).hexdigest()
    assert evidence.strategy_id == STRATEGY
    assert evidence.continuity_checkpoint_id == result.checkpoint.checkpoint_id
    assert evidence.continuity_through_bar_id == capture.bars[-1].bar_id
    assert [event.continuity for event in evidence.replay.events] == [
        ReplayContinuity.ROOT,
        ReplayContinuity.CONTIGUOUS,
        ReplayContinuity.CONTIGUOUS,
    ]
    for index, (event, binding, bar) in enumerate(
        zip(evidence.replay.events, evidence.bindings, capture.bars, strict=True)
    ):
        assert event.sequence == index + 1
        assert event.source_event_id == bar.source_event_id
        assert event.receive_timestamp == OBSERVED
        assert event.exchange_timestamp == bar.source_timestamp
        assert event.raw_event_sha256 == capture.raw_row_sha256[index]
        assert event.normalized_event_sha256 == bar.content_hash
        assert binding.normalized_event_sha256 == bar.content_hash
        assert len(binding.strategy_input_sha256) == 64
        assert len(binding.risk_market_input_sha256) == 64
        assert binding.replay_event_digest == event.digest
    assert len(evidence.replay.evidence_sha256) == 64
    assert len(evidence.evidence_sha256) == 64


def test_capture_rejects_raw_response_substitution_even_when_normalized_bars_are_unchanged(
    tmp_path,
) -> None:
    original_body = response_body([row(0), row(1), row(2)])
    transport = FakeTransport(original_body)
    client = BybitPublicKlineClient(
        subscription=subscription(),
        transport=transport,
    )
    capture = client.fetch_closed_range_capture(
        first_open_time=BASE,
        last_open_time=BASE + timedelta(minutes=10),
        observed_at=OBSERVED,
    )

    substituted = replace(
        capture,
        response_body=response_body([row(0), row(1, close="777"), row(2)]),
    )
    with pytest.raises(ValueError, match="raw rows disagree"):
        substituted.validate()


def test_transport_receive_timestamp_is_preserved_in_provider_evidence() -> None:
    received = OBSERVED + timedelta(milliseconds=250)
    transport = FakeTransport(
        response_body([row(0), row(1), row(2)]),
        received_at=received,
    )
    client = BybitPublicKlineClient(
        subscription=subscription(),
        transport=transport,
    )

    capture = client.fetch_closed_range_capture(
        first_open_time=BASE,
        last_open_time=BASE + timedelta(minutes=10),
        observed_at=OBSERVED,
    )

    assert capture.response_received_at == received
    assert all(bar.received_at == received for bar in capture.bars)


def conformance_bundle(tmp_path):
    body = response_body([row(0), row(1), row(2)])
    path = tmp_path / "conformance.sqlite"
    marketdata = SQLiteOperationalMarketDataStore(path)
    repair_store = SQLiteOperationalRepairBarStore(path)
    continuity = SQLiteOperationalContinuityStore(path)
    client = BybitPublicKlineClient(
        subscription=subscription(),
        transport=FakeTransport(body),
    )
    result = BybitContinuityRepairService(
        subscription=subscription(),
        marketdata=marketdata,
        repair_store=repair_store,
        continuity=continuity,
        client=client,
    ).repair(
        observed_at=OBSERVED,
        bootstrap_open_time=BASE,
    )
    capture = result.provider_capture
    assert capture is not None
    spec = instrument_spec()
    evidence = build_bybit_provider_complete_replay(
        capture=capture,
        instrument_spec=spec,
        continuity_checkpoint=result.checkpoint,
        strategy_id=STRATEGY,
    )
    return capture, spec, result.checkpoint, evidence


def test_bybit_public_adapter_conformance_is_machine_testable_and_scope_honest(
    tmp_path,
) -> None:
    capture, spec, checkpoint, evidence = conformance_bundle(tmp_path)

    report = qualify_bybit_public_marketdata_adapter(
        capture=capture,
        instrument_spec=spec,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        subject_version="296d627be05a2ce5fd115fb52a3245324c5a69e8",
    )

    assert report.qualified
    assert report.failure_reasons == ()
    assert len(report.evidence_sha256) == 64
    required = [check for check in report.checks if check.required]
    assert required
    assert all(check.status is ConformanceStatus.PASS for check in required)
    not_in_scope = {
        check.check_id
        for check in report.checks
        if check.status is ConformanceStatus.NOT_IN_SCOPE
    }
    assert not_in_scope == {
        "EXECUTION_ORDER_SUBMIT",
        "EXECUTION_CANCEL_REPLACE",
        "EXECUTION_FILL_STATUS_MAPPING",
        "ACCOUNT_RECONCILIATION",
    }


def test_conformance_detects_downstream_digest_substitution(tmp_path) -> None:
    capture, spec, checkpoint, evidence = conformance_bundle(tmp_path)
    first = replace(
        evidence.bindings[0],
        strategy_input_sha256="0" * 64,
    )
    substituted = replace(
        evidence,
        bindings=(first, *evidence.bindings[1:]),
    )

    report = qualify_bybit_public_marketdata_adapter(
        capture=capture,
        instrument_spec=spec,
        continuity_checkpoint=checkpoint,
        provider_replay=substituted,
        subject_version="candidate-sha",
    )

    assert not report.qualified
    failed = {
        check.check_id: check
        for check in report.checks
        if check.status is ConformanceStatus.FAIL
    }
    assert "DOWNSTREAM_INPUT_BINDING" in failed
    assert report.failure_reasons == (
        "DOWNSTREAM_INPUT_BINDING:"
        "strategy/risk market inputs cannot be independently reproduced",
    )


def test_conformance_blocks_dependent_checks_when_raw_capture_is_structurally_invalid(
    tmp_path,
) -> None:
    capture, spec, checkpoint, evidence = conformance_bundle(tmp_path)
    broken_capture = replace(
        capture,
        response_body=response_body([row(0), row(1, close="777"), row(2)]),
    )

    report = qualify_bybit_public_marketdata_adapter(
        capture=broken_capture,
        instrument_spec=spec,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        subject_version="candidate-sha",
    )

    assert not report.qualified
    statuses = {check.check_id: check.status for check in report.checks}
    assert statuses["STRUCTURAL_EVIDENCE_INTEGRITY"] is ConformanceStatus.FAIL
    assert statuses["RAW_RESPONSE_BINDING"] is ConformanceStatus.BLOCKED
    assert statuses["DOWNSTREAM_INPUT_BINDING"] is ConformanceStatus.BLOCKED



def integrity_bundle(tmp_path):
    capture, spec, checkpoint, evidence = conformance_bundle(tmp_path)
    report = qualify_bybit_public_marketdata_adapter(
        capture=capture,
        instrument_spec=spec,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        subject_version="a1308410f64ff9a639e0fb8c32e287fa2c484a0e",
    )
    assert report.qualified
    policy = MarketDataIntegrityPolicy(
        maximum_server_skew_seconds=Decimal("5"),
        high_water_receive_delay_seconds=Decimal("5"),
        maximum_final_bar_age_seconds=Decimal("10"),
    )
    return capture, checkpoint, evidence, report, policy


def test_marketdata_integrity_authority_accepts_only_qualified_fresh_feed(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)

    decision = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
    )

    assert decision.status is MarketDataIntegrityStatus.HEALTHY
    assert decision.action is MarketDataSafetyAction.ALLOW_QUALIFIED_PAPER_INPUT
    assert decision.reasons == ()
    assert decision.conflict_count == 0
    assert len(decision.evidence_sha256) == 64


def test_marketdata_integrity_decision_is_deterministic(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)
    kwargs = dict(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
    )

    first = evaluate_bybit_marketdata_integrity(**kwargs)
    second = evaluate_bybit_marketdata_integrity(**kwargs)

    assert first == second
    assert first.evidence_sha256 == second.evidence_sha256


def test_marketdata_conflict_quarantines_feed(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)

    decision = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
        conflict_count=1,
    )

    assert decision.status is MarketDataIntegrityStatus.QUARANTINED
    assert decision.action is MarketDataSafetyAction.QUARANTINE_FEED
    assert decision.reasons == ("MARKET_DATA_CONFLICT_PRESENT",)


def test_stale_final_bar_forces_read_only(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)

    decision = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED + timedelta(seconds=20),
    )

    assert decision.status is MarketDataIntegrityStatus.STALE
    assert decision.action is MarketDataSafetyAction.READ_ONLY
    assert decision.reasons == ("FINAL_BAR_STALE",)


def test_provider_clock_or_delivery_degradation_forces_read_only(tmp_path) -> None:
    capture, checkpoint, evidence, report, _ = integrity_bundle(tmp_path)
    strict = MarketDataIntegrityPolicy(
        maximum_server_skew_seconds=Decimal("0"),
        high_water_receive_delay_seconds=Decimal("1"),
        maximum_final_bar_age_seconds=Decimal("10"),
    )

    decision = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=strict,
        observed_at=OBSERVED,
    )

    assert decision.status is MarketDataIntegrityStatus.DEGRADED
    assert decision.action is MarketDataSafetyAction.READ_ONLY
    assert "PROVIDER_EVENT_DELAY_EXCEEDED" in decision.reasons


def test_unqualified_adapter_blocks_marketdata_qualification(tmp_path) -> None:
    capture, spec, checkpoint, evidence = conformance_bundle(tmp_path)
    substituted = replace(
        evidence,
        bindings=(
            replace(evidence.bindings[0], strategy_input_sha256="0" * 64),
            *evidence.bindings[1:],
        ),
    )
    failed_report = qualify_bybit_public_marketdata_adapter(
        capture=capture,
        instrument_spec=spec,
        continuity_checkpoint=checkpoint,
        provider_replay=substituted,
        subject_version="candidate-sha",
    )
    assert not failed_report.qualified
    policy = MarketDataIntegrityPolicy(
        maximum_server_skew_seconds=Decimal("5"),
        high_water_receive_delay_seconds=Decimal("5"),
        maximum_final_bar_age_seconds=Decimal("10"),
    )

    decision = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=failed_report,
        policy=policy,
        observed_at=OBSERVED,
    )

    assert decision.status is MarketDataIntegrityStatus.BLOCKED
    assert decision.action is MarketDataSafetyAction.BLOCK_QUALIFICATION
    assert decision.reasons == ("ADAPTER_NOT_QUALIFIED",)



def test_marketdata_integrity_quarantines_substituted_continuity_identity(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)
    substituted = replace(
        evidence,
        continuity_checkpoint_id="substituted-checkpoint",
    )
    substituted.validate()

    decision = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=substituted,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
    )

    assert decision.status is MarketDataIntegrityStatus.QUARANTINED
    assert decision.action is MarketDataSafetyAction.QUARANTINE_FEED
    assert decision.reasons == ("CONTINUITY_PROOF_MISMATCH",)


def test_marketdata_integrity_blocks_structurally_corrupt_capture(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)
    broken_capture = replace(
        capture,
        response_body=response_body([row(0), row(1, close="777"), row(2)]),
    )

    decision = evaluate_bybit_marketdata_integrity(
        capture=broken_capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
    )

    assert decision.status is MarketDataIntegrityStatus.BLOCKED
    assert decision.action is MarketDataSafetyAction.BLOCK_QUALIFICATION
    assert decision.reasons == ("STRUCTURAL_EVIDENCE_INVALID",)


def test_marketdata_integrity_policy_rejects_negative_threshold() -> None:
    policy = MarketDataIntegrityPolicy(
        maximum_server_skew_seconds=Decimal("-1"),
        high_water_receive_delay_seconds=Decimal("5"),
        maximum_final_bar_age_seconds=Decimal("10"),
    )

    with pytest.raises(ValueError, match="maximum_server_skew_seconds"):
        policy.validate()


def test_marketdata_integrity_rejects_negative_conflict_count(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)

    with pytest.raises(ValueError, match="conflict_count"):
        evaluate_bybit_marketdata_integrity(
            capture=capture,
            continuity_checkpoint=checkpoint,
            provider_replay=evidence,
            adapter_conformance=report,
            policy=policy,
            observed_at=OBSERVED,
            conflict_count=-1,
        )


def test_marketdata_integrity_rejects_future_high_water_observation(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)

    with pytest.raises(ValueError, match="future"):
        evaluate_bybit_marketdata_integrity(
            capture=capture,
            continuity_checkpoint=checkpoint,
            provider_replay=evidence,
            adapter_conformance=report,
            policy=policy,
            observed_at=checkpoint.through_close_time - timedelta(seconds=1),
        )



def qualification_job_request(evidence) -> QualificationJobRequest:
    return QualificationJobRequest(
        organisation_id="ASTRA_INTERNAL",
        subject="BYBIT_PUBLIC_MARKETDATA_ADAPTER",
        subject_version="a1308410f64ff9a639e0fb8c32e287fa2c484a0e",
        profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
        profile_version="1.0.0",
        environment="mainnet-public-readonly",
        corpus_ids=(evidence.replay.dataset_id,),
        submitted_at=OBSERVED,
    )


def test_qualification_job_passes_only_on_bound_healthy_evidence(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)
    integrity = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
    )
    request = qualification_job_request(evidence)

    result = evaluate_public_marketdata_qualification_job(
        request=request,
        adapter_conformance=report,
        marketdata_integrity=integrity,
        started_at=OBSERVED + timedelta(seconds=1),
        completed_at=OBSERVED + timedelta(seconds=2),
    )

    assert result.status is QualificationJobStatus.PASS
    assert result.reasons == ()
    assert result.job_id == request.job_id
    assert result.job_id.startswith("qjob_")
    assert len(result.result_sha256) == 64


def test_qualification_job_is_deterministic_for_identical_request_and_evidence(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)
    integrity = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
    )
    kwargs = dict(
        request=qualification_job_request(evidence),
        adapter_conformance=report,
        marketdata_integrity=integrity,
        started_at=OBSERVED + timedelta(seconds=1),
        completed_at=OBSERVED + timedelta(seconds=2),
    )

    first = evaluate_public_marketdata_qualification_job(**kwargs)
    second = evaluate_public_marketdata_qualification_job(**kwargs)

    assert first == second
    assert first.result_sha256 == second.result_sha256


def test_qualification_job_fails_when_marketdata_integrity_is_degraded(tmp_path) -> None:
    capture, checkpoint, evidence, report, _ = integrity_bundle(tmp_path)
    strict = MarketDataIntegrityPolicy(
        maximum_server_skew_seconds=Decimal("5"),
        high_water_receive_delay_seconds=Decimal("1"),
        maximum_final_bar_age_seconds=Decimal("10"),
    )
    integrity = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=strict,
        observed_at=OBSERVED,
    )
    assert integrity.status is MarketDataIntegrityStatus.DEGRADED

    result = evaluate_public_marketdata_qualification_job(
        request=qualification_job_request(evidence),
        adapter_conformance=report,
        marketdata_integrity=integrity,
        started_at=OBSERVED + timedelta(seconds=1),
        completed_at=OBSERVED + timedelta(seconds=2),
    )

    assert result.status is QualificationJobStatus.FAIL
    assert result.reasons == ("MARKET_DATA_INTEGRITY_DEGRADED",)


def test_qualification_job_blocks_profile_substitution(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)
    integrity = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
    )
    request = replace(
        qualification_job_request(evidence),
        profile_version="9.9.9",
    )

    result = evaluate_public_marketdata_qualification_job(
        request=request,
        adapter_conformance=report,
        marketdata_integrity=integrity,
        started_at=OBSERVED + timedelta(seconds=1),
        completed_at=OBSERVED + timedelta(seconds=2),
    )

    assert result.status is QualificationJobStatus.BLOCKED
    assert result.reasons == ("PROFILE_VERSION_MISMATCH",)


def test_qualification_job_blocks_broken_evidence_chain(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)
    integrity = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
    )
    substituted = replace(
        integrity,
        adapter_conformance_sha256="0" * 64,
    )
    substituted.validate()

    result = evaluate_public_marketdata_qualification_job(
        request=qualification_job_request(evidence),
        adapter_conformance=report,
        marketdata_integrity=substituted,
        started_at=OBSERVED + timedelta(seconds=1),
        completed_at=OBSERVED + timedelta(seconds=2),
    )

    assert result.status is QualificationJobStatus.BLOCKED
    assert result.reasons == ("EVIDENCE_CHAIN_MISMATCH",)


def test_qualification_job_request_rejects_duplicate_corpus_ids() -> None:
    request = QualificationJobRequest(
        organisation_id="ASTRA_INTERNAL",
        subject="BYBIT_PUBLIC_MARKETDATA_ADAPTER",
        subject_version="candidate-sha",
        profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
        profile_version="1.0.0",
        environment="mainnet-public-readonly",
        corpus_ids=("same", "same"),
        submitted_at=OBSERVED,
    )

    with pytest.raises(ValueError, match="unique"):
        request.validate()



def test_qualification_job_fails_when_adapter_conformance_failed(tmp_path) -> None:
    capture, spec, checkpoint, evidence = conformance_bundle(tmp_path)
    substituted = replace(
        evidence,
        bindings=(
            replace(evidence.bindings[0], strategy_input_sha256="0" * 64),
            *evidence.bindings[1:],
        ),
    )
    failed_report = qualify_bybit_public_marketdata_adapter(
        capture=capture,
        instrument_spec=spec,
        continuity_checkpoint=checkpoint,
        provider_replay=substituted,
        subject_version="a1308410f64ff9a639e0fb8c32e287fa2c484a0e",
    )
    assert not failed_report.qualified
    policy = MarketDataIntegrityPolicy(
        maximum_server_skew_seconds=Decimal("5"),
        high_water_receive_delay_seconds=Decimal("5"),
        maximum_final_bar_age_seconds=Decimal("10"),
    )
    integrity = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=failed_report,
        policy=policy,
        observed_at=OBSERVED,
    )

    result = evaluate_public_marketdata_qualification_job(
        request=qualification_job_request(evidence),
        adapter_conformance=failed_report,
        marketdata_integrity=integrity,
        started_at=OBSERVED + timedelta(seconds=1),
        completed_at=OBSERVED + timedelta(seconds=2),
    )

    assert result.status is QualificationJobStatus.FAIL
    assert result.reasons == ("ADAPTER_CONFORMANCE_FAILED",)


def test_qualification_job_blocks_subject_substitution(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)
    integrity = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
    )
    request = replace(
        qualification_job_request(evidence),
        subject="SUBSTITUTED_ADAPTER",
    )

    result = evaluate_public_marketdata_qualification_job(
        request=request,
        adapter_conformance=report,
        marketdata_integrity=integrity,
        started_at=OBSERVED + timedelta(seconds=1),
        completed_at=OBSERVED + timedelta(seconds=2),
    )

    assert result.status is QualificationJobStatus.BLOCKED
    assert result.reasons == ("SUBJECT_MISMATCH",)


def test_qualification_job_blocks_structurally_invalid_integrity_evidence(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)
    integrity = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
    )
    invalid_integrity = replace(
        integrity,
        schema_version="invalid-schema",
    )

    result = evaluate_public_marketdata_qualification_job(
        request=qualification_job_request(evidence),
        adapter_conformance=report,
        marketdata_integrity=invalid_integrity,
        started_at=OBSERVED + timedelta(seconds=1),
        completed_at=OBSERVED + timedelta(seconds=2),
    )

    assert result.status is QualificationJobStatus.BLOCKED
    assert result.reasons == ("STRUCTURAL_EVIDENCE_INVALID",)
    assert len(result.marketdata_integrity_sha256) == 64


def test_qualification_job_result_rejects_invalid_lifecycle(tmp_path) -> None:
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)
    integrity = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
    )

    with pytest.raises(ValueError, match="before submission"):
        evaluate_public_marketdata_qualification_job(
            request=qualification_job_request(evidence),
            adapter_conformance=report,
            marketdata_integrity=integrity,
            started_at=OBSERVED - timedelta(seconds=1),
            completed_at=OBSERVED + timedelta(seconds=2),
        )


def test_qualification_job_request_requires_nonempty_corpus() -> None:
    request = QualificationJobRequest(
        organisation_id="ASTRA_INTERNAL",
        subject="BYBIT_PUBLIC_MARKETDATA_ADAPTER",
        subject_version="candidate-sha",
        profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
        profile_version="1.0.0",
        environment="mainnet-public-readonly",
        corpus_ids=(),
        submitted_at=OBSERVED,
    )

    with pytest.raises(ValueError, match="at least one corpus"):
        request.validate()



def passing_qualification_chain(tmp_path):
    capture, checkpoint, evidence, report, policy = integrity_bundle(tmp_path)
    integrity = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=policy,
        observed_at=OBSERVED,
    )
    job = evaluate_public_marketdata_qualification_job(
        request=qualification_job_request(evidence),
        adapter_conformance=report,
        marketdata_integrity=integrity,
        started_at=OBSERVED + timedelta(seconds=1),
        completed_at=OBSERVED + timedelta(seconds=2),
    )
    assert job.status is QualificationJobStatus.PASS
    return evidence, report, integrity, job


def test_qualification_manifest_binds_complete_success_chain(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)

    manifest = build_qualification_manifest(
        job_result=job,
        adapter_conformance=report,
        marketdata_integrity=integrity,
        provider_replay=evidence,
    )

    assert manifest.job_id == job.job_id
    assert manifest.job_result_sha256 == job.result_sha256
    assert manifest.request_sha256 == job.request.request_sha256
    assert manifest.provider_replay_sha256 == evidence.evidence_sha256
    assert manifest.adapter_conformance_sha256 == report.evidence_sha256
    assert manifest.marketdata_integrity_sha256 == integrity.evidence_sha256
    assert manifest.continuity_checkpoint_id == evidence.continuity_checkpoint_id
    assert manifest.scope == "PUBLIC_MARKET_DATA_ONLY"
    assert "PROFITABILITY_NOT_PROVEN" in manifest.limitations
    assert "REGULATORY_CERTIFICATION_NOT_CLAIMED" in manifest.limitations
    assert manifest.manifest_id.startswith("qmanifest_")
    assert len(manifest.manifest_sha256) == 64


def test_qualification_manifest_is_deterministic(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)

    first = build_qualification_manifest(
        job_result=job,
        adapter_conformance=report,
        marketdata_integrity=integrity,
        provider_replay=evidence,
    )
    second = build_qualification_manifest(
        job_result=job,
        adapter_conformance=report,
        marketdata_integrity=integrity,
        provider_replay=evidence,
    )

    assert first == second
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.payload() == second.payload()


def test_qualification_manifest_rejects_non_pass_job(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)
    failed_job = replace(
        job,
        status=QualificationJobStatus.FAIL,
        reasons=("MARKET_DATA_INTEGRITY_DEGRADED",),
    )
    failed_job.validate()

    with pytest.raises(ValueError, match="PASS job"):
        build_qualification_manifest(
            job_result=failed_job,
            adapter_conformance=report,
            marketdata_integrity=integrity,
            provider_replay=evidence,
        )


def test_qualification_manifest_rejects_job_result_evidence_substitution(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)
    substituted_job = replace(
        job,
        adapter_conformance_sha256="0" * 64,
    )
    substituted_job.validate()

    with pytest.raises(ValueError, match="adapter evidence mismatch"):
        build_qualification_manifest(
            job_result=substituted_job,
            adapter_conformance=report,
            marketdata_integrity=integrity,
            provider_replay=evidence,
        )


def test_qualification_manifest_rejects_provider_replay_substitution(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)
    substituted = replace(
        evidence,
        strategy_id="substituted-strategy",
    )
    substituted.validate()

    with pytest.raises(ValueError, match="provider replay mismatch"):
        build_qualification_manifest(
            job_result=job,
            adapter_conformance=report,
            marketdata_integrity=integrity,
            provider_replay=substituted,
        )


def test_qualification_manifest_rejects_corpus_substitution(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)
    request = replace(job.request, corpus_ids=("other-corpus",))
    substituted_job = replace(job, request=request)

    with pytest.raises(ValueError, match="corpus"):
        build_qualification_manifest(
            job_result=substituted_job,
            adapter_conformance=report,
            marketdata_integrity=integrity,
            provider_replay=evidence,
        )


def test_qualification_manifest_rejects_degraded_feed(tmp_path) -> None:
    capture, checkpoint, evidence, report, _ = integrity_bundle(tmp_path)
    strict = MarketDataIntegrityPolicy(
        maximum_server_skew_seconds=Decimal("5"),
        high_water_receive_delay_seconds=Decimal("1"),
        maximum_final_bar_age_seconds=Decimal("10"),
    )
    integrity = evaluate_bybit_marketdata_integrity(
        capture=capture,
        continuity_checkpoint=checkpoint,
        provider_replay=evidence,
        adapter_conformance=report,
        policy=strict,
        observed_at=OBSERVED,
    )
    assert integrity.status is MarketDataIntegrityStatus.DEGRADED
    failed_job = evaluate_public_marketdata_qualification_job(
        request=qualification_job_request(evidence),
        adapter_conformance=report,
        marketdata_integrity=integrity,
        started_at=OBSERVED + timedelta(seconds=1),
        completed_at=OBSERVED + timedelta(seconds=2),
    )
    assert failed_job.status is QualificationJobStatus.FAIL

    with pytest.raises(ValueError, match="PASS job"):
        build_qualification_manifest(
            job_result=failed_job,
            adapter_conformance=report,
            marketdata_integrity=integrity,
            provider_replay=evidence,
        )



def test_qualification_manifest_rejects_profile_identity_substitution(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)
    substituted = replace(report, profile_id="OTHER_PROFILE")
    substituted.validate()

    with pytest.raises(ValueError, match="profile_id mismatch"):
        build_qualification_manifest(
            job_result=job,
            adapter_conformance=substituted,
            marketdata_integrity=integrity,
            provider_replay=evidence,
        )


def test_qualification_manifest_rejects_subject_version_substitution(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)
    substituted = replace(report, subject_version="other-build")
    substituted.validate()

    with pytest.raises(ValueError, match="subject_version mismatch"):
        build_qualification_manifest(
            job_result=job,
            adapter_conformance=substituted,
            marketdata_integrity=integrity,
            provider_replay=evidence,
        )


def test_qualification_manifest_rejects_integrity_evidence_substitution(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)
    substituted_job = replace(
        job,
        marketdata_integrity_sha256="0" * 64,
    )
    substituted_job.validate()

    with pytest.raises(ValueError, match="integrity evidence mismatch"):
        build_qualification_manifest(
            job_result=substituted_job,
            adapter_conformance=report,
            marketdata_integrity=integrity,
            provider_replay=evidence,
        )


def test_qualification_manifest_rejects_continuity_substitution(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)
    substituted = replace(
        evidence,
        continuity_checkpoint_id="other-checkpoint",
    )
    substituted.validate()

    substituted_integrity = replace(
        integrity,
        provider_replay_sha256=substituted.evidence_sha256,
    )
    substituted_integrity.validate()
    rebound_job = replace(
        job,
        marketdata_integrity_sha256=substituted_integrity.evidence_sha256,
    )
    rebound_job.validate()

    with pytest.raises(ValueError, match="continuity mismatch"):
        build_qualification_manifest(
            job_result=rebound_job,
            adapter_conformance=report,
            marketdata_integrity=substituted_integrity,
            provider_replay=substituted,
        )


def test_qualification_manifest_validation_rejects_scope_and_claim_drift(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)
    manifest = build_qualification_manifest(
        job_result=job,
        adapter_conformance=report,
        marketdata_integrity=integrity,
        provider_replay=evidence,
    )

    with pytest.raises(ValueError, match="scope mismatch"):
        replace(manifest, scope="EXECUTION").validate()

    with pytest.raises(ValueError, match="assertions mismatch"):
        replace(manifest, assertions=("ADAPTER_CONFORMANCE_PASS",)).validate()

    with pytest.raises(ValueError, match="limitations mismatch"):
        replace(manifest, limitations=("PROFITABILITY_NOT_PROVEN",)).validate()



def test_profile_binding_binds_exact_manifest_and_policy_digest(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)
    manifest = build_qualification_manifest(
        job_result=job,
        adapter_conformance=report,
        marketdata_integrity=integrity,
        provider_replay=evidence,
    )
    profile = QualificationProfile(
        profile_id=manifest.profile_id,
        version=manifest.profile_version,
        scope=manifest.scope,
        allowed_environments=(manifest.environment,),
        required_corpus_classes=("BYBIT_PUBLIC_KLINE",),
        required_checks=tuple(check.check_id for check in report.checks),
        required_assertions=manifest.assertions,
        required_limitations=manifest.limitations,
        created_at=OBSERVED,
    )

    bound = bind_manifest_to_profile(
        manifest=manifest,
        profile=profile,
        adapter_conformance=report,
    )

    assert bound.manifest_id == manifest.manifest_id
    assert bound.manifest_sha256 == manifest.manifest_sha256
    assert bound.profile_sha256 == profile.profile_sha256
    assert bound.binding_id.startswith("qbinding_")
    assert len(bound.binding_sha256) == 64


def test_profile_binding_rejects_policy_mutation_under_same_version(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)
    manifest = build_qualification_manifest(
        job_result=job,
        adapter_conformance=report,
        marketdata_integrity=integrity,
        provider_replay=evidence,
    )
    profile = QualificationProfile(
        profile_id=manifest.profile_id,
        version=manifest.profile_version,
        scope=manifest.scope,
        allowed_environments=(manifest.environment,),
        required_corpus_classes=("BYBIT_PUBLIC_KLINE",),
        required_checks=tuple(check.check_id for check in report.checks),
        required_assertions=manifest.assertions,
        required_limitations=manifest.limitations,
        created_at=OBSERVED,
    )
    mutated = replace(
        profile,
        required_checks=(*profile.required_checks, "NEW-UNSATISFIED-CHECK"),
    )
    mutated.validate()

    with pytest.raises(ValueError, match="missing required checks"):
        bind_manifest_to_profile(
            manifest=manifest,
            profile=mutated,
            adapter_conformance=report,
        )


def test_profile_binding_rejects_environment_scope_and_profile_substitution(tmp_path) -> None:
    evidence, report, integrity, job = passing_qualification_chain(tmp_path)
    manifest = build_qualification_manifest(
        job_result=job,
        adapter_conformance=report,
        marketdata_integrity=integrity,
        provider_replay=evidence,
    )
    base = QualificationProfile(
        profile_id=manifest.profile_id,
        version=manifest.profile_version,
        scope=manifest.scope,
        allowed_environments=(manifest.environment,),
        required_corpus_classes=("BYBIT_PUBLIC_KLINE",),
        required_checks=tuple(check.check_id for check in report.checks),
        required_assertions=manifest.assertions,
        required_limitations=manifest.limitations,
        created_at=OBSERVED,
    )

    with pytest.raises(ValueError, match="profile_id mismatch"):
        bind_manifest_to_profile(
            manifest=manifest,
            profile=replace(base, profile_id="OTHER_PROFILE"),
            adapter_conformance=report,
        )

    with pytest.raises(ValueError, match="scope mismatch"):
        bind_manifest_to_profile(
            manifest=manifest,
            profile=replace(base, scope="EXECUTION"),
            adapter_conformance=report,
        )

    with pytest.raises(ValueError, match="environment is not allowed"):
        bind_manifest_to_profile(
            manifest=manifest,
            profile=replace(base, allowed_environments=("testnet",)),
            adapter_conformance=report,
        )
