from __future__ import annotations

from app.domain.instrument import InstrumentSpec
from app.marketdata.bybit_instruments import ENDPOINT as BYBIT_INSTRUMENT_ENDPOINT
from app.marketdata.bybit_public import BYBIT_PUBLIC_LINEAR_STREAM
from app.marketdata.bybit_repair import (
    BYBIT_LINEAR_VENUE,
    BYBIT_PROVIDER,
    BYBIT_PUBLIC_REST_BASE,
    BybitKlineRangeCapture,
)
from app.marketdata.continuity import OperationalContinuityCheckpoint
from app.qualification.adapter_conformance import (
    AdapterConformanceCheck,
    AdapterConformanceReport,
    ConformanceStatus,
    conformance_evidence_sha256,
)
from app.qualification.bybit_provider_replay import (
    BybitProviderReplayEvidence,
    risk_market_input_sha256,
    strategy_input_sha256,
)

PROFILE_ID = "ASTRA_BYBIT_PUBLIC_MARKETDATA"
PROFILE_VERSION = "1.0.0"
SUBJECT = "BYBIT_PUBLIC_MARKETDATA_ADAPTER"


def _check(
    check_id: str,
    *,
    condition: bool,
    evidence: object,
    reason: str,
) -> AdapterConformanceCheck:
    return AdapterConformanceCheck(
        check_id=check_id,
        status=ConformanceStatus.PASS if condition else ConformanceStatus.FAIL,
        required=True,
        evidence_sha256=conformance_evidence_sha256(evidence),
        reason=None if condition else reason,
    )


def _blocked(check_id: str, *, dependency_digest: str) -> AdapterConformanceCheck:
    return AdapterConformanceCheck(
        check_id=check_id,
        status=ConformanceStatus.BLOCKED,
        required=True,
        evidence_sha256=dependency_digest,
        reason="STRUCTURAL_EVIDENCE_INVALID",
    )


def _execution_not_in_scope(check_id: str) -> AdapterConformanceCheck:
    return AdapterConformanceCheck(
        check_id=check_id,
        status=ConformanceStatus.NOT_IN_SCOPE,
        required=False,
        evidence_sha256=None,
        reason="PUBLIC_MARKETDATA_PROFILE_ONLY",
    )


