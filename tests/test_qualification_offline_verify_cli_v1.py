from __future__ import annotations

import base64
from datetime import timedelta

from app.qualification.persistent_trust_state_authority_v1 import (
    PersistentTrustStateAuthorityV1,
    PersistentTrustStateCASMismatch,
)
from app.qualification.portable_artifact_codec import (
    canonical_json_bytes,
    encode_portable_qualification_artifact_json,
)
from app.qualification.verification_service_v4 import QualificationTrustStateV4
from tests.helpers_v108 import NOW
from tests.test_qualification_portable_verification_v4 import bundle_v4
from tools.qualification_offline_verify_v1 import (
    EXIT_AUTHORITY_CAS_CONFLICT,
    EXIT_INPUT_ERROR,
    EXIT_VERIFICATION_NOT_USABLE,
    EXIT_VERIFIED_USABLE,
    decode_trust_state_file,
    encode_trust_state_file,
    initialize_authority_from_artifact,
    main,
    run_authority_verification,
    run_offline_verification,
)


def _genesis_state(bundle) -> QualificationTrustStateV4:
    return QualificationTrustStateV4(
        profile_event_count=0,
        profile_event_head_sha256="0" * 64,
        transparency_tree_size=bundle.base_v3.transparency_head.tree_size,
        transparency_root_sha256=bundle.base_v3.transparency_head.root_sha256,
        checkpoint_v4_sha256="0" * 64,
    )


def _fixture():
    bundle, root = bundle_v4()
    state = _genesis_state(bundle)
    artifact = encode_portable_qualification_artifact_json(
        bundle=bundle,
        trusted_state=state,
        previous_keyring_generation=0,
    )
    roots = canonical_json_bytes(
        {
            "schema_version": "astra-qualification-trusted-roots-v1",
            "roots": [
                {
                    "key_id": root.key_id,
                    "public_key_b64": base64.b64encode(
                        root.public_key_bytes()
                    ).decode("ascii"),
                }
            ],
        }
    )
    return bundle, state, artifact, roots


def test_offline_verifier_valid_artifact_is_verified_and_usable() -> None:
    _, state, artifact, roots = _fixture()

    payload, next_state, exit_code = run_offline_verification(
        artifact_bytes=artifact,
        trusted_roots_bytes=roots,
        state_bytes=encode_trust_state_file(state),
        observed_at=NOW + timedelta(seconds=11),
    )

    assert exit_code == EXIT_VERIFIED_USABLE
    assert payload["outcome"] == "VERIFIED"
    assert payload["usable"] is True
    assert payload["bootstrap_used"] is False
    assert next_state is not None
    assert next_state.profile_event_count > state.profile_event_count
    assert next_state.checkpoint_v4_sha256 != state.checkpoint_v4_sha256


def test_offline_verifier_explicit_genesis_bootstrap_is_allowed() -> None:
    _, _, artifact, roots = _fixture()

    payload, next_state, exit_code = run_offline_verification(
        artifact_bytes=artifact,
        trusted_roots_bytes=roots,
        state_bytes=None,
        observed_at=NOW + timedelta(seconds=11),
        allow_genesis_bootstrap=True,
    )

    assert exit_code == EXIT_VERIFIED_USABLE
    assert payload["bootstrap_used"] is True
    assert next_state is not None


def test_offline_verifier_missing_state_without_bootstrap_is_input_error(
    tmp_path,
    capsys,
) -> None:
    _, _, artifact, roots = _fixture()
    artifact_path = tmp_path / "artifact.json"
    roots_path = tmp_path / "roots.json"
    artifact_path.write_bytes(artifact)
    roots_path.write_bytes(roots)

    exit_code = main(
        [
            "--artifact",
            str(artifact_path),
            "--trusted-roots",
            str(roots_path),
            "--observed-at",
            (NOW + timedelta(seconds=11)).isoformat(),
        ]
    )

    assert exit_code == EXIT_INPUT_ERROR
    output = capsys.readouterr().out
    assert '"outcome":"INPUT_ERROR"' in output
    assert "state-in is required" in output


def test_offline_verifier_rejects_local_state_mismatch() -> None:
    _, state, artifact, roots = _fixture()
    mismatched = QualificationTrustStateV4(
        profile_event_count=state.profile_event_count,
        profile_event_head_sha256=state.profile_event_head_sha256,
        transparency_tree_size=state.transparency_tree_size,
        transparency_root_sha256=state.transparency_root_sha256,
        checkpoint_v4_sha256="f" * 64,
    )

    try:
        run_offline_verification(
            artifact_bytes=artifact,
            trusted_roots_bytes=roots,
            state_bytes=encode_trust_state_file(mismatched),
            observed_at=NOW + timedelta(seconds=11),
        )
    except ValueError as exc:
        assert "does not match artifact embedded trusted state" in str(exc)
    else:
        raise AssertionError("state mismatch must fail closed")


