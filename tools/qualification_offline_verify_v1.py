from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from app.qualification.portable_artifact_codec import (
    DecodedQualificationPortableArtifact,
    QualificationArtifactCodecError,
    canonical_json_bytes,
    decode_portable_qualification_artifact_json,
)
from app.qualification.portable_bundle_decoder_v4 import (
    QualificationBundleDecodeError,
    decode_typed_portable_qualification_bundle_v4,
)
from app.qualification.verification_service_v4 import (
    QualificationTrustStateV4,
    QualificationVerificationOutcomeV4,
    QualificationVerificationRequestV4,
    QualificationVerificationServiceV4,
)

_ROOTS_SCHEMA = "astra-qualification-trusted-roots-v1"
_STATE_FILE_SCHEMA = "astra-qualification-trust-state-file-v1"
_RESULT_SCHEMA = "astra-qualification-offline-verifier-result-v1"
_CLI_VERSION = "1.0.0"

EXIT_VERIFIED_USABLE = 0
EXIT_VERIFICATION_NOT_USABLE = 2
EXIT_INPUT_ERROR = 3


class OfflineQualificationVerifierInputError(ValueError):
    pass


@dataclass(frozen=True)
class OfflineVerificationInputs:
    artifact: DecodedQualificationPortableArtifact
    trusted_roots: Mapping[str, bytes]
    trusted_state: QualificationTrustStateV4
    observed_at: datetime
    max_clock_skew_seconds: int
    bootstrap_used: bool


def run_offline_verification(
    *,
    artifact_bytes: bytes,
    trusted_roots_bytes: bytes,
    state_bytes: bytes | None,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
    allow_genesis_bootstrap: bool = False,
) -> tuple[dict[str, object], QualificationTrustStateV4 | None, int]:
    inputs = _prepare_inputs(
        artifact_bytes=artifact_bytes,
        trusted_roots_bytes=trusted_roots_bytes,
        state_bytes=state_bytes,
        observed_at=observed_at,
        max_clock_skew_seconds=max_clock_skew_seconds,
        allow_genesis_bootstrap=allow_genesis_bootstrap,
    )
    bundle = decode_typed_portable_qualification_bundle_v4(inputs.artifact)
    service = QualificationVerificationServiceV4(
        trusted_root_public_keys=inputs.trusted_roots,
        max_clock_skew_seconds=inputs.max_clock_skew_seconds,
    )
    result = service.verify(
        QualificationVerificationRequestV4(
            bundle=bundle,
            previous_keyring_generation=inputs.artifact.previous_keyring_generation,
            trusted_state=inputs.trusted_state,
            observed_at=inputs.observed_at,
        )
    )
    payload = {
        "schema_version": _RESULT_SCHEMA,
        "cli_version": _CLI_VERSION,
        "artifact_id": inputs.artifact.artifact_id,
        "artifact_sha256": inputs.artifact.artifact_sha256,
        "bootstrap_used": inputs.bootstrap_used,
        **result.payload(),
    }
    next_state = (
        result.next_trust_state
        if result.outcome is QualificationVerificationOutcomeV4.VERIFIED
        else None
    )
    exit_code = (
        EXIT_VERIFIED_USABLE
        if result.outcome is QualificationVerificationOutcomeV4.VERIFIED
        and result.usable
        else EXIT_VERIFICATION_NOT_USABLE
    )
    return payload, next_state, exit_code


def encode_trust_state_file(state: QualificationTrustStateV4) -> bytes:
    state.validate()
    return canonical_json_bytes(
        {
            "schema_version": _STATE_FILE_SCHEMA,
            "state": state.payload(),
        }
    )


def decode_trust_state_file(encoded: bytes) -> QualificationTrustStateV4:
    raw = _decode_strict_json_object(encoded, source="trust state")
    _exact_fields(raw, {"schema_version", "state"}, "$")
    if raw["schema_version"] != _STATE_FILE_SCHEMA:
        raise OfflineQualificationVerifierInputError("trust state schema mismatch")
    state_payload = _object(raw["state"], "$.state")
    _exact_fields(
        state_payload,
        {
            "profile_event_count",
            "profile_event_head_sha256",
            "transparency_tree_size",
            "transparency_root_sha256",
            "checkpoint_v4_sha256",
        },
        "$.state",
    )
    state = QualificationTrustStateV4(
        profile_event_count=_integer(
            state_payload["profile_event_count"],
            "$.state.profile_event_count",
        ),
        profile_event_head_sha256=_string(
            state_payload["profile_event_head_sha256"],
            "$.state.profile_event_head_sha256",
        ),
        transparency_tree_size=_integer(
            state_payload["transparency_tree_size"],
            "$.state.transparency_tree_size",
        ),
        transparency_root_sha256=_string(
            state_payload["transparency_root_sha256"],
            "$.state.transparency_root_sha256",
        ),
        checkpoint_v4_sha256=_string(
            state_payload["checkpoint_v4_sha256"],
            "$.state.checkpoint_v4_sha256",
        ),
    )
    try:
        state.validate()
    except ValueError as exc:
        raise OfflineQualificationVerifierInputError(
            f"trust state validation failed: {exc}"
        ) from exc
    return state