def qualify_bybit_public_marketdata_adapter(
    *,
    capture: BybitKlineRangeCapture,
    instrument_spec: InstrumentSpec,
    continuity_checkpoint: OperationalContinuityCheckpoint,
    provider_replay: BybitProviderReplayEvidence,
    subject_version: str,
) -> AdapterConformanceReport:
    """Qualify only ASTRA's read-only Bybit public-marketdata adapter scope.

    This intentionally does not claim conformance for authenticated order execution,
    fills, cancel/replace or account reconciliation. Those require a different adapter
    and profile.
    """

    if not subject_version.strip():
        raise ValueError("subject_version is required")

    structural_errors: list[str] = []
    for name, value in (
        ("capture", capture),
        ("instrument_spec", instrument_spec),
        ("continuity_checkpoint", continuity_checkpoint),
        ("provider_replay", provider_replay),
    ):
        try:
            value.validate()
        except Exception as exc:
            structural_errors.append(f"{name}:{type(exc).__name__}:{exc}")

    structure_evidence = {
        "capture_response_bytes": len(capture.response_body),
        "capture_bars": len(capture.bars),
        "instrument_symbol": instrument_spec.symbol,
        "continuity_checkpoint_id": continuity_checkpoint.checkpoint_id,
        "provider_replay_schema": provider_replay.schema_version,
        "errors": structural_errors,
    }
    structure_digest = conformance_evidence_sha256(structure_evidence)
    checks: list[AdapterConformanceCheck] = [
        AdapterConformanceCheck(
            check_id="STRUCTURAL_EVIDENCE_INTEGRITY",
            status=(
                ConformanceStatus.PASS
                if not structural_errors
                else ConformanceStatus.FAIL
            ),
            required=True,
            evidence_sha256=structure_digest,
            reason=None if not structural_errors else ";".join(structural_errors),
        )
    ]

    dependent_ids = (
        "PROVIDER_SOURCE_IDENTITY",
        "RAW_RESPONSE_BINDING",
        "CLOCK_BINDING",
        "NORMALIZED_EVENT_BINDING",
        "INSTRUMENT_REVISION_BINDING",
        "CONTINUITY_BINDING",
        "DOWNSTREAM_INPUT_BINDING",
        "READ_ONLY_ENDPOINT_BOUNDARY",
    )
    if structural_errors:
        checks.extend(
            _blocked(check_id, dependency_digest=structure_digest)
            for check_id in dependent_ids
        )
    else:
        bars = capture.bars
        events = provider_replay.replay.events
        bindings = provider_replay.bindings
        raw_rows = capture.raw_row_sha256

        source_identity = (
            provider_replay.request_url == capture.request_url
            and provider_replay.replay.source_name == "BYBIT_V5_MARKET_KLINE_GET"
            and all(bar.provider == BYBIT_PROVIDER for bar in bars)
            and all(bar.venue == BYBIT_LINEAR_VENUE for bar in bars)
            and all(event.source_event_id == bar.source_event_id for event, bar in zip(events, bars, strict=True))
        )
        checks.append(
            _check(
                "PROVIDER_SOURCE_IDENTITY",
                condition=source_identity,
                evidence={
                    "request_url": capture.request_url,
                    "source_name": provider_replay.replay.source_name,
                    "provider": [bar.provider for bar in bars],
                    "venue": [bar.venue for bar in bars],
                    "source_event_id": [bar.source_event_id for bar in bars],
                },
                reason="provider/request/source event identity mismatch",
            )
        )

        raw_binding = (
            provider_replay.raw_response_sha256 == capture.response_sha256
            and len(events) == len(raw_rows)
            and all(
                event.raw_event_sha256 == raw_sha
                for event, raw_sha in zip(events, raw_rows, strict=True)
            )
        )
        checks.append(
            _check(
                "RAW_RESPONSE_BINDING",
                condition=raw_binding,
                evidence={
                    "capture_response_sha256": capture.response_sha256,
                    "replay_response_sha256": provider_replay.raw_response_sha256,
                    "capture_raw_row_sha256": list(raw_rows),
                    "replay_raw_row_sha256": [
                        event.raw_event_sha256 for event in events
                    ],
                },
                reason="raw provider bytes/rows are not bound to replay evidence",
            )
        )

        clock_binding = (
            provider_replay.response_received_at == capture.response_received_at
            and provider_replay.exchange_server_at == capture.server_at
            and all(
                event.receive_timestamp == capture.response_received_at
                and event.exchange_timestamp == bar.source_timestamp
                for event, bar in zip(events, bars, strict=True)
            )
        )
        checks.append(
            _check(
                "CLOCK_BINDING",
                condition=clock_binding,
                evidence={
                    "response_received_at": capture.response_received_at.isoformat(),
                    "exchange_server_at": capture.server_at.isoformat(),
                    "event_receive_at": [
                        None
                        if event.receive_timestamp is None
                        else event.receive_timestamp.isoformat()
                        for event in events
                    ],
                    "event_exchange_at": [
                        event.exchange_timestamp.isoformat() for event in events
                    ],
                },
                reason="provider receive/exchange clocks are not preserved",
            )
        )

        normalized_binding = (
            len(events) == len(bars)
            and len(bindings) == len(bars)
            and all(
                event.normalized_event_sha256 == bar.content_hash
                and binding.normalized_event_sha256 == bar.content_hash
                and binding.bar_id == bar.bar_id
                for event, binding, bar in zip(events, bindings, bars, strict=True)
            )
        )
        checks.append(
            _check(
                "NORMALIZED_EVENT_BINDING",
                condition=normalized_binding,
                evidence={
                    "bar_id": [bar.bar_id for bar in bars],
                    "content_hash": [bar.content_hash for bar in bars],
                    "event_hash": [
                        event.normalized_event_sha256 for event in events
                    ],
                },
                reason="normalized OperationalBar identity diverges from replay evidence",
            )
        )

        spec_revision = instrument_spec.revision
        instrument_binding = (
            provider_replay.instrument_spec_revision == spec_revision
            and instrument_spec.symbol == bars[0].symbol
            and all(
                event.instrument_spec_revision == spec_revision
                for event in events
            )
        )
        checks.append(
            _check(
                "INSTRUMENT_REVISION_BINDING",
                condition=instrument_binding,
                evidence={
                    "symbol": instrument_spec.symbol,
                    "spec_revision": spec_revision,
                    "replay_spec_revision": provider_replay.instrument_spec_revision,
                    "event_spec_revision": [
                        event.instrument_spec_revision for event in events
                    ],
                },
                reason="instrument specification revision is not bound to replay",
            )
        )

        continuity_binding = (
            provider_replay.continuity_checkpoint_id
            == continuity_checkpoint.checkpoint_id
            and provider_replay.continuity_through_bar_id
            == continuity_checkpoint.through_bar_id
            and continuity_checkpoint.through_bar_id == bars[-1].bar_id
            and continuity_checkpoint.through_close_time == bars[-1].close_time
        )
        checks.append(
            _check(
                "CONTINUITY_BINDING",
                condition=continuity_binding,
                evidence={
                    "checkpoint_id": continuity_checkpoint.checkpoint_id,
                    "through_bar_id": continuity_checkpoint.through_bar_id,
                    "through_close_time": continuity_checkpoint.through_close_time.isoformat(),
                    "last_bar_id": bars[-1].bar_id,
                    "last_close_time": bars[-1].close_time.isoformat(),
                },
                reason="durable continuity checkpoint does not prove replay high-water",
            )
        )

        downstream_binding = all(
            binding.strategy_input_sha256
            == strategy_input_sha256(
                bar,
                strategy_id=provider_replay.strategy_id,
            )
            and binding.risk_market_input_sha256
            == risk_market_input_sha256(
                bar,
                instrument_spec_revision=spec_revision,
            )
            for binding, bar in zip(bindings, bars, strict=True)
        )
        checks.append(
            _check(
                "DOWNSTREAM_INPUT_BINDING",
                condition=downstream_binding,
                evidence={
                    "strategy_id": provider_replay.strategy_id,
                    "strategy_input_sha256": [
                        binding.strategy_input_sha256 for binding in bindings
                    ],
                    "risk_market_input_sha256": [
                        binding.risk_market_input_sha256 for binding in bindings
                    ],
                },
                reason="strategy/risk market inputs cannot be independently reproduced",
            )
        )

        read_only_boundary = (
            BYBIT_PUBLIC_LINEAR_STREAM
            == "wss://stream.bybit.com/v5/public/linear"
            and BYBIT_PUBLIC_REST_BASE == "https://api.bybit.com"
            and BYBIT_INSTRUMENT_ENDPOINT
            == "https://api.bybit.com/v5/market/instruments-info"
            and capture.request_url.startswith(
                "https://api.bybit.com/v5/market/kline?"
            )
            and "/order/" not in capture.request_url
            and "/position/" not in capture.request_url
        )
        checks.append(
            _check(
                "READ_ONLY_ENDPOINT_BOUNDARY",
                condition=read_only_boundary,
                evidence={
                    "websocket": BYBIT_PUBLIC_LINEAR_STREAM,
                    "rest_base": BYBIT_PUBLIC_REST_BASE,
                    "instrument_endpoint": BYBIT_INSTRUMENT_ENDPOINT,
                    "capture_url": capture.request_url,
                },
                reason="Bybit public-marketdata profile crossed a mutating endpoint boundary",
            )
        )

    checks.extend(
        (
            _execution_not_in_scope("EXECUTION_ORDER_SUBMIT"),
            _execution_not_in_scope("EXECUTION_CANCEL_REPLACE"),
            _execution_not_in_scope("EXECUTION_FILL_STATUS_MAPPING"),
            _execution_not_in_scope("ACCOUNT_RECONCILIATION"),
        )
    )
    report = AdapterConformanceReport(
        profile_id=PROFILE_ID,
        profile_version=PROFILE_VERSION,
        subject=SUBJECT,
        subject_version=subject_version,
        checks=tuple(checks),
    )
    report.validate()
    return report
