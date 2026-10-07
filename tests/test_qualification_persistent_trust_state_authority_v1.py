from __future__ import annotations

import json
import multiprocessing
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import app.qualification.persistent_trust_state_authority_v1 as authority_module
from app.qualification.persistent_trust_state_authority_v1 import (
    PersistentTrustStateAuthorityError,
    PersistentTrustStateAuthorityV1,
    PersistentTrustStateCASMismatch,
    PersistentTrustStateCorruption,
    PersistentTrustStateRecord,
    TrustStateTransitionContext,
)
from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.qualification.verification_service_v4 import QualificationTrustStateV4

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _state(
    *,
    profile_count: int,
    profile_head: str,
    tree_size: int,
    tree_root: str,
    checkpoint: str,
) -> QualificationTrustStateV4:
    return QualificationTrustStateV4(
        profile_event_count=profile_count,
        profile_event_head_sha256=profile_head,
        transparency_tree_size=tree_size,
        transparency_root_sha256=tree_root,
        checkpoint_v4_sha256=checkpoint,
    )


def _initial_state() -> QualificationTrustStateV4:
    return _state(
        profile_count=0,
        profile_head="0" * 64,
        tree_size=1,
        tree_root="1" * 64,
        checkpoint="0" * 64,
    )


def _next_state() -> QualificationTrustStateV4:
    return _state(
        profile_count=1,
        profile_head="2" * 64,
        tree_size=3,
        tree_root="3" * 64,
        checkpoint="4" * 64,
    )


def _third_state() -> QualificationTrustStateV4:
    return _state(
        profile_count=2,
        profile_head="5" * 64,
        tree_size=4,
        tree_root="6" * 64,
        checkpoint="7" * 64,
    )


def _transition(seed: str, checkpoint_sha256: str) -> TrustStateTransitionContext:
    return TrustStateTransitionContext(
        artifact_id=f"qartifact_{seed * 24}",
        artifact_sha256=seed * 64,
        bundle_id=f"qverifyv4_{seed * 24}",
        bundle_sha256=seed * 64,
        checkpoint_v4_id=f"qtrustv4_{seed * 24}",
        checkpoint_v4_sha256=checkpoint_sha256,
        verified_at=NOW,
    )


def _advance_worker(
    directory: str,
    expected_generation: int,
    expected_record_sha256: str,
    expected_state_sha256: str,
    queue,
) -> None:
    authority = PersistentTrustStateAuthorityV1(Path(directory))
    try:
        record, _ = authority.advance(
            next_state=_next_state(),
            transition=_transition("b", "4" * 64),
            expected_generation=expected_generation,
            expected_record_sha256=expected_record_sha256,
            expected_trust_state_sha256=expected_state_sha256,
        )
        queue.put(("ok", record.generation, record.record_sha256))
    except PersistentTrustStateCASMismatch:
        queue.put(("cas", None, None))
    except Exception as exc:  # pragma: no cover - child diagnostic
        queue.put(("error", type(exc).__name__, str(exc)))


def test_initialize_and_advance_build_hash_chained_history(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )

    current, receipt = authority.advance(
        next_state=_next_state(),
        transition=_transition("b", "4" * 64),
        expected_generation=initial.generation,
        expected_record_sha256=initial.record_sha256,
        expected_trust_state_sha256=initial.trust_state_sha256,
    )

    assert initial.generation == 0
    assert current.generation == 1
    assert current.previous_record_sha256 == initial.record_sha256
    assert receipt.previous_record_sha256 == initial.record_sha256
    assert receipt.current_record_sha256 == current.record_sha256
    assert receipt.current_trust_state_sha256 == current.trust_state_sha256
    assert authority.current() == current
    history = authority.export_history()
    assert [record.generation for record in history] == [0, 1]