def decode_trusted_roots_file(encoded: bytes) -> Mapping[str, bytes]:
    raw = _decode_strict_json_object(encoded, source="trusted roots")
    _exact_fields(raw, {"schema_version", "roots"}, "$")
    if raw["schema_version"] != _ROOTS_SCHEMA:
        raise OfflineQualificationVerifierInputError("trusted roots schema mismatch")
    roots_raw = _array(raw["roots"], "$.roots")
    if not roots_raw:
        raise OfflineQualificationVerifierInputError("trusted roots cannot be empty")
    roots: dict[str, bytes] = {}
    for index, item in enumerate(roots_raw):
        path = f"$.roots[{index}]"
        entry = _object(item, path)
        _exact_fields(entry, {"key_id", "public_key_b64"}, path)
        key_id = _string(entry["key_id"], f"{path}.key_id")
        if not key_id.strip():
            raise OfflineQualificationVerifierInputError(
                f"{path}.key_id cannot be blank"
            )
        if key_id in roots:
            raise OfflineQualificationVerifierInputError(
                f"duplicate trusted root key_id: {key_id}"
            )
        public_key_b64 = _string(
            entry["public_key_b64"],
            f"{path}.public_key_b64",
        )
        try:
            public_key = base64.b64decode(
                public_key_b64.encode("ascii"),
                validate=True,
            )
        except (ValueError, UnicodeEncodeError) as exc:
            raise OfflineQualificationVerifierInputError(
                f"{path}.public_key_b64 is not valid base64"
            ) from exc
        if len(public_key) != 32:
            raise OfflineQualificationVerifierInputError(
                f"{path}.public_key_b64 must decode to 32 bytes"
            )
        roots[key_id] = public_key
    return roots


def atomic_write_trust_state(path: Path, state: QualificationTrustStateV4) -> None:
    encoded = encode_trust_state_file(state)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=parent,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="astra-qualification-verify-offline",
        description="Offline fail-closed verifier for ASTRA qualification artefacts.",
    )
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--trusted-roots", required=True, type=Path)
    parser.add_argument("--state-in", type=Path)
    parser.add_argument("--state-out", type=Path)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--max-clock-skew-seconds", type=int, default=5)
    parser.add_argument("--allow-genesis-bootstrap", action="store_true")
    args = parser.parse_args(argv)

    try:
        observed_at = _parse_datetime(args.observed_at, "--observed-at")
        artifact_bytes = args.artifact.read_bytes()
        roots_bytes = args.trusted_roots.read_bytes()
        state_bytes = None if args.state_in is None else args.state_in.read_bytes()
        result_payload, next_state, exit_code = run_offline_verification(
            artifact_bytes=artifact_bytes,
            trusted_roots_bytes=roots_bytes,
            state_bytes=state_bytes,
            observed_at=observed_at,
            max_clock_skew_seconds=args.max_clock_skew_seconds,
            allow_genesis_bootstrap=args.allow_genesis_bootstrap,
        )
        if args.state_out is not None and next_state is not None:
            atomic_write_trust_state(args.state_out, next_state)
        print(canonical_json_bytes(result_payload).decode("utf-8"))
        return exit_code
    except (
        OSError,
        QualificationArtifactCodecError,
        QualificationBundleDecodeError,
        OfflineQualificationVerifierInputError,
        ValueError,
    ) as exc:
        print(
            canonical_json_bytes(
                {
                    "schema_version": _RESULT_SCHEMA,
                    "cli_version": _CLI_VERSION,
                    "outcome": "INPUT_ERROR",
                    "usable": False,
                    "failure_code": "INPUT_ERROR",
                    "failure_detail": str(exc),
                }
            ).decode("utf-8")
        )
        return EXIT_INPUT_ERROR


