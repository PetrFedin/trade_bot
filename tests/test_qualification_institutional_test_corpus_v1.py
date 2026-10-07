from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from app.qualification.institutional_test_corpus_v1 import (
    InstitutionalCorpusCaseClass,
    InstitutionalCorpusCaseV1,
    InstitutionalCorpusMismatch,
    InstitutionalCorpusMutationExpectation,
    InstitutionalCorpusObservationV1,
    build_institutional_corpus,
    decode_institutional_corpus_case_json,
    run_institutional_corpus,
)
from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.qualification.verification_api_contract_v1 import (
    VerificationAPIOperation,
    VerificationAPIRequestV1,
    VerificationAPIResultClass,
    encode_artifact_b64,
)
from app.qualification.verification_reference_profiles_v1 import (
    canonical_reference_profiles_v1,
)
from tests.helpers_v108 import NOW
from tests.test_qualification_verification_api_service_v1 import _setup


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _case(
    case_class: InstitutionalCorpusCaseClass,
    *,
    suffix: str,
    result_class: str = "OK",
    failure_code: str | None = None,
    mutation: InstitutionalCorpusMutationExpectation = (
        InstitutionalCorpusMutationExpectation.NONE
    ),
    generation_delta: int = 0,
    output_sha256: str | None = None,
    profile_ref: str | None = None,
    profile_sha256: str | None = None,
) -> InstitutionalCorpusCaseV1:
    return InstitutionalCorpusCaseV1.from_input_bytes(
        case_class=case_class,
        intent=f"exercise {case_class.value} {suffix}",
        input_bytes=f"input:{case_class.value}:{suffix}".encode("utf-8"),
        expected_result_class=result_class,
        expected_failure_code=failure_code,
        expected_mutation=mutation,
        expected_generation_delta=generation_delta,
        expected_output_sha256=output_sha256,
        profile_ref=profile_ref,
        profile_sha256=profile_sha256,
        provenance="ASTRA_TEST_CORPUS_GENERATOR_V1",
    )


class MappingExecutor:
    def __init__(self, observations):
        self.observations = observations

    def execute(self, case):
        return self.observations[case.case_id]


def test_all_twelve_corpus_classes_are_portable_and_stable() -> None:
    cases = tuple(
        _case(case_class, suffix=str(index))
        for index, case_class in enumerate(InstitutionalCorpusCaseClass)
    )
    corpus = build_institutional_corpus(cases)

    assert len(corpus.cases) == 12
    assert {case.case_class for case in corpus.cases} == set(
        InstitutionalCorpusCaseClass
    )
    assert len({case.case_id for case in corpus.cases}) == 12
    assert len({case.case_sha256 for case in corpus.cases}) == 12
    assert corpus.canonical_bytes() == build_institutional_corpus(
        tuple(reversed(cases))
    ).canonical_bytes()


@pytest.mark.parametrize(
    "case_class",
    tuple(InstitutionalCorpusCaseClass),
    ids=lambda item: item.value,
)
def test_corpus_case_round_trip_is_byte_stable(
    case_class: InstitutionalCorpusCaseClass,
) -> None:
    case = _case(case_class, suffix="roundtrip")
    encoded = case.canonical_bytes()

    decoded = decode_institutional_corpus_case_json(encoded)

    assert decoded == case
    assert decoded.case_id == case.case_id
    assert decoded.case_sha256 == case.case_sha256
    assert decoded.canonical_bytes() == encoded


@pytest.mark.parametrize(
    "encoded",
    [
        b"",
        b"\xef\xbb\xbf{}",
        b'{"a":1,"a":2}',
        b'{"value":1.5}',
        b'{"value":NaN}',
        b"[]",
        b'{ "not":"canonical" }',
        b"{",
        b"\xff",
    ],
)
def test_corpus_case_decoder_rejects_ambiguous_json(encoded: bytes) -> None:
    with pytest.raises(ValueError):
        decode_institutional_corpus_case_json(encoded)