def test_offline_verifier_wrong_but_well_formed_root_is_rejected() -> None:
    _, state, artifact, _ = _fixture()
    wrong_roots = canonical_json_bytes(
        {
            "schema_version": "astra-qualification-trusted-roots-v1",
            "roots": [
                {
                    "key_id": "wrong-root",
                    "public_key_b64": base64.b64encode(b"x" * 32).decode("ascii"),
                }
            ],
        }
    )

    payload, next_state, exit_code = run_offline_verification(
        artifact_bytes=artifact,
        trusted_roots_bytes=wrong_roots,
        state_bytes=encode_trust_state_file(state),
        observed_at=NOW + timedelta(seconds=11),
    )

    assert exit_code == EXIT_VERIFICATION_NOT_USABLE
    assert payload["outcome"] == "REJECTED"
    assert next_state is None


def test_rejected_cli_does_not_modify_existing_state_out(
    tmp_path,
    capsys,
) -> None:
    _, state, artifact, _ = _fixture()
    wrong_roots = canonical_json_bytes(
        {
            "schema_version": "astra-qualification-trusted-roots-v1",
            "roots": [
                {
                    "key_id": "wrong-root",
                    "public_key_b64": base64.b64encode(b"x" * 32).decode("ascii"),
                }
            ],
        }
    )
    artifact_path = tmp_path / "artifact.json"
    roots_path = tmp_path / "roots.json"
    state_in = tmp_path / "state-in.json"
    state_out = tmp_path / "state-out.json"
    artifact_path.write_bytes(artifact)
    roots_path.write_bytes(wrong_roots)
    state_in.write_bytes(encode_trust_state_file(state))
    sentinel = b'{"must":"remain byte-for-byte unchanged"}'
    state_out.write_bytes(sentinel)

    exit_code = main(
        [
            "--artifact",
            str(artifact_path),
            "--trusted-roots",
            str(roots_path),
            "--state-in",
            str(state_in),
            "--state-out",
            str(state_out),
            "--observed-at",
            (NOW + timedelta(seconds=11)).isoformat(),
        ]
    )

    assert exit_code == EXIT_VERIFICATION_NOT_USABLE
    assert state_out.read_bytes() == sentinel
    assert '"outcome":"REJECTED"' in capsys.readouterr().out


def test_verified_cli_atomically_writes_next_state(
    tmp_path,
    capsys,
) -> None:
    _, state, artifact, roots = _fixture()
    artifact_path = tmp_path / "artifact.json"
    roots_path = tmp_path / "roots.json"
    state_in = tmp_path / "state-in.json"
    state_out = tmp_path / "state-out.json"
    artifact_path.write_bytes(artifact)
    roots_path.write_bytes(roots)
    state_in.write_bytes(encode_trust_state_file(state))

    exit_code = main(
        [
            "--artifact",
            str(artifact_path),
            "--trusted-roots",
            str(roots_path),
            "--state-in",
            str(state_in),
            "--state-out",
            str(state_out),
            "--observed-at",
            (NOW + timedelta(seconds=11)).isoformat(),
        ]
    )

    assert exit_code == EXIT_VERIFIED_USABLE
    next_state = decode_trust_state_file(state_out.read_bytes())
    assert next_state.profile_event_count > state.profile_event_count
    assert next_state.checkpoint_v4_sha256 != state.checkpoint_v4_sha256
    assert '"outcome":"VERIFIED"' in capsys.readouterr().out


def test_state_file_encoding_is_deterministic() -> None:
    _, state, _, _ = _fixture()

    first = encode_trust_state_file(state)
    second = encode_trust_state_file(state)

    assert first == second
    assert decode_trust_state_file(first) == state


def test_authority_initialize_then_read_only_preserves_generation(tmp_path) -> None:
    _, _, artifact, roots = _fixture()
    authority_dir = tmp_path / "authority"

    payload, exit_code = initialize_authority_from_artifact(
        artifact_bytes=artifact,
        trusted_roots_bytes=roots,
        authority_directory=authority_dir,
        observed_at=NOW + timedelta(seconds=11),
    )

    assert exit_code == EXIT_VERIFIED_USABLE
    assert payload["authority_commit_outcome"] == "INITIALIZED"
    authority = PersistentTrustStateAuthorityV1(authority_dir)
    before = authority.current()

    verify_payload, verify_exit = run_authority_verification(
        artifact_bytes=artifact,
        trusted_roots_bytes=roots,
        authority_directory=authority_dir,
        observed_at=NOW + timedelta(seconds=11),
        advance=False,
    )

    after = authority.current()
    assert verify_exit == EXIT_VERIFIED_USABLE
    assert verify_payload["authority_mode"] == "read-only"
    assert verify_payload["authority_receipt"] is None
    assert before == after


