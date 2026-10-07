from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn, Protocol, runtime_checkable

from app.qualification.portable_artifact_codec import canonical_json_bytes

_CASE_SCHEMA_VERSION = "astra-institutional-test-corpus-case-v1"
_CORPUS_SCHEMA_VERSION = "astra-institutional-test-corpus-v1"
_OBSERVATION_SCHEMA_VERSION = "astra-institutional-test-observation-v1"
_CASE_RESULT_SCHEMA_VERSION = "astra-institutional-test-case-result-v1"
_REPORT_SCHEMA_VERSION = "astra-institutional-test-corpus-report-v1"


class InstitutionalCorpusCaseClass(StrEnum):
    POSITIVE_VERIFICATION = "POSITIVE_VERIFICATION"
    DETERMINISTIC_REJECTION = "DETERMINISTIC_REJECTION"
    ROLLBACK_REPLAY_STALE_STATE = "ROLLBACK_REPLAY_STALE_STATE"
    IDEMPOTENCY_SINGLE_FLIGHT = "IDEMPOTENCY_SINGLE_FLIGHT"
    CAS_CONFLICT = "CAS_CONFLICT"
    SERIALIZATION_REJECTION = "SERIALIZATION_REJECTION"
    TRUSTED_ROOT_SET_MISMATCH = "TRUSTED_ROOT_SET_MISMATCH"
    REFERENCE_PROFILE_CONFORMANCE = "REFERENCE_PROFILE_CONFORMANCE"
    SDK_RETRY_CORRELATION = "SDK_RETRY_CORRELATION"
    TRANSPORT_FRAMING = "TRANSPORT_FRAMING"
    CRASH_RECOVERY = "CRASH_RECOVERY"
    FORWARD_COMPATIBILITY = "FORWARD_COMPATIBILITY"


class InstitutionalCorpusMutationExpectation(StrEnum):
    NONE = "NONE"
    ADVANCE_ONCE = "ADVANCE_ONCE"
    CONFLICT_NO_ADVANCE = "CONFLICT_NO_ADVANCE"
    RECOVERY_NO_SECOND_ADVANCE = "RECOVERY_NO_SECOND_ADVANCE"


class InstitutionalCorpusMismatch(StrEnum):
    RESULT_CLASS_MISMATCH = "RESULT_CLASS_MISMATCH"
    FAILURE_CODE_MISMATCH = "FAILURE_CODE_MISMATCH"
    MUTATION_BEHAVIOR_MISMATCH = "MUTATION_BEHAVIOR_MISMATCH"
    GENERATION_DELTA_MISMATCH = "GENERATION_DELTA_MISMATCH"
    OUTPUT_SHA256_MISMATCH = "OUTPUT_SHA256_MISMATCH"


