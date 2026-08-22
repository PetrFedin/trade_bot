from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.execution.bybit_mainnet_clock_preflight import BybitMainnetClockPreflight
from app.runtime.bybit_mainnet_readonly_probe import BybitMainnetReadOnlySnapshot

_ZERO = Decimal("0")


@dataclass(frozen=True)
class BybitMainnetReadOnlyHealth:
    api_host: str
    api_key_fingerprint_sha256: str
    clock_preflight: BybitMainnetClockPreflight
    total_equity_usd: Decimal
    total_wallet_balance_usd: Decimal
    total_available_balance_usd: Decimal
    total_margin_balance_usd: Decimal
    total_initial_margin_usd: Decimal
    total_maintenance_margin_usd: Decimal
    total_perp_upl_usd: Decimal
    available_balance_ratio: Decimal | None
    initial_margin_ratio: Decimal | None
    maintenance_margin_ratio: Decimal | None
    open_position_count: int
    gross_position_value_usd: Decimal | None
    open_position_unrealised_pnl_usd: Decimal | None
    read_only_verified: bool
    ip_binding_verified: bool
    environment: str = "BYBIT_MAINNET_READONLY"
    live_mainnet_order_routing_allowed: bool = False
    order_writes_supported: bool = False

    @property
    def ready(self) -> bool:
        return (
            self.clock_preflight.ready
            and self.read_only_verified
            and self.ip_binding_verified
            and self.total_equity_usd >= _ZERO
            and self.environment == "BYBIT_MAINNET_READONLY"
            and not self.live_mainnet_order_routing_allowed
            and not self.order_writes_supported
        )

    @property
    def reasons(self) -> tuple[str, ...]:
        reasons = list(self.clock_preflight.reasons)
        if not self.read_only_verified:
            reasons.append("BYBIT_MAINNET_API_KEY_NOT_READ_ONLY")
        if not self.ip_binding_verified:
            reasons.append("BYBIT_MAINNET_API_KEY_IP_BINDING_UNVERIFIED")
        return tuple(reasons)

    def validate(self) -> None:
        self.clock_preflight.validate()
        if self.clock_preflight.api_host != self.api_host:
            raise ValueError("Bybit mainnet health host must match clock-preflight host")
        if len(self.api_key_fingerprint_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.api_key_fingerprint_sha256
        ):
            raise ValueError("Bybit mainnet health API-key fingerprint must be sha256 hex")
        for name, value in (
            ("total_equity_usd", self.total_equity_usd),
            ("total_wallet_balance_usd", self.total_wallet_balance_usd),
            ("total_available_balance_usd", self.total_available_balance_usd),
            ("total_margin_balance_usd", self.total_margin_balance_usd),
            ("total_initial_margin_usd", self.total_initial_margin_usd),
            ("total_maintenance_margin_usd", self.total_maintenance_margin_usd),
            ("total_perp_upl_usd", self.total_perp_upl_usd),
        ):
            if not value.is_finite():
                raise ValueError(f"Bybit mainnet health {name} must be finite")
        if self.total_equity_usd < _ZERO:
            raise ValueError("Bybit mainnet health total equity cannot be negative")
        if self.total_initial_margin_usd < _ZERO or self.total_maintenance_margin_usd < _ZERO:
            raise ValueError("Bybit mainnet health margin requirements cannot be negative")
        for name, ratio in (
            ("available_balance_ratio", self.available_balance_ratio),
            ("initial_margin_ratio", self.initial_margin_ratio),
            ("maintenance_margin_ratio", self.maintenance_margin_ratio),
        ):
            if ratio is not None and (not ratio.is_finite() or ratio < _ZERO):
                raise ValueError(f"Bybit mainnet health {name} must be non-negative and finite")
        if isinstance(self.open_position_count, bool) or self.open_position_count < 0:
            raise ValueError("Bybit mainnet health open-position count must be non-negative")
        if self.gross_position_value_usd is not None and (
            not self.gross_position_value_usd.is_finite() or self.gross_position_value_usd < _ZERO
        ):
            raise ValueError("Bybit mainnet health gross position value must be non-negative")
        if self.open_position_unrealised_pnl_usd is not None and (
            not self.open_position_unrealised_pnl_usd.is_finite()
        ):
            raise ValueError("Bybit mainnet health open-position UPL must be finite")
        if self.environment != "BYBIT_MAINNET_READONLY":
            raise ValueError("Bybit mainnet health environment is invalid")
        if self.live_mainnet_order_routing_allowed or self.order_writes_supported:
            raise ValueError("Bybit mainnet health cannot grant order writes")

    def to_safe_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "ready": self.ready,
            "reasons": self.reasons,
            "environment": self.environment,
            "api_host": self.api_host,
            "api_key_fingerprint_sha256": self.api_key_fingerprint_sha256,
            "read_only_verified": self.read_only_verified,
            "ip_binding_verified": self.ip_binding_verified,
            "live_mainnet_order_routing_allowed": False,
            "order_writes_supported": False,
            "clock": self.clock_preflight.to_safe_dict(),
            "account": {
                "total_equity_usd": _decimal_text(self.total_equity_usd),
                "total_wallet_balance_usd": _decimal_text(self.total_wallet_balance_usd),
                "total_available_balance_usd": _decimal_text(self.total_available_balance_usd),
                "total_margin_balance_usd": _decimal_text(self.total_margin_balance_usd),
                "total_initial_margin_usd": _decimal_text(self.total_initial_margin_usd),
                "total_maintenance_margin_usd": _decimal_text(
                    self.total_maintenance_margin_usd
                ),
                "total_perp_upl_usd": _decimal_text(self.total_perp_upl_usd),
                "available_balance_ratio": _optional_decimal_text(
                    self.available_balance_ratio
                ),
                "initial_margin_ratio": _optional_decimal_text(self.initial_margin_ratio),
                "maintenance_margin_ratio": _optional_decimal_text(
                    self.maintenance_margin_ratio
                ),
            },
            "positions": {
                "open_position_count": self.open_position_count,
                "gross_position_value_usd": _optional_decimal_text(
                    self.gross_position_value_usd
                ),
                "open_position_unrealised_pnl_usd": _optional_decimal_text(
                    self.open_position_unrealised_pnl_usd
                ),
            },
        }


