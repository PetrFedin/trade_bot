from __future__ import annotations

import json
import multiprocessing
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
            transition=_transition("b"),
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
        transition=_transition("a"),
    )

    current, receipt = authority.advance(
        next_state=_next_state(),
        transition=_transition("b"),
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
        transition=_transition("a"),
    )
    current, _ = authority.advance(
        next_state=_next_state(),
        transition=_transition("b"),
        expected_generation=0,
        expected_record_sha256=initial.record_sha256,
        expected_trust_state_sha256=initial.trust_state_sha256,
    )
    before = (tmp_path / "current.json").read_bytes()
    history_before = sorted(path.name for path in (tmp_path / "history").glob("*.json"))

    with pytest.raises(PersistentTrustStateCASMismatch):
        authority.advance(
            next_state=_third_state(),
            transition=_transition("c"),
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
        transition=_transition("a"),
    )
    current, _ = authority.advance(
        next_state=_next_state(),
        transition=_transition("b"),
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
            transition=_transition("c"),
            expected_generation=current.generation,
            expected_record_sha256=current.record_sha256,
            expected_trust_state_sha256=current.trust_state_sha256,
        )

    assert authority.current() == current


def test_crash_after_history_commit_before_current_update_recovers(tmp_path: Path) -> None:
    authority = PersistentTrustStateAuthorityV1(tmp_path)
    initial = authority.initialize(
        initial_state=_initial_state(),
        transition=_transition("a"),
    )
    next_record = PersistentTrustStateRecord.build(
        generation=1,
        previous_record_sha256=initial.record_sha256,
        trust_state=_next_state(),
        transition=_transition("b"),
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
        transition=_transition("a"),
    )
    second = PersistentTrustStateRecord.build(
        generation=1,
        previous_record_sha256=initial.record_sha256,
        trust_state=_next_state(),
        transition=_transition("b"),
    )
    third = PersistentTrustStateRecord.build(
        generation=2,
        previous_record_sha256=second.record_sha256,
        trust_state=_third_state(),
        transition=_transition("c"),
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
        transition=_transition("a"),
    )
    current, _ = authority.advance(
        next_state=_next_state(),
        transition=_transition("b"),
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
        transition=_transition("a"),
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
        transition=_transition("a"),
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
        transition=_transition("a"),
    )
    current, receipt = authority.advance(
        next_state=_next_state(),
        transition=_transition("b"),
        expected_generation=initial.generation,
        expected_record_sha256=initial.record_sha256,
        expected_trust_state_sha256=initial.trust_state_sha256,
    )

    first = canonical_json_bytes(receipt.payload())
    second = canonical_json_bytes(receipt.payload())

    assert first == second
    assert receipt.current_record_sha256 == current.record_sha256
