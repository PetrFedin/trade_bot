from decimal import Decimal

import pytest

from app.execution.bybit_mainnet_clock_preflight import (
    BybitMainnetClockPreflight,
    BybitMainnetClockPreflightError,
)
from app.execution.bybit_mainnet_readonly import (
    BybitMainnetAccountInfo,
    BybitMainnetApiKeyInfo,
    BybitMainnetPosition,
    BybitMainnetWalletBalance,
)
from app.observability.bybit_mainnet_readonly_health import (
    build_bybit_mainnet_readonly_health,
)
from app.runtime.bybit_mainnet_readonly_operational_probe import (
    probe_bybit_mainnet_readonly_operational,
)
from app.runtime.bybit_mainnet_readonly_probe import (
    BybitMainnetReadOnlyCredentials,
    BybitMainnetReadOnlySnapshot,
)

_FINGERPRINT = "a" * 64


def _clock(*, host: str = "api.bybit.com", offset_ms: int = 0) -> BybitMainnetClockPreflight:
    send = 10_000
    receive = 10_200
    midpoint = 10_100
    server = midpoint + offset_ms
    preflight = BybitMainnetClockPreflight(
        api_host=host,
        local_send_time_ms=send,
        local_receive_time_ms=receive,
        server_time_ms=server,
        round_trip_time_ms=200,
        estimated_clock_offset_ms=offset_ms,
        uncertainty_ms=100,
        worst_case_abs_clock_skew_ms=abs(offset_ms) + 100,
    )
    preflight.validate()
    return preflight


def _snapshot(*, host: str = "api.bybit.com") -> BybitMainnetReadOnlySnapshot:
    snapshot = BybitMainnetReadOnlySnapshot(
        api_key=BybitMainnetApiKeyInfo(
            key_fingerprint_sha256=_FINGERPRINT,
            read_only=True,
            ip_bindings=("203.0.113.10",),
            key_type=1,
            note="astra-readonly",
            permissions=("ContractTrade:Position",),
        ),
        account=BybitMainnetAccountInfo(
            margin_mode="REGULAR_MARGIN",
            unified_margin_status=6,
            updated_time_ms=10_000,
        ),
        wallet=BybitMainnetWalletBalance(
            total_equity_usd=Decimal("2500"),
            total_wallet_balance_usd=Decimal("2475"),
            total_margin_balance_usd=Decimal("2500"),
            total_available_balance_usd=Decimal("2000"),
            total_perp_upl_usd=Decimal("25"),
            total_initial_margin_usd=Decimal("400"),
            total_maintenance_margin_usd=Decimal("40"),
            usdt_wallet_balance=Decimal("2475"),
        ),
        positions=(
            BybitMainnetPosition(
                symbol="BTCUSDT",
                side="Buy",
                size=Decimal("0.01"),
                position_idx=0,
                average_price=Decimal("100000"),
                mark_price=Decimal("101000"),
                position_value=Decimal("1010"),
                unrealised_pnl=Decimal("10"),
                liquidation_price=Decimal("50000"),
                leverage=Decimal("2"),
            ),
            BybitMainnetPosition(
                symbol="ETHUSDT",
                side="Sell",
                size=Decimal("0.2"),
                position_idx=0,
                average_price=Decimal("4000"),
                mark_price=Decimal("3950"),
                position_value=Decimal("790"),
                unrealised_pnl=Decimal("10"),
                liquidation_price=Decimal("8000"),
                leverage=Decimal("2"),
            ),
        ),
        api_host=host,
    )
    snapshot.validate()
    return snapshot


def test_health_aggregates_real_account_metrics_without_granting_writes() -> None:
    health = build_bybit_mainnet_readonly_health(
        clock_preflight=_clock(),
        snapshot=_snapshot(),
    )

    assert health.ready is True
    assert health.reasons == ()
    assert health.available_balance_ratio == Decimal("0.8")
    assert health.initial_margin_ratio == Decimal("0.16")
    assert health.maintenance_margin_ratio == Decimal("0.016")
    assert health.open_position_count == 2
    assert health.gross_position_value_usd == Decimal("1800")
    assert health.open_position_unrealised_pnl_usd == Decimal("20")
    assert health.live_mainnet_order_routing_allowed is False
    assert health.order_writes_supported is False
    safe = health.to_safe_dict()
    assert safe["account"]["total_equity_usd"] == "2500"
    assert safe["positions"]["gross_position_value_usd"] == "1800"


def test_health_rejects_clock_and_account_snapshots_from_different_hosts() -> None:
    with pytest.raises(ValueError, match="different hosts"):
        build_bybit_mainnet_readonly_health(
            clock_preflight=_clock(host="api.bybit.nl"),
            snapshot=_snapshot(host="api.bybit.com"),
        )


def test_operational_probe_runs_clock_preflight_before_authenticated_connection() -> None:
    events: list[str] = []
    credentials = BybitMainnetReadOnlyCredentials(
        api_key="key",
        api_secret="secret",
        site="global",
    )

    def clock_probe(*, host: str) -> BybitMainnetClockPreflight:
        events.append(f"clock:{host}")
        return _clock(host=host)

    def connection_probe(client: object) -> BybitMainnetReadOnlySnapshot:
        events.append("private-account")
        assert getattr(client, "host") == "api.bybit.com"
        return _snapshot()

    health = probe_bybit_mainnet_readonly_operational(
        credentials=credentials,
        clock_probe=clock_probe,
        connection_probe=connection_probe,
    )

    assert health.ready is True
    assert events == ["clock:api.bybit.com", "private-account"]


def test_operational_probe_never_performs_private_read_when_clock_is_unsafe() -> None:
    events: list[str] = []
    credentials = BybitMainnetReadOnlyCredentials(
        api_key="key",
        api_secret="secret",
        site="global",
    )

    def clock_probe(*, host: str) -> BybitMainnetClockPreflight:
        events.append(f"clock:{host}")
        return _clock(host=host, offset_ms=600)

    def connection_probe(client: object) -> BybitMainnetReadOnlySnapshot:
        events.append("private-account")
        return _snapshot()

    with pytest.raises(BybitMainnetClockPreflightError, match="CLOCK_SKEW_UNSAFE"):
        probe_bybit_mainnet_readonly_operational(
            credentials=credentials,
            clock_probe=clock_probe,
            connection_probe=connection_probe,
        )

    assert events == ["clock:api.bybit.com"]


def test_health_keeps_position_aggregates_unknown_when_broker_fields_are_missing() -> None:
    snapshot = _snapshot()
    incomplete = BybitMainnetReadOnlySnapshot(
        api_key=snapshot.api_key,
        account=snapshot.account,
        wallet=snapshot.wallet,
        positions=(
            BybitMainnetPosition(
                symbol="BTCUSDT",
                side="Buy",
                size=Decimal("0.01"),
                position_idx=0,
                average_price=Decimal("100000"),
                mark_price=Decimal("101000"),
                position_value=None,
                unrealised_pnl=None,
                liquidation_price=None,
                leverage=Decimal("2"),
            ),
        ),
        api_host=snapshot.api_host,
    )
    incomplete.validate()

    health = build_bybit_mainnet_readonly_health(
        clock_preflight=_clock(),
        snapshot=incomplete,
    )

    assert health.open_position_count == 1
    assert health.gross_position_value_usd is None
    assert health.open_position_unrealised_pnl_usd is None
