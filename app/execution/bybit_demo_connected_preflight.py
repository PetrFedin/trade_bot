from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo_account_reader import BybitDemoAccountingClient

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_DEMO_HOST = "api-demo.bybit.com"
_REQUIRED_RELATIONS = (
    "astra_bybit_demo_runtime_lease_v119",
    "astra_bybit_demo_active_excursion_v119",
    "astra_bybit_demo_approved_entry_authorization_v120",
    "astra_bybit_demo_entry_provenance_v120",
    "astra_bybit_demo_terminal_evidence_v120",
)
_REQUIRED_APPEND_ONLY_TRIGGERS = (
    "astra_bybit_demo_approval_append_only_v120",
    "astra_bybit_demo_provenance_append_only_v120",
    "astra_bybit_demo_terminal_append_only_v120",
)


@dataclass(frozen=True)
class BybitDemoReadOnlyOpenPosition:
    symbol: str
    side: str
    size: Decimal
    average_price: Decimal | None

    def validate(self) -> None:
        if self.symbol != self.symbol.strip().upper() or not self.symbol.endswith("USDT"):
            raise ValueError("Bybit Demo preflight position symbol must be normalized USDT")
        if self.side not in {"Buy", "Sell"}:
            raise ValueError("Bybit Demo preflight position side must be Buy or Sell")
        if not self.size.is_finite() or self.size <= 0:
            raise ValueError("Bybit Demo preflight position size must be positive")
        if self.average_price is not None and (
            not self.average_price.is_finite() or self.average_price <= 0
        ):
            raise ValueError("Bybit Demo preflight average price must be positive")


@dataclass(frozen=True)
class BybitDemoReadOnlyApiKeyInfo:
    read_only: bool
    ip_binding_present: bool


class BybitDemoPreflightAccountClient(BybitDemoAccountingClient):
    """Demo-only authenticated GET surface used before any order-capable client exists."""

    def get_api_key_info(self) -> BybitDemoReadOnlyApiKeyInfo:
        result = self._private_get_result(  # noqa: SLF001 - bounded read-only subclass extension.
            path="/v5/user/query-api",
            query={},
        )
        raw_read_only = result.get("readOnly")
        if isinstance(raw_read_only, bool) or raw_read_only not in {0, 1}:
            raise ValueError("Bybit Demo preflight API key readOnly flag is invalid")
        raw_ips = result.get("ips")
        if not isinstance(raw_ips, list) or any(not isinstance(ip, str) for ip in raw_ips):
            raise ValueError("Bybit Demo preflight API key IP binding list is invalid")
        return BybitDemoReadOnlyApiKeyInfo(
            read_only=raw_read_only == 1,
            ip_binding_present=any(ip not in {"", "*"} for ip in raw_ips),
        )

    def get_open_positions(self) -> tuple[BybitDemoReadOnlyOpenPosition, ...]:
        page = self._private_get_page(  # noqa: SLF001 - bounded read-only subclass extension.
            path="/v5/position/list",
            query={"category": "linear", "settleCoin": "USDT"},
        )
        if page.next_page_cursor is not None:
            raise RuntimeError("Bybit Demo preflight position read unexpectedly paginated")
        positions: list[BybitDemoReadOnlyOpenPosition] = []
        for row in page.rows:
            size = _decimal(row.get("size"), "position size")
            if size == 0:
                continue
            if size < 0:
                raise ValueError("Bybit Demo preflight position size cannot be negative")
            symbol = row.get("symbol")
            side = row.get("side")
            if not isinstance(symbol, str) or not isinstance(side, str):
                raise ValueError("Bybit Demo preflight position is missing symbol/side")
            position = BybitDemoReadOnlyOpenPosition(
                symbol=symbol,
                side=side,
                size=size,
                average_price=_optional_decimal(row.get("avgPrice"), "average price"),
            )
            position.validate()
            positions.append(position)
        return tuple(positions)

    def has_entry_execution(
        self,
        *,
        symbol: str,
        side: str,
        entry_order_link_id: str,
    ) -> bool:
        if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
            raise ValueError("Bybit Demo preflight execution symbol must be normalized USDT")
        if side not in {"Buy", "Sell"}:
            raise ValueError("Bybit Demo preflight execution side must be Buy or Sell")
        if not entry_order_link_id.startswith("ASTRA-DEMO-"):
            raise ValueError("Bybit Demo preflight execution requires ASTRA-DEMO orderLinkId")
        page = self._private_get_page(  # noqa: SLF001 - bounded read-only subclass extension.
            path="/v5/execution/list",
            query={
                "category": "linear",
                "symbol": symbol,
                "orderLinkId": entry_order_link_id,
                "limit": "100",
            },
        )
        if page.next_page_cursor is not None:
            raise RuntimeError("Bybit Demo preflight execution read unexpectedly paginated")
        found = False
        for row in page.rows:
            if row.get("orderLinkId") != entry_order_link_id:
                continue
            if row.get("symbol") != symbol or row.get("side") != side:
                raise ValueError("Bybit Demo preflight execution identity mismatch")
            quantity = _decimal(row.get("execQty"), "execution quantity")
            if quantity <= 0:
                raise ValueError("Bybit Demo preflight execution quantity must be positive")
            found = True
        return found


