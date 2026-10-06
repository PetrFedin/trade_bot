from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.qualification.transparency_log import (
    QualificationTransparencyEntry,
    QualificationTransparencyLog,
    verify_delta_consistency,
    verify_inclusion_proof,
)

NOW = datetime(2026, 10, 6, 13, 0, tzinfo=UTC)


def entry(index: int) -> QualificationTransparencyEntry:
    value = QualificationTransparencyEntry(
        entry_type="SIGNED_QUALIFICATION_EVIDENCE",
        object_id=f"qevidence_{index:024d}",
        object_sha256=f"{index + 1:064x}",
        subject="BYBIT_PUBLIC_MARKETDATA_ADAPTER",
        subject_version=f"build-{index}",
        profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
        profile_version="1.0.0",
        published_at=NOW + timedelta(seconds=index),
    )
    value.validate()
    return value


def populated_log(count: int) -> QualificationTransparencyLog:
    log = QualificationTransparencyLog()
    for index in range(count):
        log.append(
            entry=entry(index),
            issued_at=NOW + timedelta(seconds=index, milliseconds=1),
        )
    return log


def test_transparency_log_builds_chained_tree_heads() -> None:
    log = populated_log(3)
    entries = log.entries()
    head = log.latest_head()

    assert len(entries) == 3
    assert head.tree_size == 3
    assert len(head.root_sha256) == 64
    assert head.tree_head_id.startswith("qtree_")
    assert head.previous_tree_head_sha256 != "0" * 64


def test_inclusion_proof_verifies_every_leaf() -> None:
    log = populated_log(7)

    for item in log.entries():
        proof = log.inclusion_proof(object_id=item.object_id)
        assert proof.leaf_sha256 == item.leaf_sha256
        assert verify_inclusion_proof(proof)


def test_inclusion_proof_rejects_tampered_path() -> None:
    log = populated_log(4)
    proof = log.inclusion_proof(object_id=entry(1).object_id)
    assert proof.audit_path

    tampered = replace(
        proof,
        audit_path=("f" * 64, *proof.audit_path[1:]),
    )

    assert not verify_inclusion_proof(tampered)


def test_delta_consistency_proves_append_only_growth() -> None:
    log = populated_log(5)

    proof = log.consistency_proof(previous_tree_size=2)

    assert proof.previous_tree_size == 2
    assert proof.current_tree_size == 5
    assert len(proof.previous_leaf_hashes) == 2
    assert len(proof.appended_leaf_hashes) == 3
    assert verify_delta_consistency(proof)


def test_delta_consistency_rejects_tampered_append() -> None:
    log = populated_log(5)
    proof = log.consistency_proof(previous_tree_size=2)
    tampered = replace(
        proof,
        appended_leaf_hashes=("f" * 64, *proof.appended_leaf_hashes[1:]),
    )

    assert not verify_delta_consistency(tampered)


def test_duplicate_transparency_object_is_rejected() -> None:
    log = QualificationTransparencyLog()
    value = entry(0)
    log.append(entry=value, issued_at=NOW + timedelta(milliseconds=1))

    with pytest.raises(ValueError, match="already published"):
        log.append(
            entry=value,
            issued_at=NOW + timedelta(seconds=1),
        )


def test_transparency_head_cannot_predate_entry() -> None:
    log = QualificationTransparencyLog()

    with pytest.raises(ValueError, match="cannot predate"):
        log.append(
            entry=entry(1),
            issued_at=NOW,
        )


def test_empty_transparency_log_has_genesis_head() -> None:
    log = QualificationTransparencyLog()
    head = log.latest_head()

    assert head.tree_size == 0
    assert head.root_sha256 == "0" * 64
    assert head.previous_tree_head_sha256 == "0" * 64


def test_consistency_proof_rejects_out_of_range_prefix() -> None:
    log = populated_log(2)

    with pytest.raises(ValueError, match="out of range"):
        log.consistency_proof(previous_tree_size=3)
