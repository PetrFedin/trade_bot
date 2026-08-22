from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from app.execution.bybit_mainnet_clock_preflight import (
    BybitMainnetClockPreflight,
    measure_bybit_mainnet_clock_preflight,
)
from app.execution.bybit_mainnet_readonly_activity import (
    BybitMainnetActivitySnapshot,
    BybitMainnetActivityWindow,
    BybitMainnetReadOnlyActivityClient,
    read_bybit_mainnet_activity,
)
from app.runtime.bybit_mainnet_readonly_probe import BybitMainnetReadOnlyCredentials

ClockProbe = Callable[..., BybitMainnetClockPreflight]
ActivityReader = Callable[..., BybitMainnetActivitySnapshot]


def probe_bybit_mainnet_readonly_activity(
    *,
    credentials: BybitMainnetReadOnlyCredentials,
    clock_probe: ClockProbe = measure_bybit_mainnet_clock_preflight,
    activity_reader: ActivityReader = read_bybit_mainnet_activity,
) -> BybitMainnetActivitySnapshot:
    """Read the latest 24h broker activity only after public clock readiness succeeds."""

    preflight = clock_probe(host=credentials.host)
    preflight.require_ready()
    window = BybitMainnetActivityWindow.last_24_hours_ending_at(preflight.server_time_ms)
    client = BybitMainnetReadOnlyActivityClient(
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        host=credentials.host,
    )
    snapshot = activity_reader(client, window=window)
    snapshot.validate()
    if snapshot.api_host != preflight.api_host:
        raise RuntimeError("Bybit activity and clock preflight used different API hosts")
    return snapshot


def probe_bybit_mainnet_readonly_activity_from_env(
    env: Mapping[str, str] | None = None,
) -> BybitMainnetActivitySnapshot:
    credentials = BybitMainnetReadOnlyCredentials.from_env(env)
    return probe_bybit_mainnet_readonly_activity(credentials=credentials)


def main() -> int:
    snapshot = probe_bybit_mainnet_readonly_activity_from_env()
    print(json.dumps(snapshot.to_safe_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