@dataclass(frozen=True)
class InstitutionalCorpusCaseV1:
    case_class: InstitutionalCorpusCaseClass
    intent: str
    input_b64: str
    expected_result_class: str
    expected_failure_code: str | None
    expected_mutation: InstitutionalCorpusMutationExpectation
    expected_generation_delta: int
    expected_output_sha256: str | None
    profile_ref: str | None
    profile_sha256: str | None
    provenance: str
    schema_version: str = _CASE_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _CASE_SCHEMA_VERSION:
            raise ValueError("institutional corpus case schema mismatch")
        if not self.intent.strip():
            raise ValueError("institutional corpus case intent is required")
        if not self.expected_result_class.strip():
            raise ValueError("institutional corpus expected_result_class is required")
        if self.expected_failure_code is not None and not self.expected_failure_code.strip():
            raise ValueError("institutional corpus expected_failure_code cannot be blank")
        if self.expected_generation_delta < 0:
            raise ValueError("institutional corpus expected_generation_delta must be non-negative")
        if not self.provenance.strip():
            raise ValueError("institutional corpus provenance is required")
        _decode_b64(self.input_b64)
        if self.expected_output_sha256 is not None:
            _digest(self.expected_output_sha256, "expected_output_sha256")
        if (self.profile_ref is None) != (self.profile_sha256 is None):
            raise ValueError(
                "institutional corpus profile_ref/profile_sha256 must be provided together"
            )
        if self.profile_ref is not None:
            if not self.profile_ref.strip():
                raise ValueError("institutional corpus profile_ref cannot be blank")
            _digest(self.profile_sha256 or "", "profile_sha256")

        if (
            self.expected_mutation
            is InstitutionalCorpusMutationExpectation.ADVANCE_ONCE
            and self.expected_generation_delta != 1
        ):
            raise ValueError("ADVANCE_ONCE requires expected_generation_delta=1")
        if self.expected_mutation in {
            InstitutionalCorpusMutationExpectation.NONE,
            InstitutionalCorpusMutationExpectation.CONFLICT_NO_ADVANCE,
            InstitutionalCorpusMutationExpectation.RECOVERY_NO_SECOND_ADVANCE,
        } and self.expected_generation_delta != 0:
            raise ValueError(
                f"{self.expected_mutation.value} requires expected_generation_delta=0"
            )

    @classmethod
    def from_input_bytes(
        cls,
        *,
        case_class: InstitutionalCorpusCaseClass,
        intent: str,
        input_bytes: bytes,
        expected_result_class: str,
        expected_failure_code: str | None,
        expected_mutation: InstitutionalCorpusMutationExpectation,
        expected_generation_delta: int,
        expected_output_sha256: str | None,
        profile_ref: str | None,
        profile_sha256: str | None,
        provenance: str,
    ) -> InstitutionalCorpusCaseV1:
        value = cls(
            case_class=case_class,
            intent=intent,
            input_b64=base64.b64encode(input_bytes).decode("ascii"),
            expected_result_class=expected_result_class,
            expected_failure_code=expected_failure_code,
            expected_mutation=expected_mutation,
            expected_generation_delta=expected_generation_delta,
            expected_output_sha256=expected_output_sha256,
            profile_ref=profile_ref,
            profile_sha256=profile_sha256,
            provenance=provenance,
        )
        value.validate()
        return value

    def input_bytes(self) -> bytes:
        self.validate()
        return _decode_b64(self.input_b64)

    def unsigned_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "case_class": self.case_class.value,
            "intent": self.intent,
            "input_b64": self.input_b64,
            "expected_result_class": self.expected_result_class,
            "expected_failure_code": self.expected_failure_code,
            "expected_mutation": self.expected_mutation.value,
            "expected_generation_delta": self.expected_generation_delta,
            "expected_output_sha256": self.expected_output_sha256,
            "profile_ref": self.profile_ref,
            "profile_sha256": self.profile_sha256,
            "provenance": self.provenance,
        }

    @property
    def case_sha256(self) -> str:
        return _sha256(self.unsigned_payload())

    @property
    def case_id(self) -> str:
        return f"qcorpus_{self.case_sha256[:24]}"

    def payload(self) -> dict[str, object]:
        return {
            **self.unsigned_payload(),
            "case_id": self.case_id,
            "case_sha256": self.case_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.payload())


@dataclass(frozen=True)
class InstitutionalCorpusV1:
    cases: tuple[InstitutionalCorpusCaseV1, ...]
    schema_version: str = _CORPUS_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _CORPUS_SCHEMA_VERSION:
            raise ValueError("institutional corpus schema mismatch")
        if not self.cases:
            raise ValueError("institutional corpus cannot be empty")
        for case in self.cases:
            case.validate()
        ids = tuple(case.case_id for case in self.cases)
        if len(set(ids)) != len(ids):
            raise ValueError("institutional corpus case IDs must be unique")
        if ids != tuple(sorted(ids)):
            raise ValueError("institutional corpus cases must be sorted by case_id")

    def unsigned_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "cases": [case.payload() for case in self.cases],
        }

    @property
    def corpus_sha256(self) -> str:
        return _sha256(self.unsigned_payload())

    def payload(self) -> dict[str, object]:
        return {
            **self.unsigned_payload(),
            "corpus_sha256": self.corpus_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.payload())