def _prepare_inputs(
    *,
    artifact_bytes: bytes,
    trusted_roots_bytes: bytes,
    state_bytes: bytes | None,
    observed_at: datetime,
    max_clock_skew_seconds: int,
    allow_genesis_bootstrap: bool,
) -> OfflineVerificationInputs:
    if max_clock_skew_seconds < 0:
        raise OfflineQualificationVerifierInputError(
            "max_clock_skew_seconds must be non-negative"
        )
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise OfflineQualificationVerifierInputError(
            "observed_at must be timezone-aware"
        )

    artifact = decode_portable_qualification_artifact_json(artifact_bytes)
    roots = decode_trusted_roots_file(trusted_roots_bytes)

    if state_bytes is not None:
        trusted_state = decode_trust_state_file(state_bytes)
        if canonical_json_bytes(trusted_state.payload()) != canonical_json_bytes(
            artifact.trusted_state.payload()
        ):
            raise OfflineQualificationVerifierInputError(
                "local trusted state does not match artifact embedded trusted state"
            )
        bootstrap_used = False
    else:
        if not allow_genesis_bootstrap:
            raise OfflineQualificationVerifierInputError(
                "state-in is required unless --allow-genesis-bootstrap is explicit"
            )
        typed_bundle = decode_typed_portable_qualification_bundle_v4(artifact)
        trusted_state = artifact.trusted_state
        _require_genesis_bootstrap_state(
            trusted_state,
            base_transparency_tree_size=typed_bundle.base_v3.transparency_head.tree_size,
            base_transparency_root_sha256=(
                typed_bundle.base_v3.transparency_head.root_sha256
            ),
        )
        bootstrap_used = True

    return OfflineVerificationInputs(
        artifact=artifact,
        trusted_roots=roots,
        trusted_state=trusted_state,
        observed_at=observed_at,
        max_clock_skew_seconds=max_clock_skew_seconds,
        bootstrap_used=bootstrap_used,
    )


def _require_genesis_bootstrap_state(
    state: QualificationTrustStateV4,
    *,
    base_transparency_tree_size: int,
    base_transparency_root_sha256: str,
) -> None:
    state.validate()
    if state.profile_event_count != 0:
        raise OfflineQualificationVerifierInputError(
            "genesis bootstrap requires zero profile event count"
        )
    if state.profile_event_head_sha256 != "0" * 64:
        raise OfflineQualificationVerifierInputError(
            "genesis bootstrap requires genesis profile event head"
        )
    if state.checkpoint_v4_sha256 != "0" * 64:
        raise OfflineQualificationVerifierInputError(
            "genesis bootstrap requires genesis checkpoint v4 sha"
        )
    if (
        state.transparency_tree_size != base_transparency_tree_size
        or state.transparency_root_sha256 != base_transparency_root_sha256
    ):
        raise OfflineQualificationVerifierInputError(
            "genesis bootstrap transparency anchor must match base v3 head"
        )


def _parse_datetime(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise OfflineQualificationVerifierInputError(
            f"{name} must be an ISO-8601 datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OfflineQualificationVerifierInputError(
            f"{name} must be timezone-aware"
        )
    return parsed


def _decode_strict_json_object(encoded: bytes, *, source: str) -> dict[str, object]:
    if not encoded:
        raise OfflineQualificationVerifierInputError(f"{source} file is empty")
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise OfflineQualificationVerifierInputError(
            f"{source} UTF-8 BOM is not allowed"
        )
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise OfflineQualificationVerifierInputError(
            f"{source} is not valid UTF-8"
        ) from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except OfflineQualificationVerifierInputError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise OfflineQualificationVerifierInputError(
            f"{source} JSON is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise OfflineQualificationVerifierInputError(
            f"{source} root must be an object"
        )
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OfflineQualificationVerifierInputError(
                f"duplicate JSON object key: {key}"
            )
        result[key] = value
    return result


def _reject_float(value: str) -> NoReturn:
    raise OfflineQualificationVerifierInputError(
        f"floating-point JSON number is forbidden: {value}"
    )


def _reject_constant(value: str) -> NoReturn:
    raise OfflineQualificationVerifierInputError(
        f"non-finite JSON number is forbidden: {value}"
    )


def _exact_fields(
    value: Mapping[str, object],
    expected: set[str],
    path: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise OfflineQualificationVerifierInputError(
            f"{path} fields mismatch; "
            f"missing={sorted(expected - actual)}; "
            f"unknown={sorted(actual - expected)}"
        )


def _object(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OfflineQualificationVerifierInputError(f"{path} must be an object")
    return value


def _array(value: object, path: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise OfflineQualificationVerifierInputError(f"{path} must be an array")
    return tuple(value)


def _string(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise OfflineQualificationVerifierInputError(f"{path} must be a string")
    return value


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OfflineQualificationVerifierInputError(f"{path} must be an integer")
    return value


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


if __name__ == "__main__":
    raise SystemExit(main())
