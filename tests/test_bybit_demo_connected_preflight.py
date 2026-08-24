from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from app.execution.bybit_demo_account_reader import (
    BybitDemoAccountInfo,
    BybitDemoWalletBalance,
)
from app.execution.bybit_demo_connected_preflight import (
    BybitDemoConnectedPreflightStatus,
    BybitDemoOperationalDatabaseState,
    BybitDemoPreflightAccountClient,
    BybitDemoReadOnlyOpenPosition,
    run_bybit_demo_connected_preflight,
)


class _FakeTransport:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        self.calls.append((path, query_string, dict(headers)))
        return self.response


class _Account:
    host = "api-demo.bybit.com"
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, positions: tuple[BybitDemoReadOnlyOpenPosition, ...] = ()) -> None:
        self.positions = positions

    def get_wallet_balance(self) -> BybitDemoWalletBalance:
        return BybitDemoWalletBalance(
            total_equity_usd=Decimal("1000"),
            total_wallet_balance_usd=Decimal("1000"),
            total_margin_balance_usd=Decimal("1000"),
            total_available_balance_usd=Decimal("900"),
            total_perp_upl_usd=Decimal("0"),
            total_initial_margin_usd=Decimal("0"),
            total_maintenance_margin_usd=Decimal("0"),
            usdt_wallet_balance=Decimal("1000"),
        )

    def get_account_info(self) -> BybitDemoAccountInfo:
        return BybitDemoAccountInfo(
            margin_mode="REGULAR_MARGIN",
            unified_margin_status=6,
            updated_time_ms=1,
        )

    def get_open_positions(self) -> tuple[BybitDemoReadOnlyOpenPosition, ...]:
        return self.positions


class _Database:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    schema_mutation_supported = False

    def __init__(self, state: BybitDemoOperationalDatabaseState) -> None:
        self.state = state

    def read_state(self) -> BybitDemoOperationalDatabaseState:
        return self.state


def _state(
    *,
    checkpoint_symbol: str | None = None,
    checkpoint_side: str | None = None,
    lease: bool = False,
    relations: bool = True,
    triggers: bool = True,
) -> BybitDemoOperationalDatabaseState:
    return BybitDemoOperationalDatabaseState(
        required_relations_present=relations,
        append_only_triggers_present=triggers,
        runtime_lease_present=lease,
        active_checkpoint_order_link_id=(
            None if checkpoint_symbol is None else "ASTRA-DEMO-E-TEST"
        ),
        active_checkpoint_symbol=checkpoint_symbol,
        active_checkpoint_side=checkpoint_side,
        approval_record_count=3,
        provenance_record_count=2,
        terminal_record_count=1,
    )


def _position(
    symbol: str = "BTCUSDT",
    side: str = "Buy",
) -> BybitDemoReadOnlyOpenPosition:
    return BybitDemoReadOnlyOpenPosition(
        symbol=symbol,
        side=side,
        size=Decimal("0.01"),
        average_price=Decimal("60000"),
    )


def test_clean_account_is_ready_only_for_manual_operator_approval() -> None:
    result = run_bybit_demo_connected_preflight(_Account(), _Database(_state()))

    assert result.status is BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL
    assert result.reasons == ()
    assert result.passed is True
    assert result.trade_actionable is False
    assert result.order_writes_supported is False
    assert result.live_mainnet_order_routing_allowed is False
    payload = result.to_payload()
    assert payload["account"]["positive_equity"] is True
    assert "total_equity_usd" not in payload["account"]


def test_matching_exchange_position_and_checkpoint_requires_management_only() -> None:
    result = run_bybit_demo_connected_preflight(
        _Account((_position(),)),
        _Database(_state(checkpoint_symbol="BTCUSDT", checkpoint_side="LONG")),
    )

    assert result.status is BybitDemoConnectedPreflightStatus.EXISTING_TRADE_MANAGEMENT_REQUIRED
    assert result.reasons == ()
    assert result.passed is True
    assert result.open_position_symbols == ("BTCUSDT",)


def test_exchange_position_without_checkpoint_is_blocked() -> None:
    result = run_bybit_demo_connected_preflight(
        _Account((_position(),)),
        _Database(_state()),
    )

    assert result.status is BybitDemoConnectedPreflightStatus.BLOCKED
    assert "DEMO_EXCHANGE_POSITION_WITHOUT_CHECKPOINT" in result.reasons


def test_checkpoint_without_exchange_position_is_blocked() -> None:
    result = run_bybit_demo_connected_preflight(
        _Account(),
        _Database(_state(checkpoint_symbol="BTCUSDT", checkpoint_side="LONG")),
    )

    assert result.status is BybitDemoConnectedPreflightStatus.BLOCKED
    assert "DEMO_CHECKPOINT_WITHOUT_EXCHANGE_POSITION" in result.reasons


def test_mismatch_lease_and_missing_durable_guards_accumulate_fail_closed() -> None:
    result = run_bybit_demo_connected_preflight(
        _Account((_position(symbol="ETHUSDT", side="Sell"),)),
        _Database(
            _state(
                checkpoint_symbol="BTCUSDT",
                checkpoint_side="LONG",
                lease=True,
                triggers=False,
            )
        ),
    )

    assert result.status is BybitDemoConnectedPreflightStatus.BLOCKED
    assert "DEMO_POSTGRES_V120_APPEND_ONLY_TRIGGERS_NOT_READY" in result.reasons
    assert "DEMO_CANONICAL_RUNTIME_LEASE_PRESENT" in result.reasons
    assert "DEMO_POSITION_CHECKPOINT_SYMBOL_MISMATCH" in result.reasons
    assert "DEMO_POSITION_CHECKPOINT_SIDE_MISMATCH" in result.reasons


def test_more_than_one_exchange_position_is_blocked() -> None:
    result = run_bybit_demo_connected_preflight(
        _Account((_position(), _position("ETHUSDT"))),
        _Database(_state()),
    )

    assert result.status is BybitDemoConnectedPreflightStatus.BLOCKED
    assert "DEMO_MULTIPLE_OPEN_POSITIONS_NOT_SUPPORTED" in result.reasons


def test_preflight_position_reader_uses_demo_get_only_and_skips_zero_positions() -> None:
    transport = _FakeTransport(
        {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "size": "0.01",
                        "avgPrice": "60000",
                    },
                    {"symbol": "ETHUSDT", "side": "", "size": "0", "avgPrice": ""},
                ],
                "nextPageCursor": "",
            },
        }
    )
    client = BybitDemoPreflightAccountClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1,
    )

    positions = client.get_open_positions()

    assert len(positions) == 1
    assert positions[0].symbol == "BTCUSDT"
    path, query, _headers = transport.calls[0]
    assert path == "/v5/position/list"
    assert query == "category=linear&settleCoin=USDT"
    assert client.live_mainnet_order_routing_allowed is False
    assert client.order_writes_supported is False
    assert not hasattr(client, "place_order")
    assert not hasattr(client, "cancel_order")