def test_authority_verify_and_advance_commits_one_generation_and_receipt(
    tmp_path,
) -> None:
    _, _, artifact, roots = _fixture()
    authority_dir = tmp_path / "authority"
    initialize_authority_from_artifact(
        artifact_bytes=artifact,
        trusted_roots_bytes=roots,
        authority_directory=authority_dir,
        observed_at=NOW + timedelta(seconds=11),
    )
    authority = PersistentTrustStateAuthorityV1(authority_dir)
    before = authority.current()

    payload, exit_code = run_authority_verification(
        artifact_bytes=artifact,
        trusted_roots_bytes=roots,
        authority_directory=authority_dir,
        observed_at=NOW + timedelta(seconds=11),
        advance=True,
    )

    after = authority.current()
    assert exit_code == EXIT_VERIFIED_USABLE
    assert payload["authority_commit_outcome"] == "COMMITTED"
    assert after.generation == before.generation + 1
    assert payload["authority_generation_before"] == before.generation
    assert payload["authority_generation_after"] == after.generation
    receipt = payload["authority_receipt"]
    assert receipt["previous_record_sha256"] == before.record_sha256
    assert receipt["current_record_sha256"] == after.record_sha256


def test_authority_rejected_verification_does_not_mutate(tmp_path) -> None:
    _, _, artifact, roots = _fixture()
    authority_dir = tmp_path / "authority"
    initialize_authority_from_artifact(
        artifact_bytes=artifact,
        trusted_roots_bytes=roots,
        authority_directory=authority_dir,
        observed_at=NOW + timedelta(seconds=11),
    )
    authority = PersistentTrustStateAuthorityV1(authority_dir)
    before = authority.current()
    wrong_roots = canonical_json_bytes(
        {
            "schema_version": "astra-qualification-trusted-roots-v1",
            "roots": [
                {
                    "key_id": "wrong-root",
                    "public_key_b64": base64.b64encode(b"x" * 32).decode("ascii"),
                }
            ],
        }
    )

    payload, exit_code = run_authority_verification(
        artifact_bytes=artifact,
        trusted_roots_bytes=wrong_roots,
        authority_directory=authority_dir,
        observed_at=NOW + timedelta(seconds=11),
        advance=True,
    )

    assert exit_code == EXIT_VERIFICATION_NOT_USABLE
    assert payload["outcome"] == "REJECTED"
    assert payload["authority_receipt"] is None
    assert authority.current() == before


def test_authority_cas_conflict_is_deterministic(
    tmp_path,
    monkeypatch,
) -> None:
    _, _, artifact, roots = _fixture()
    authority_dir = tmp_path / "authority"
    initialize_authority_from_artifact(
        artifact_bytes=artifact,
        trusted_roots_bytes=roots,
        authority_directory=authority_dir,
        observed_at=NOW + timedelta(seconds=11),
    )

    def conflict(*args, **kwargs):
        raise PersistentTrustStateCASMismatch("TrustState generation CAS mismatch")

    monkeypatch.setattr(PersistentTrustStateAuthorityV1, "advance", conflict)

    payload, exit_code = run_authority_verification(
        artifact_bytes=artifact,
        trusted_roots_bytes=roots,
        authority_directory=authority_dir,
        observed_at=NOW + timedelta(seconds=11),
        advance=True,
    )

    assert exit_code == EXIT_AUTHORITY_CAS_CONFLICT
    assert payload["failure_code"] == "AUTHORITY_CAS_CONFLICT"
    assert payload["authority_commit_outcome"] == "CAS_CONFLICT"
    assert payload["authority_receipt"] is None


def test_cli_rejects_mixed_legacy_and_authority_modes(tmp_path, capsys) -> None:
    _, state, artifact, roots = _fixture()
    artifact_path = tmp_path / "artifact.json"
    roots_path = tmp_path / "roots.json"
    state_path = tmp_path / "state.json"
    authority_dir = tmp_path / "authority"
    artifact_path.write_bytes(artifact)
    roots_path.write_bytes(roots)
    state_path.write_bytes(encode_trust_state_file(state))

    exit_code = main(
        [
            "--artifact",
            str(artifact_path),
            "--trusted-roots",
            str(roots_path),
            "--state-in",
            str(state_path),
            "--authority-dir",
            str(authority_dir),
            "--authority-read-only",
            "--observed-at",
            (NOW + timedelta(seconds=11)).isoformat(),
        ]
    )

    assert exit_code == EXIT_INPUT_ERROR
    assert "cannot be combined" in capsys.readouterr().out