def test_corpus_case_decoder_rejects_unknown_fields_and_identity_tampering() -> None:
    case = _case(
        InstitutionalCorpusCaseClass.SERIALIZATION_REJECTION,
        suffix="tamper",
    )
    payload = case.payload()
    payload["unknown"] = "forbidden"

    with pytest.raises(ValueError, match="fields mismatch"):
        decode_institutional_corpus_case_json(canonical_json_bytes(payload))

    payload = case.payload()
    payload["case_id"] = "qcorpus_wrong"
    with pytest.raises(ValueError, match="ID mismatch"):
        decode_institutional_corpus_case_json(canonical_json_bytes(payload))

    payload = case.payload()
    payload["case_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="digest mismatch"):
        decode_institutional_corpus_case_json(canonical_json_bytes(payload))


def test_profile_identity_must_be_complete_pair() -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        _case(
            InstitutionalCorpusCaseClass.REFERENCE_PROFILE_CONFORMANCE,
            suffix="bad-profile",
            profile_ref="read-only-auditor@1.0",
            profile_sha256=None,
        )


@pytest.mark.parametrize(
    ("mutation", "delta"),
    [
        (InstitutionalCorpusMutationExpectation.ADVANCE_ONCE, 0),
        (InstitutionalCorpusMutationExpectation.NONE, 1),
        (InstitutionalCorpusMutationExpectation.CONFLICT_NO_ADVANCE, 1),
        (InstitutionalCorpusMutationExpectation.RECOVERY_NO_SECOND_ADVANCE, 1),
    ],
)
def test_mutation_expectation_and_generation_delta_are_consistent(
    mutation: InstitutionalCorpusMutationExpectation,
    delta: int,
) -> None:
    with pytest.raises(ValueError):
        _case(
            InstitutionalCorpusCaseClass.CAS_CONFLICT,
            suffix="bad-delta",
            mutation=mutation,
            generation_delta=delta,
        )


def test_runner_report_is_deterministic_and_ordered() -> None:
    cases = (
        _case(
            InstitutionalCorpusCaseClass.POSITIVE_VERIFICATION,
            suffix="a",
            result_class="VERIFIED_USABLE",
            output_sha256="1" * 64,
        ),
        _case(
            InstitutionalCorpusCaseClass.DETERMINISTIC_REJECTION,
            suffix="b",
            result_class="REJECTED",
            failure_code="SIGNATURE_INVALID",
        ),
    )
    corpus = build_institutional_corpus(cases)
    observations = {
        case.case_id: InstitutionalCorpusObservationV1(
            result_class=case.expected_result_class,
            failure_code=case.expected_failure_code,
            mutation=case.expected_mutation,
            generation_delta=case.expected_generation_delta,
            output_sha256=case.expected_output_sha256,
        )
        for case in corpus.cases
    }

    first = run_institutional_corpus(
        corpus=corpus,
        executor=MappingExecutor(observations),
    )
    second = run_institutional_corpus(
        corpus=corpus,
        executor=MappingExecutor(observations),
    )

    assert first.passed is True
    assert all(result.passed for result in first.results)
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.report_sha256 == second.report_sha256
    assert tuple(result.case_id for result in first.results) == tuple(
        sorted(result.case_id for result in first.results)
    )


def test_runner_surfaces_all_observation_mismatches_in_stable_order() -> None:
    case = _case(
        InstitutionalCorpusCaseClass.CRASH_RECOVERY,
        suffix="mismatch",
        result_class="VERIFIED_USABLE",
        failure_code=None,
        mutation=InstitutionalCorpusMutationExpectation.RECOVERY_NO_SECOND_ADVANCE,
        generation_delta=0,
        output_sha256="1" * 64,
    )
    corpus = build_institutional_corpus((case,))
    observation = InstitutionalCorpusObservationV1(
        result_class="REJECTED",
        failure_code="WRONG",
        mutation=InstitutionalCorpusMutationExpectation.ADVANCE_ONCE,
        generation_delta=1,
        output_sha256="2" * 64,
    )

    report = run_institutional_corpus(
        corpus=corpus,
        executor=MappingExecutor({case.case_id: observation}),
    )

    assert report.passed is False
    assert report.results[0].mismatches == tuple(
        sorted(
            (
                InstitutionalCorpusMismatch.RESULT_CLASS_MISMATCH,
                InstitutionalCorpusMismatch.FAILURE_CODE_MISMATCH,
                InstitutionalCorpusMismatch.MUTATION_BEHAVIOR_MISMATCH,
                InstitutionalCorpusMismatch.GENERATION_DELTA_MISMATCH,
                InstitutionalCorpusMismatch.OUTPUT_SHA256_MISMATCH,
            ),
            key=lambda item: item.value,
        )
    )


def test_all_five_reference_profiles_are_bound_to_corpus_cases() -> None:
    profiles = canonical_reference_profiles_v1()
    cases = tuple(
        _case(
            InstitutionalCorpusCaseClass.REFERENCE_PROFILE_CONFORMANCE,
            suffix=profile.profile_id,
            result_class="CONFORMANT",
            profile_ref=profile.profile_ref,
            profile_sha256=profile.profile_sha256,
        )
        for profile in profiles
    )
    corpus = build_institutional_corpus(cases)

    assert len(corpus.cases) == 5
    assert {case.profile_ref for case in corpus.cases} == {
        profile.profile_ref for profile in profiles
    }
    assert {case.profile_sha256 for case in corpus.cases} == {
        profile.profile_sha256 for profile in profiles
    }


def test_real_api_positive_read_only_vector(tmp_path) -> None:
    service, authority, _, artifact_bytes, _ = _setup(tmp_path)
    before = authority.current()
    request = VerificationAPIRequestV1(
        operation=VerificationAPIOperation.VERIFY_READ_ONLY,
        request_id="corpus-read-1",
        authority_id="primary",
        trusted_root_set_id="good-roots",
        artifact_b64=encode_artifact_b64(artifact_bytes),
        observed_at=NOW + timedelta(seconds=11),
    )
    request_bytes = canonical_json_bytes(request.payload())
    response = service.handle(request)
    response_bytes = canonical_json_bytes(response)
    after = authority.current()
    case = InstitutionalCorpusCaseV1.from_input_bytes(
        case_class=InstitutionalCorpusCaseClass.POSITIVE_VERIFICATION,
        intent="real API read-only positive verification",
        input_bytes=request_bytes,
        expected_result_class=VerificationAPIResultClass.VERIFIED_USABLE.value,
        expected_failure_code=None,
        expected_mutation=InstitutionalCorpusMutationExpectation.NONE,
        expected_generation_delta=0,
        expected_output_sha256=_sha(response_bytes),
        profile_ref=None,
        profile_sha256=None,
        provenance="REAL_VERIFICATION_API_FIXTURE_V1",
    )
    observation = InstitutionalCorpusObservationV1(
        result_class=response["result_class"],
        failure_code=response["failure_code"],
        mutation=InstitutionalCorpusMutationExpectation.NONE,
        generation_delta=after.generation - before.generation,
        output_sha256=_sha(response_bytes),
    )

    report = run_institutional_corpus(
        corpus=build_institutional_corpus((case,)),
        executor=MappingExecutor({case.case_id: observation}),
    )

    assert report.passed is True
    assert authority.current() == before


def test_real_api_wrong_root_rejection_vector(tmp_path) -> None:
    service, authority, _, artifact_bytes, _ = _setup(tmp_path)
    before = authority.current()
    request = VerificationAPIRequestV1(
        operation=VerificationAPIOperation.VERIFY_READ_ONLY,
        request_id="corpus-reject-1",
        authority_id="primary",
        trusted_root_set_id="wrong-roots",
        artifact_b64=encode_artifact_b64(artifact_bytes),
        observed_at=NOW + timedelta(seconds=11),
    )
    response = service.handle(request)
    response_bytes = canonical_json_bytes(response)
    after = authority.current()
    case = InstitutionalCorpusCaseV1.from_input_bytes(
        case_class=InstitutionalCorpusCaseClass.TRUSTED_ROOT_SET_MISMATCH,
        intent="real API wrong trusted-root-set rejection",
        input_bytes=canonical_json_bytes(request.payload()),
        expected_result_class=VerificationAPIResultClass.REJECTED.value,
        expected_failure_code=response["failure_code"],
        expected_mutation=InstitutionalCorpusMutationExpectation.NONE,
        expected_generation_delta=0,
        expected_output_sha256=_sha(response_bytes),
        profile_ref=None,
        profile_sha256=None,
        provenance="REAL_VERIFICATION_API_FIXTURE_V1",
    )
    observation = InstitutionalCorpusObservationV1(
        result_class=response["result_class"],
        failure_code=response["failure_code"],
        mutation=InstitutionalCorpusMutationExpectation.NONE,
        generation_delta=after.generation - before.generation,
        output_sha256=_sha(response_bytes),
    )

    report = run_institutional_corpus(
        corpus=build_institutional_corpus((case,)),
        executor=MappingExecutor({case.case_id: observation}),
    )

    assert report.passed is True
    assert authority.current() == before


def test_real_api_idempotent_advance_vector_proves_single_commit(tmp_path) -> None:
    service, authority, _, artifact_bytes, _ = _setup(tmp_path)
    before = authority.current()
    request = VerificationAPIRequestV1(
        operation=VerificationAPIOperation.VERIFY_ADVANCE,
        request_id="corpus-advance-1",
        authority_id="primary",
        trusted_root_set_id="good-roots",
        artifact_b64=encode_artifact_b64(artifact_bytes),
        idempotency_key="corpus-idem-advance-1",
        observed_at=NOW + timedelta(seconds=11),
    )
    first = service.handle(request)
    after_first = authority.current()
    second = service.handle(request)
    after_second = authority.current()
    first_bytes = canonical_json_bytes(first)
    second_bytes = canonical_json_bytes(second)

    case = InstitutionalCorpusCaseV1.from_input_bytes(
        case_class=InstitutionalCorpusCaseClass.IDEMPOTENCY_SINGLE_FLIGHT,
        intent="real API idempotent advance replay commits once",
        input_bytes=canonical_json_bytes(request.payload()),
        expected_result_class=VerificationAPIResultClass.VERIFIED_USABLE.value,
        expected_failure_code=None,
        expected_mutation=InstitutionalCorpusMutationExpectation.ADVANCE_ONCE,
        expected_generation_delta=1,
        expected_output_sha256=_sha(first_bytes),
        profile_ref=None,
        profile_sha256=None,
        provenance="REAL_VERIFICATION_API_FIXTURE_V1",
    )
    observation = InstitutionalCorpusObservationV1(
        result_class=second["result_class"],
        failure_code=second["failure_code"],
        mutation=InstitutionalCorpusMutationExpectation.ADVANCE_ONCE,
        generation_delta=after_second.generation - before.generation,
        output_sha256=_sha(second_bytes),
    )

    report = run_institutional_corpus(
        corpus=build_institutional_corpus((case,)),
        executor=MappingExecutor({case.case_id: observation}),
    )

    assert report.passed is True
    assert first_bytes == second_bytes
    assert after_first.generation == before.generation + 1
    assert after_second == after_first