@dataclass(frozen=True)
class InstitutionalCorpusObservationV1:
    result_class: str
    failure_code: str | None
    mutation: InstitutionalCorpusMutationExpectation
    generation_delta: int
    output_sha256: str | None
    schema_version: str = _OBSERVATION_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _OBSERVATION_SCHEMA_VERSION:
            raise ValueError("institutional corpus observation schema mismatch")
        if not self.result_class.strip():
            raise ValueError("institutional corpus observation result_class is required")
        if self.failure_code is not None and not self.failure_code.strip():
            raise ValueError("institutional corpus observation failure_code cannot be blank")
        if self.generation_delta < 0:
            raise ValueError(
                "institutional corpus observation generation_delta must be non-negative"
            )
        if self.output_sha256 is not None:
            _digest(self.output_sha256, "output_sha256")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "result_class": self.result_class,
            "failure_code": self.failure_code,
            "mutation": self.mutation.value,
            "generation_delta": self.generation_delta,
            "output_sha256": self.output_sha256,
        }

    @property
    def observation_sha256(self) -> str:
        return _sha256(self.payload())


@dataclass(frozen=True)
class InstitutionalCorpusCaseResultV1:
    case_id: str
    case_sha256: str
    passed: bool
    mismatches: tuple[InstitutionalCorpusMismatch, ...]
    observation: InstitutionalCorpusObservationV1
    schema_version: str = _CASE_RESULT_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _CASE_RESULT_SCHEMA_VERSION:
            raise ValueError("institutional corpus case result schema mismatch")
        if not self.case_id.startswith("qcorpus_"):
            raise ValueError("institutional corpus case result case_id mismatch")
        _digest(self.case_sha256, "case_sha256")
        self.observation.validate()
        if len(set(self.mismatches)) != len(self.mismatches):
            raise ValueError("institutional corpus mismatches must be unique")
        if tuple(sorted(item.value for item in self.mismatches)) != tuple(
            item.value for item in self.mismatches
        ):
            raise ValueError("institutional corpus mismatches must be canonically sorted")
        if self.passed != (not self.mismatches):
            raise ValueError("institutional corpus case result passed/mismatches mismatch")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "case_sha256": self.case_sha256,
            "passed": self.passed,
            "mismatches": [item.value for item in self.mismatches],
            "observation": {
                **self.observation.payload(),
                "observation_sha256": self.observation.observation_sha256,
            },
        }


@dataclass(frozen=True)
class InstitutionalCorpusReportV1:
    corpus_sha256: str
    results: tuple[InstitutionalCorpusCaseResultV1, ...]
    passed: bool
    schema_version: str = _REPORT_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _REPORT_SCHEMA_VERSION:
            raise ValueError("institutional corpus report schema mismatch")
        _digest(self.corpus_sha256, "corpus_sha256")
        if not self.results:
            raise ValueError("institutional corpus report cannot be empty")
        for result in self.results:
            result.validate()
        ids = tuple(result.case_id for result in self.results)
        if ids != tuple(sorted(ids)):
            raise ValueError("institutional corpus report results must be sorted by case_id")
        if self.passed != all(result.passed for result in self.results):
            raise ValueError("institutional corpus report passed/results mismatch")

    def unsigned_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "corpus_sha256": self.corpus_sha256,
            "passed": self.passed,
            "results": [result.payload() for result in self.results],
        }

    @property
    def report_sha256(self) -> str:
        return _sha256(self.unsigned_payload())

    def payload(self) -> dict[str, object]:
        return {
            **self.unsigned_payload(),
            "report_sha256": self.report_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.payload())


@runtime_checkable
class InstitutionalCorpusExecutorV1(Protocol):
    def execute(
        self,
        case: InstitutionalCorpusCaseV1,
    ) -> InstitutionalCorpusObservationV1:
        ...


def build_institutional_corpus(
    cases: tuple[InstitutionalCorpusCaseV1, ...],
) -> InstitutionalCorpusV1:
    corpus = InstitutionalCorpusV1(
        cases=tuple(sorted(cases, key=lambda case: case.case_id))
    )
    corpus.validate()
    return corpus


