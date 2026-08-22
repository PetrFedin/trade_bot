from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from app.execution.bybit_mainnet_clock_preflight import (
    BybitMainnetClockPreflight,
    measure_bybit_mainnet_clock_preflight,
)
from app.observability.bybit_mainnet_readonly_health import (
    BybitMainnetReadOnlyHealth,
    build_bybit_mainnet_readonly_health,
)
from app.runtime.bybit_mainnet_readonly_probe import (
    BybitMainnetReadOnlyCredentials,
    BybitMainnetReadOnlySnapshot,
    probe_bybit_mainnet_readonly_connection,
)

ClockProbe = Callable[..., BybitMainnetClockPreflight]
ConnectionProbe = Callable[..., BybitMainnetReadOnlySnapshot]


def probe_bybit_mainnet_readonly_operational(
    *,
    credentials: BybitMainnetReadOnlyCredentials,
    clock_probe: ClockProbe = measure_bybit_mainnet_clock_preflight,
    connection_probe: ConnectionProbe = probe_bybit_mainnet_readonly_connection,
) -> BybitMainnetReadOnlyHealth:
    """Run public clock readiness before any authenticated real-account request."""

    preflight = clock_probe(host=credentials.host)
    preflight.require_ready()
    client = credentials.build_client()
    snapshot = connection_probe(client)
    health = build_bybit_mainnet_readonly_health(
        clock_preflight=preflight,
        snapshot=snapshot,
    )
    health.validate()
    if not health.ready:
        raise RuntimeError(
            "Bybit mainnet read-only operational health is not ready:"
            + ",".join(health.reasons)
        )
    return health


def probe_bybit_mainnet_readonly_operational_from_env(
    env: Mapping[str, str] | None = None,
) -> BybitMainnetReadOnlyHealth:
    credentials = BybitMainnetReadOnlyCredentials.from_env(env)
    return probe_bybit_mainnet_readonly_operational(credentials=credentials)


def main() -> int:
    health = probe_bybit_mainnet_readonly_operational_from_env()
    print(json.dumps(health.to_safe_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