@dataclass(frozen=True)
class BybitDemoOperationalDatabaseState:
    required_relations_present: bool
    append_only_triggers_present: bool
    runtime_lease_present: bool
    active_checkpoint_order_link_id: str | None
    active_checkpoint_symbol: str | None
    active_checkpoint_side: str | None
    active_checkpoint_entry_price: Decimal | None
    active_checkpoint_current_quantity: Decimal | None
    approval_record_count: int
    provenance_record_count: int
    terminal_record_count: int

    @property
    def active_checkpoint_present(self) -> bool:
        return self.active_checkpoint_order_link_id is not None


class PostgresBybitDemoOperationalStateReader:
    """Read-only startup state check for v119/v120 Demo runtime tables."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    schema_mutation_supported = False

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("Bybit Demo preflight PostgreSQL DSN is required")
        self._dsn = dsn

    def read_state(self) -> BybitDemoOperationalDatabaseState:
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        with psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    relation_rows = []
                    for relation in _REQUIRED_RELATIONS:
                        cursor.execute("SELECT to_regclass(%s) AS relation", (relation,))
                        relation_rows.append(cursor.fetchone())
                    relations_present = all(
                        row is not None and row["relation"] is not None
                        for row in relation_rows
                    )
                    if not relations_present:
                        return BybitDemoOperationalDatabaseState(
                            required_relations_present=False,
                            append_only_triggers_present=False,
                            runtime_lease_present=False,
                            active_checkpoint_order_link_id=None,
                            active_checkpoint_symbol=None,
                            active_checkpoint_side=None,
                            active_checkpoint_entry_price=None,
                            active_checkpoint_current_quantity=None,
                            approval_record_count=0,
                            provenance_record_count=0,
                            terminal_record_count=0,
                        )
                    cursor.execute(
                        """SELECT count(*) AS count
                           FROM pg_trigger
                           WHERE NOT tgisinternal
                             AND tgname = ANY(%s)""",
                        (list(_REQUIRED_APPEND_ONLY_TRIGGERS),),
                    )
                    trigger_row = cursor.fetchone()
                    trigger_count = 0 if trigger_row is None else int(trigger_row["count"])
                    cursor.execute(
                        """SELECT count(*) AS count
                           FROM astra_bybit_demo_runtime_lease_v119"""
                    )
                    lease_row = cursor.fetchone()
                    lease_count = 0 if lease_row is None else int(lease_row["count"])
                    cursor.execute(
                        """SELECT entry_order_link_id, state_json
                           FROM astra_bybit_demo_active_excursion_v119
                           WHERE checkpoint_name='ACTIVE'"""
                    )
                    checkpoint = cursor.fetchone()
                    cursor.execute(
                        """SELECT count(*) AS count
                           FROM astra_bybit_demo_approved_entry_authorization_v120"""
                    )
                    approval_row = cursor.fetchone()
                    cursor.execute(
                        """SELECT count(*) AS count
                           FROM astra_bybit_demo_entry_provenance_v120"""
                    )
                    provenance_row = cursor.fetchone()
                    cursor.execute(
                        """SELECT count(*) AS count
                           FROM astra_bybit_demo_terminal_evidence_v120"""
                    )
                    terminal_row = cursor.fetchone()
        checkpoint_link = None
        checkpoint_symbol = None
        checkpoint_side = None
        checkpoint_entry_price = None
        checkpoint_current_quantity = None
        if checkpoint is not None:
            checkpoint_link = checkpoint["entry_order_link_id"]
            state = checkpoint["state_json"]
            if (
                not isinstance(checkpoint_link, str)
                or not checkpoint_link.startswith("ASTRA-DEMO-")
            ):
                raise ValueError("Bybit Demo preflight checkpoint orderLinkId is invalid")
            if not isinstance(state, dict):
                raise ValueError("Bybit Demo preflight checkpoint state is invalid")
            checkpoint_symbol = state.get("symbol")
            checkpoint_side = state.get("side")
            if not isinstance(checkpoint_symbol, str) or not isinstance(checkpoint_side, str):
                raise ValueError("Bybit Demo preflight checkpoint identity is incomplete")
            checkpoint_entry_price = _positive_decimal(
                state.get("entry_price"),
                "checkpoint entry price",
            )
            checkpoint_current_quantity = _positive_decimal(
                state.get("current_quantity"),
                "checkpoint current quantity",
            )
        return BybitDemoOperationalDatabaseState(
            required_relations_present=True,
            append_only_triggers_present=(
                trigger_count == len(_REQUIRED_APPEND_ONLY_TRIGGERS)
            ),
            runtime_lease_present=lease_count > 0,
            active_checkpoint_order_link_id=checkpoint_link,
            active_checkpoint_symbol=checkpoint_symbol,
            active_checkpoint_side=checkpoint_side,
            active_checkpoint_entry_price=checkpoint_entry_price,
            active_checkpoint_current_quantity=checkpoint_current_quantity,
            approval_record_count=_count_value(approval_row),
            provenance_record_count=_count_value(provenance_row),
            terminal_record_count=_count_value(terminal_row),
        )


class BybitDemoConnectedPreflightStatus(StrEnum):
    READY_FOR_MANUAL_OPERATOR_APPROVAL = "READY_FOR_MANUAL_OPERATOR_APPROVAL"
    EXISTING_TRADE_MANAGEMENT_REQUIRED = "EXISTING_TRADE_MANAGEMENT_REQUIRED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class BybitDemoConnectedPreflightResult:
    status: BybitDemoConnectedPreflightStatus
    reasons: tuple[str, ...]
    margin_mode: str
    unified_margin_status: int
    positive_equity: bool
    positive_available_balance: bool
    usdt_wallet_visible: bool
    open_position_count: int
    open_position_symbols: tuple[str, ...]
    active_checkpoint_present: bool
    active_checkpoint_symbol: str | None
    runtime_lease_present: bool
    required_relations_present: bool
    append_only_triggers_present: bool
    approval_record_count: int
    provenance_record_count: int
    terminal_record_count: int
    read_only_api_key_verified: bool
    api_key_ip_binding_present: bool
    demo_host_verified: bool = True
    credentials_verified_by_authenticated_reads: bool = True
    preflight_only: bool = True
    trade_actionable: bool = False
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    @property
    def passed(self) -> bool:
        return self.status is not BybitDemoConnectedPreflightStatus.BLOCKED

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "BYBIT_DEMO_CONNECTED_PREFLIGHT_V1",
            "status": self.status.value,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "account": {
                "margin_mode": self.margin_mode,
                "unified_margin_status": self.unified_margin_status,
                "positive_equity": self.positive_equity,
                "positive_available_balance": self.positive_available_balance,
                "usdt_wallet_visible": self.usdt_wallet_visible,
                "open_position_count": self.open_position_count,
                "open_position_symbols": list(self.open_position_symbols),
            },
            "credential": {
                "read_only_api_key_verified": self.read_only_api_key_verified,
                "ip_binding_present": self.api_key_ip_binding_present,
            },
            "durable_state": {
                "active_checkpoint_present": self.active_checkpoint_present,
                "active_checkpoint_symbol": self.active_checkpoint_symbol,
                "runtime_lease_present": self.runtime_lease_present,
                "required_relations_present": self.required_relations_present,
                "append_only_triggers_present": self.append_only_triggers_present,
                "approval_record_count": self.approval_record_count,
                "provenance_record_count": self.provenance_record_count,
                "terminal_record_count": self.terminal_record_count,
            },
            "demo_host_verified": self.demo_host_verified,
            "credentials_verified_by_authenticated_reads": (
                self.credentials_verified_by_authenticated_reads
            ),
            "preflight_only": self.preflight_only,
            "trade_actionable": self.trade_actionable,
            "order_writes_supported": self.order_writes_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


def run_bybit_demo_connected_preflight(
    account_client: BybitDemoPreflightAccountClient,
    database_reader: PostgresBybitDemoOperationalStateReader,
) -> BybitDemoConnectedPreflightResult:
    _validate_read_only_dependencies(account_client, database_reader)
    key_info = account_client.get_api_key_info()
    wallet = account_client.get_wallet_balance()
    account = account_client.get_account_info()
    positions = account_client.get_open_positions()
    database = database_reader.read_state()

    reasons: list[str] = []
    if not key_info.read_only:
        reasons.append("DEMO_API_KEY_IS_NOT_READ_ONLY")
    if not database.required_relations_present:
        reasons.append("DEMO_POSTGRES_V119_V120_SCHEMA_NOT_READY")
    if not database.append_only_triggers_present:
        reasons.append("DEMO_POSTGRES_V120_APPEND_ONLY_TRIGGERS_NOT_READY")
    if database.runtime_lease_present:
        reasons.append("DEMO_CANONICAL_RUNTIME_LEASE_PRESENT")
    if len(positions) > 1:
        reasons.append("DEMO_MULTIPLE_OPEN_POSITIONS_NOT_SUPPORTED")

    checkpoint = database.active_checkpoint_present
    if not positions and checkpoint:
        reasons.append("DEMO_CHECKPOINT_WITHOUT_EXCHANGE_POSITION")
    elif positions and not checkpoint:
        reasons.append("DEMO_EXCHANGE_POSITION_WITHOUT_CHECKPOINT")
    elif len(positions) == 1 and checkpoint:
        position = positions[0]
        expected_side = "LONG" if position.side == "Buy" else "SHORT"
        if position.symbol != database.active_checkpoint_symbol:
            reasons.append("DEMO_POSITION_CHECKPOINT_SYMBOL_MISMATCH")
        if expected_side != database.active_checkpoint_side:
            reasons.append("DEMO_POSITION_CHECKPOINT_SIDE_MISMATCH")
        if position.size != database.active_checkpoint_current_quantity:
            reasons.append("DEMO_POSITION_CHECKPOINT_QUANTITY_MISMATCH")
        if position.average_price != database.active_checkpoint_entry_price:
            reasons.append("DEMO_POSITION_CHECKPOINT_ENTRY_PRICE_MISMATCH")
        if (
            position.symbol == database.active_checkpoint_symbol
            and expected_side == database.active_checkpoint_side
            and database.active_checkpoint_order_link_id is not None
            and not account_client.has_entry_execution(
                symbol=position.symbol,
                side=position.side,
                entry_order_link_id=database.active_checkpoint_order_link_id,
            )
        ):
            reasons.append("DEMO_CHECKPOINT_ENTRY_EXECUTION_NOT_FOUND")

    if reasons:
        status = BybitDemoConnectedPreflightStatus.BLOCKED
    elif positions:
        status = BybitDemoConnectedPreflightStatus.EXISTING_TRADE_MANAGEMENT_REQUIRED
    else:
        status = BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL

    return BybitDemoConnectedPreflightResult(
        status=status,
        reasons=tuple(reasons),
        margin_mode=account.margin_mode,
        unified_margin_status=account.unified_margin_status,
        positive_equity=wallet.total_equity_usd > 0,
        positive_available_balance=wallet.total_available_balance_usd > 0,
        usdt_wallet_visible=wallet.usdt_wallet_balance is not None,
        open_position_count=len(positions),
        open_position_symbols=tuple(position.symbol for position in positions),
        active_checkpoint_present=checkpoint,
        active_checkpoint_symbol=database.active_checkpoint_symbol,
        runtime_lease_present=database.runtime_lease_present,
        required_relations_present=database.required_relations_present,
        append_only_triggers_present=database.append_only_triggers_present,
        approval_record_count=database.approval_record_count,
        provenance_record_count=database.provenance_record_count,
        terminal_record_count=database.terminal_record_count,
        read_only_api_key_verified=key_info.read_only,
        api_key_ip_binding_present=key_info.ip_binding_present,
    )


def _validate_read_only_dependencies(
    account_client: BybitDemoPreflightAccountClient,
    database_reader: PostgresBybitDemoOperationalStateReader,
) -> None:
    if account_client.host != _DEMO_HOST:
        raise ValueError("Bybit Demo preflight rejected non-demo account host")
    if account_client.live_mainnet_order_routing_allowed:
        raise ValueError("Bybit Demo preflight rejected mainnet-capable account client")
    if account_client.order_writes_supported:
        raise ValueError("Bybit Demo preflight account client cannot support order writes")
    if hasattr(account_client, "place_order") or hasattr(account_client, "cancel_order"):
        raise ValueError("Bybit Demo preflight account client exposes mutation methods")
    if database_reader.live_mainnet_order_routing_allowed:
        raise ValueError("Bybit Demo preflight rejected mainnet-capable database reader")
    if database_reader.order_writes_supported or database_reader.schema_mutation_supported:
        raise ValueError("Bybit Demo preflight database reader must remain read-only")


def _count_value(row: Any) -> int:
    if row is None:
        return 0
    return int(row["count"])


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Bybit Demo preflight {label} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"Bybit Demo preflight {label} must be finite")
    return parsed


def _positive_decimal(value: object, label: str) -> Decimal:
    parsed = _decimal(value, label)
    if parsed <= 0:
        raise ValueError(f"Bybit Demo preflight {label} must be positive")
    return parsed


def _optional_decimal(value: object, label: str) -> Decimal | None:
    if value in (None, ""):
        return None
    return _decimal(value, label)


__all__ = [
    "BybitDemoConnectedPreflightResult",
    "BybitDemoConnectedPreflightStatus",
    "BybitDemoOperationalDatabaseState",
    "BybitDemoPreflightAccountClient",
    "BybitDemoReadOnlyApiKeyInfo",
    "BybitDemoReadOnlyOpenPosition",
    "PostgresBybitDemoOperationalStateReader",
    "run_bybit_demo_connected_preflight",
]