def run_institutional_corpus(
    *,
    corpus: InstitutionalCorpusV1,
    executor: InstitutionalCorpusExecutorV1,
) -> InstitutionalCorpusReportV1:
    corpus.validate()
    results: list[InstitutionalCorpusCaseResultV1] = []
    for case in corpus.cases:
        observation = executor.execute(case)
        observation.validate()
        mismatches: set[InstitutionalCorpusMismatch] = set()

        if observation.result_class != case.expected_result_class:
            mismatches.add(InstitutionalCorpusMismatch.RESULT_CLASS_MISMATCH)
        if observation.failure_code != case.expected_failure_code:
            mismatches.add(InstitutionalCorpusMismatch.FAILURE_CODE_MISMATCH)
        if observation.mutation is not case.expected_mutation:
            mismatches.add(
                InstitutionalCorpusMismatch.MUTATION_BEHAVIOR_MISMATCH
            )
        if observation.generation_delta != case.expected_generation_delta:
            mismatches.add(
                InstitutionalCorpusMismatch.GENERATION_DELTA_MISMATCH
            )
        if (
            case.expected_output_sha256 is not None
            and observation.output_sha256 != case.expected_output_sha256
        ):
            mismatches.add(InstitutionalCorpusMismatch.OUTPUT_SHA256_MISMATCH)

        ordered = tuple(sorted(mismatches, key=lambda item: item.value))
        result = InstitutionalCorpusCaseResultV1(
            case_id=case.case_id,
            case_sha256=case.case_sha256,
            passed=not ordered,
            mismatches=ordered,
            observation=observation,
        )
        result.validate()
        results.append(result)

    report = InstitutionalCorpusReportV1(
        corpus_sha256=corpus.corpus_sha256,
        results=tuple(results),
        passed=all(result.passed for result in results),
    )
    report.validate()
    return report


def decode_institutional_corpus_case_json(
    encoded: bytes,
) -> InstitutionalCorpusCaseV1:
    raw = _strict_canonical_json(encoded)
    required = {
        "schema_version",
        "case_class",
        "intent",
        "input_b64",
        "expected_result_class",
        "expected_failure_code",
        "expected_mutation",
        "expected_generation_delta",
        "expected_output_sha256",
        "profile_ref",
        "profile_sha256",
        "provenance",
        "case_id",
        "case_sha256",
    }
    if set(raw) != required:
        raise ValueError("institutional corpus case fields mismatch")
    try:
        case = InstitutionalCorpusCaseV1(
            case_class=InstitutionalCorpusCaseClass(
                _string(raw["case_class"], "case_class")
            ),
            intent=_string(raw["intent"], "intent"),
            input_b64=_string(raw["input_b64"], "input_b64"),
            expected_result_class=_string(
                raw["expected_result_class"],
                "expected_result_class",
            ),
            expected_failure_code=_optional_string(
                raw["expected_failure_code"],
                "expected_failure_code",
            ),
            expected_mutation=InstitutionalCorpusMutationExpectation(
                _string(raw["expected_mutation"], "expected_mutation")
            ),
            expected_generation_delta=_integer(
                raw["expected_generation_delta"],
                "expected_generation_delta",
            ),
            expected_output_sha256=_optional_string(
                raw["expected_output_sha256"],
                "expected_output_sha256",
            ),
            profile_ref=_optional_string(raw["profile_ref"], "profile_ref"),
            profile_sha256=_optional_string(
                raw["profile_sha256"],
                "profile_sha256",
            ),
            provenance=_string(raw["provenance"], "provenance"),
            schema_version=_string(raw["schema_version"], "schema_version"),
        )
        case.validate()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid institutional corpus case: {exc}") from exc

    if _string(raw["case_id"], "case_id") != case.case_id:
        raise ValueError("institutional corpus case ID mismatch")
    if _string(raw["case_sha256"], "case_sha256") != case.case_sha256:
        raise ValueError("institutional corpus case digest mismatch")
    return case


def _strict_canonical_json(encoded: bytes) -> dict[str, object]:
    if not encoded:
        raise ValueError("institutional corpus case is empty")
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise ValueError("institutional corpus case UTF-8 BOM is forbidden")
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("institutional corpus case is not valid UTF-8") from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except ValueError:
        raise
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("institutional corpus case JSON is invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError("institutional corpus case root must be an object")
    if canonical_json_bytes(raw) != encoded:
        raise ValueError("institutional corpus case JSON is not canonical")
    return raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate institutional corpus JSON key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> NoReturn:
    raise ValueError(f"floating-point institutional corpus JSON is forbidden: {value}")


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite institutional corpus JSON is forbidden: {value}")


def _decode_b64(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("institutional corpus input_b64 must be strict base64") from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("institutional corpus input_b64 is not canonical base64")
    return decoded


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _sha256(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized
