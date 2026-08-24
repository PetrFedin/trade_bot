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
    BybitDemoReadOnlyApiKeyInfo,
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

    def __init__(
        self,
        positions: tuple[BybitDemoReadOnlyOpenPosition, ...] = (),
        *,
        entry_execution_found: bool = True,
        api_key_read_only: bool = True,
        api_key_ip_binding_present: bool = False,
    ) -> None:
        self.positions = positions
        self.entry_execution_found = entry_execution_found
        self.api_key_read_only = api_key_read_only
        self.api_key_ip_binding_present = api_key_ip_binding_present
        self.execution_queries: list[tuple[str, str, str]] = []

    def get_api_key_info(self) -> BybitDemoReadOnlyApiKeyInfo:
        return BybitDemoReadOnlyApiKeyInfo(
            read_only=self.api_key_read_only,
            ip_binding_present=self.api_key_ip_binding_present,
        )

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

    def has_entry_execution(
        self,
        *,
        symbol: str,
        side: str,
        entry_order_link_id: str,
    ) -> bool:
        self.execution_queries.append((symbol, side, entry_order_link_id))
        return self.entry_execution_found


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
    checkpoint_entry_price: Decimal = Decimal("60000"),
    checkpoint_current_quantity: Decimal = Decimal("0.01"),
    lease: bool = False,
    relations: bool = True,
    triggers: bool = True,
) -> BybitDemoOperationalDatabaseState:
    has_checkpoint = checkpoint_symbol is not None
    return BybitDemoOperationalDatabaseState(
        required_relations_present=relations,
        append_only_triggers_present=triggers,
        runtime_lease_present=lease,
        active_checkpoint_order_link_id=(
            "ASTRA-DEMO-E-TEST" if has_checkpoint else None
        ),
        active_checkpoint_symbol=checkpoint_symbol,
        active_checkpoint_side=checkpoint_side,
        active_checkpoint_entry_price=(checkpoint_entry_price if has_checkpoint else None),
        active_checkpoint_current_quantity=(
            checkpoint_current_quantity if has_checkpoint else None
        ),
        approval_record_count=3,
        provenance_record_count=2,
        terminal_record_count=1,
    )


def _position(
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    *,
    size: Decimal = Decimal("0.01"),
    average_price: Decimal = Decimal("60000"),
) -> BybitDemoReadOnlyOpenPosition:
    return BybitDemoReadOnlyOpenPosition(
        symbol=symbol,
        side=side,
        size=size,
        average_price=average_price,
    )


def test_clean_account_is_ready_only_for_manual_operator_approval() -> None:
    result = run_bybit_demo_connected_preflight(_Account(), _Database(_state()))

    assert result.status is BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL
    assert result.reasons == ()
    assert result.passed is True
    assert result.read_only_api_key_verified is True
    assert result.trade_actionable is False
    assert result.order_writes_supported is False
    assert result.live_mainnet_order_routing_allowed is False
    payload = result.to_payload()
    assert payload["account"]["positive_equity"] is True
    assert payload["credential"]["read_only_api_key_verified"] is True
    assert "total_equity_usd" not in payload["account"]


def test_write_enabled_api_key_is_blocked_even_though_client_code_is_get_only() -> None:
    result = run_bybit_demo_connected_preflight(
        _Account(api_key_read_only=False),
        _Database(_state()),
    )

    assert result.status is BybitDemoConnectedPreflightStatus.BLOCKED
    assert result.reasons == ("DEMO_API_KEY_IS_NOT_READ_ONLY",)
    assert result.read_only_api_key_verified is False


def test_matching_exchange_position_and_checkpoint_requires_management_only() -> None:
    account = _Account((_position(),))
    result = run_bybit_demo_connected_preflight(
        account,
        _Database(_state(checkpoint_symbol="BTCUSDT", checkpoint_side="LONG")),
    )

    assert result.status is BybitDemoConnectedPreflightStatus.EXISTING_TRADE_MANAGEMENT_REQUIRED
    assert result.reasons == ()
    assert result.passed is True
    assert result.open_position_symbols == ("BTCUSDT",)
    assert account.execution_queries == [("BTCUSDT", "Buy", "ASTRA-DEMO-E-TEST")]


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


def test_missing_exact_entry_execution_blocks_matching_position() -> None:
    account = _Account((_position(),), entry_execution_found=False)
    result = run_bybit_demo_connected_preflight(
        account,
        _Database(_state(checkpoint_symbol="BTCUSDT", checkpoint_side="LONG")),
    )

    assert result.status is BybitDemoConnectedPreflightStatus.BLOCKED
    assert "DEMO_CHECKPOINT_ENTRY_EXECUTION_NOT_FOUND" in result.reasons


def test_quantity_and_entry_price_drift_are_blocked() -> None:
    result = run_bybit_demo_connected_preflight(
        _Account((_position(size=Decimal("0.009"), average_price=Decimal("60001")),)),
        _Database(_state(checkpoint_symbol="BTCUSDT", checkpoint_side="LONG")),
    )

    assert result.status is BybitDemoConnectedPreflightStatus.BLOCKED
    assert "DEMO_POSITION_CHECKPOINT_QUANTITY_MISMATCH" in result.reasons
    assert "DEMO_POSITION_CHECKPOINT_ENTRY_PRICE_MISMATCH" in result.reasons


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


def test_preflight_api_key_reader_proves_read_only_capability() -> None:
    transport = _FakeTransport(
        {
            "retCode": 0,
            "result": {
                "readOnly": 1,
                "ips": ["*"],
            },
        }
    )
    client = BybitDemoPreflightAccountClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1,
    )

    info = client.get_api_key_info()

    assert info.read_only is True
    assert info.ip_binding_present is False
    path, query, _headers = transport.calls[0]
    assert path == "/v5/user/query-api"
    assert query == ""


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


def test_preflight_entry_execution_reader_requires_exact_order_identity() -> None:
    transport = _FakeTransport(
        {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "orderLinkId": "ASTRA-DEMO-E-TEST",
                        "execQty": "0.01",
                    }
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

    found = client.has_entry_execution(
        symbol="BTCUSDT",
        side="Buy",
        entry_order_link_id="ASTRA-DEMO-E-TEST",
    )

    assert found is True
    path, query, _headers = transport.calls[0]
    assert path == "/v5/execution/list"
    assert query == (
        "category=linear&limit=100&orderLinkId=ASTRA-DEMO-E-TEST&symbol=BTCUSDT"
    )