def test_stale_cas_rejected_without_mutation(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    current, _ = authority.advance(
        next_state=_next_state(),
        transition=_transition("b", "4" * 64),
        expected_generation=0,
        expected_record_sha256=initial.record_sha256,
        expected_trust_state_sha256=initial.trust_state_sha256,
    )
    before = (tmp_path / "current.json").read_bytes()
    history_before = sorted(path.name for path in (tmp_path / "history").glob("*.json"))

    with pytest.raises(PersistentTrustStateCASMismatch):
        authority.advance(
            next_state=_third_state(),
            transition=_transition("c", "7" * 64),
            expected_generation=0,
            expected_record_sha256=initial.record_sha256,
            expected_trust_state_sha256=initial.trust_state_sha256,
        )

    assert authority.current() == current
    assert (tmp_path / "current.json").read_bytes() == before
    assert sorted(path.name for path in (tmp_path / "history").glob("*.json")) == history_before


def test_state_regression_rejected_without_mutation(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    current, _ = authority.advance(
        next_state=_next_state(),
        transition=_transition("b", "4" * 64),
        expected_generation=0,
        expected_record_sha256=initial.record_sha256,
        expected_trust_state_sha256=initial.trust_state_sha256,
    )
    regressed = _state(
        profile_count=0,
        profile_head="0" * 64,
        tree_size=2,
        tree_root="8" * 64,
        checkpoint="9" * 64,
    )

    with pytest.raises(PersistentTrustStateAuthorityError, match="regression"):
        authority.advance(
            next_state=regressed,
            transition=_transition("c", "9" * 64),
            expected_generation=current.generation,
            expected_record_sha256=current.record_sha256,
            expected_trust_state_sha256=current.trust_state_sha256,
        )

    assert authority.current() == current


def test_crash_after_history_commit_before_current_update_recovers(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    next_record = PersistentTrustStateRecord.build(
        generation=1,
        previous_record_sha256=initial.record_sha256,
        trust_state=_next_state(),
        transition=_transition("b", "4" * 64),
    )
    history_path = tmp_path / "history" / "00000000000000000001.json"
    history_path.write_bytes(canonical_json_bytes(next_record.payload()))

    recovered = authority.current()

    assert recovered == next_record
    current_payload = json.loads((tmp_path / "current.json").read_text())
    assert current_payload["generation"] == 1
    assert current_payload["record_sha256"] == next_record.record_sha256


def test_multiple_uncommitted_successors_fail_closed_as_ambiguous(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    second = PersistentTrustStateRecord.build(
        generation=1,
        previous_record_sha256=initial.record_sha256,
        trust_state=_next_state(),
        transition=_transition("b", "4" * 64),
    )
    third = PersistentTrustStateRecord.build(
        generation=2,
        previous_record_sha256=second.record_sha256,
        trust_state=_third_state(),
        transition=_transition("c", "7" * 64),
    )
    (tmp_path / "history" / "00000000000000000001.json").write_bytes(
        canonical_json_bytes(second.payload())
    )
    (tmp_path / "history" / "00000000000000000002.json").write_bytes(
        canonical_json_bytes(third.payload())
    )

    with pytest.raises(PersistentTrustStateCorruption, match="ambiguous"):
        authority.current()


def test_retained_newer_history_repairs_one_generation_current_rollback(
    tmp_path: Path,
) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    current, _ = authority.advance(
        next_state=_next_state(),
        transition=_transition("b", "4" * 64),
        expected_generation=initial.generation,
        expected_record_sha256=initial.record_sha256,
        expected_trust_state_sha256=initial.trust_state_sha256,
    )
    rollback_pointer = canonical_json_bytes(
        {
            "schema_version": "astra-persistent-trust-state-current-v1",
            "generation": initial.generation,
            "record_sha256": initial.record_sha256,
        }
    )
    (tmp_path / "current.json").write_bytes(rollback_pointer)

    recovered = authority.current()

    assert recovered == current


def test_malformed_history_fails_closed(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    path = tmp_path / "history" / "00000000000000000000.json"
    path.write_text('{"not":"canonical", "spacing":"changed"}')

    with pytest.raises(PersistentTrustStateCorruption):
        authority.current()


@pytest.mark.skipif(os.name == "nt", reason="multiprocessing lock regression runs on POSIX CI")
def test_two_processes_cannot_commit_same_generation(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    arguments = (
        str(tmp_path),
        initial.generation,
        initial.record_sha256,
        initial.trust_state_sha256,
        queue,
    )
    first = context.Process(target=_advance_worker, args=arguments)
    second = context.Process(target=_advance_worker, args=arguments)
    first.start()
    second.start()
    first.join(timeout=20)
    second.join(timeout=20)

    assert first.exitcode == 0
    assert second.exitcode == 0
    outcomes = sorted([queue.get(timeout=5)[0], queue.get(timeout=5)[0]])
    assert outcomes == ["cas", "ok"]
    assert authority.current().generation == 1


def test_receipt_is_deterministic_for_committed_transition(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    current, receipt = authority.advance(
        next_state=_next_state(),
        transition=_transition("b", "4" * 64),
        expected_generation=initial.generation,
        expected_record_sha256=initial.record_sha256,
        expected_trust_state_sha256=initial.trust_state_sha256,
    )

    first = canonical_json_bytes(receipt.payload())
    second = canonical_json_bytes(receipt.payload())

    assert first == second
    assert receipt.current_record_sha256 == current.record_sha256


def test_initialize_twice_is_rejected(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )

    with pytest.raises(PersistentTrustStateAuthorityError, match="already initialized"):
        authority.initialize(
            initial_state=_initial_state(),
            transition=_transition("a", "0" * 64),
        )


def test_current_before_initialize_is_rejected(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)

    with pytest.raises(PersistentTrustStateAuthorityError, match="not initialized"):
        authority.current()


@pytest.mark.parametrize(
    ("record_sha", "state_sha", "message"),
    [
        ("f" * 64, None, "record SHA CAS mismatch"),
        (None, "f" * 64, "payload SHA CAS mismatch"),
    ],
)
def test_hash_cas_mismatch_rejected_without_mutation(
    tmp_path: Path,
    record_sha: str | None,
    state_sha: str | None,
    message: str,
) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    current_before = (tmp_path / "current.json").read_bytes()

    with pytest.raises(PersistentTrustStateCASMismatch, match=message):
        authority.advance(
            next_state=_next_state(),
            transition=_transition("b", "4" * 64),
            expected_generation=initial.generation,
            expected_record_sha256=record_sha or initial.record_sha256,
            expected_trust_state_sha256=state_sha or initial.trust_state_sha256,
        )

    assert (tmp_path / "current.json").read_bytes() == current_before
    assert authority.current() == initial


def test_noop_advancement_is_rejected(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )

    with pytest.raises(PersistentTrustStateAuthorityError, match="no-op"):
        authority.advance(
            next_state=_initial_state(),
            transition=_transition("a", "0" * 64),
            expected_generation=initial.generation,
            expected_record_sha256=initial.record_sha256,
            expected_trust_state_sha256=initial.trust_state_sha256,
        )


def test_transition_checkpoint_mismatch_is_rejected(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )

    with pytest.raises(
        PersistentTrustStateAuthorityError,
        match="checkpoint SHA does not match",
    ):
        authority.advance(
            next_state=_next_state(),
            transition=_transition("b", "9" * 64),
            expected_generation=initial.generation,
            expected_record_sha256=initial.record_sha256,
            expected_trust_state_sha256=initial.trust_state_sha256,
        )


def test_profile_head_cannot_change_without_count_advance(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    invalid = _state(
        profile_count=0,
        profile_head="2" * 64,
        tree_size=2,
        tree_root="3" * 64,
        checkpoint="4" * 64,
    )

    with pytest.raises(PersistentTrustStateAuthorityError, match="profile head changed"):
        authority.advance(
            next_state=invalid,
            transition=_transition("b", "4" * 64),
            expected_generation=initial.generation,
            expected_record_sha256=initial.record_sha256,
            expected_trust_state_sha256=initial.trust_state_sha256,
        )


def test_transparency_root_cannot_change_without_size_advance(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    invalid = _state(
        profile_count=1,
        profile_head="2" * 64,
        tree_size=1,
        tree_root="3" * 64,
        checkpoint="4" * 64,
    )

    with pytest.raises(
        PersistentTrustStateAuthorityError,
        match="transparency root changed",
    ):
        authority.advance(
            next_state=invalid,
            transition=_transition("b", "4" * 64),
            expected_generation=initial.generation,
            expected_record_sha256=initial.record_sha256,
            expected_trust_state_sha256=initial.trust_state_sha256,
        )


def test_missing_current_pointer_at_genesis_recovers(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    (tmp_path / "current.json").unlink()

    recovered = authority.current()

    assert recovered == initial
    assert (tmp_path / "current.json").is_file()


def test_missing_current_pointer_with_non_genesis_history_fails_closed(
    tmp_path: Path,
) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    authority.advance(
        next_state=_next_state(),
        transition=_transition("b", "4" * 64),
        expected_generation=initial.generation,
        expected_record_sha256=initial.record_sha256,
        expected_trust_state_sha256=initial.trust_state_sha256,
    )
    (tmp_path / "current.json").unlink()

    with pytest.raises(PersistentTrustStateCorruption, match="current TrustState pointer is missing"):
        authority.current()


def test_history_generation_gap_fails_closed(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    record = PersistentTrustStateRecord.build(
        generation=2,
        previous_record_sha256=initial.record_sha256,
        trust_state=_next_state(),
        transition=_transition("b", "4" * 64),
    )
    (tmp_path / "history" / "00000000000000000002.json").write_bytes(
        canonical_json_bytes(record.payload())
    )

    with pytest.raises(PersistentTrustStateCorruption, match="generation gap"):
        authority.current()


def test_history_parent_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    record = PersistentTrustStateRecord.build(
        generation=1,
        previous_record_sha256="f" * 64,
        trust_state=_next_state(),
        transition=_transition("b", "4" * 64),
    )
    (tmp_path / "history" / "00000000000000000001.json").write_bytes(
        canonical_json_bytes(record.payload())
    )

    with pytest.raises(PersistentTrustStateCorruption, match="hash chain mismatch"):
        authority.current()


def test_current_pointer_unknown_generation_fails_closed(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    (tmp_path / "current.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "astra-persistent-trust-state-current-v1",
                "generation": 9,
                "record_sha256": "f" * 64,
            }
        )
    )

    with pytest.raises(PersistentTrustStateCorruption, match="missing TrustState history record"):
        authority.current()


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\xef\xbb\xbf{}",
        b'{"a":1,"a":2}',
        b'{"value":1.5}',
        b'{"value":NaN}',
        b'{ "not":"canonical" }',
    ],
)
def test_strict_json_rejects_noncanonical_or_ambiguous_input(payload: bytes) -> None:
    with pytest.raises(PersistentTrustStateCorruption):
        authority_module._strict_json(payload, source="test")


def test_tampered_receipt_digest_is_rejected(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a", "0" * 64),
    )
    _, receipt = authority.advance(
        next_state=_next_state(),
        transition=_transition("b", "4" * 64),
        expected_generation=initial.generation,
        expected_record_sha256=initial.record_sha256,
        expected_trust_state_sha256=initial.trust_state_sha256,
    )
    tampered = authority_module.TrustStateAdvanceReceipt(
        previous_generation=receipt.previous_generation,
        current_generation=receipt.current_generation,
        previous_record_sha256=receipt.previous_record_sha256,
        current_record_sha256=receipt.current_record_sha256,
        previous_trust_state_sha256=receipt.previous_trust_state_sha256,
        current_trust_state_sha256=receipt.current_trust_state_sha256,
        artifact_id=receipt.artifact_id,
        artifact_sha256=receipt.artifact_sha256,
        bundle_id=receipt.bundle_id,
        bundle_sha256=receipt.bundle_sha256,
        checkpoint_v4_id=receipt.checkpoint_v4_id,
        checkpoint_v4_sha256=receipt.checkpoint_v4_sha256,
        verified_at=receipt.verified_at,
        receipt_sha256="f" * 64,
    )

    with pytest.raises(ValueError, match="receipt digest mismatch"):
        tampered.payload()