def build_bybit_mainnet_readonly_health(
    *,
    clock_preflight: BybitMainnetClockPreflight,
    snapshot: BybitMainnetReadOnlySnapshot,
) -> BybitMainnetReadOnlyHealth:
    clock_preflight.validate()
    snapshot.validate()
    if clock_preflight.api_host != snapshot.api_host:
        raise ValueError("Bybit mainnet clock/account snapshots came from different hosts")
    wallet = snapshot.wallet
    position_values = [position.position_value for position in snapshot.positions]
    position_upl = [position.unrealised_pnl for position in snapshot.positions]
    gross_position_value = (
        None
        if any(value is None for value in position_values)
        else sum((abs(value) for value in position_values if value is not None), start=_ZERO)
    )
    open_position_upl = (
        None
        if any(value is None for value in position_upl)
        else sum((value for value in position_upl if value is not None), start=_ZERO)
    )
    health = BybitMainnetReadOnlyHealth(
        api_host=snapshot.api_host,
        api_key_fingerprint_sha256=snapshot.api_key.key_fingerprint_sha256,
        clock_preflight=clock_preflight,
        total_equity_usd=wallet.total_equity_usd,
        total_wallet_balance_usd=wallet.total_wallet_balance_usd,
        total_available_balance_usd=wallet.total_available_balance_usd,
        total_margin_balance_usd=wallet.total_margin_balance_usd,
        total_initial_margin_usd=wallet.total_initial_margin_usd,
        total_maintenance_margin_usd=wallet.total_maintenance_margin_usd,
        total_perp_upl_usd=wallet.total_perp_upl_usd,
        available_balance_ratio=_safe_ratio(
            wallet.total_available_balance_usd,
            wallet.total_margin_balance_usd,
        ),
        initial_margin_ratio=_safe_ratio(
            wallet.total_initial_margin_usd,
            wallet.total_margin_balance_usd,
        ),
        maintenance_margin_ratio=_safe_ratio(
            wallet.total_maintenance_margin_usd,
            wallet.total_margin_balance_usd,
        ),
        open_position_count=len(snapshot.positions),
        gross_position_value_usd=gross_position_value,
        open_position_unrealised_pnl_usd=open_position_upl,
        read_only_verified=snapshot.api_key.read_only,
        ip_binding_verified=bool(snapshot.api_key.ip_bindings),
    )
    health.validate()
    return health


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= _ZERO:
        return None
    return numerator / denominator


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)
