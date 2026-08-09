import pytest

from app.runtime.sandbox_chaos_v101 import (
    ChaosInjector,
    Fault,
    InjectedMalformedResponse,
    InjectedRateLimit,
    InjectedServiceUnavailable,
    Step,
)


@pytest.mark.parametrize(
    "fault,error",
    [
        (Fault.TIMEOUT, TimeoutError),
        (Fault.CONNECTION_RESET, OSError),
        (Fault.RATE_LIMIT, InjectedRateLimit),
        (Fault.SERVICE_UNAVAILABLE, InjectedServiceUnavailable),
        (Fault.MALFORMED_RESPONSE, InjectedMalformedResponse),
    ],
)
def test_faults_are_deterministic(fault, error):
    injector = ChaosInjector([Step(fault, "read")])
    with pytest.raises(error):
        injector.call("read", lambda: "ok")
    assert injector.call("read", lambda: "ok") == "ok"
    assert injector.attempts == ["read", "read"]


def test_nonmatching_step_is_preserved():
    injector = ChaosInjector([Step(Fault.TIMEOUT, "write")])
    assert injector.call("read", lambda: 1) == 1
    with pytest.raises(TimeoutError):
        injector.call("write", lambda: 2)
