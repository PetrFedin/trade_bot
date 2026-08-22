from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from app.execution.bybit_mainnet_readonly import (
    BybitMainnetAccountInfo,
    BybitMainnetApiKeyInfo,
    BybitMainnetPosition,
    BybitMainnetReadOnlyClient,
    BybitMainnetWalletBalance,
)

_API_KEY_ENV = "BYBIT_MAINNET_READONLY_API_KEY"
_API_SECRET_ENV = "BYBIT_MAINNET_READONLY_API_SECRET"


class BybitMainnetReadOnlyConfigError(ValueError):
    """Raised when the isolated mainnet read-only credential boundary is misconfigured."""


@dataclass(frozen=True)
class BybitMainnetReadOnlyCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> BybitMainnetReadOnlyCredentials:
        source = os.environ if env is None else env
        api_key = source.get(_API_KEY_ENV, "")
        api_secret = source.get(_API_SECRET_ENV, "")
        _validate_secret_env_value(api_key, name=_API_KEY_ENV)
        _validate_secret_env_value(api_secret, name=_API_SECRET_ENV)
        return cls(api_key=api_key, api_secret=api_secret)

    def build_client(self) -> BybitMainnetReadOnlyClient:
        return BybitMainnetReadOnlyClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
        )


@dataclass(frozen=True)
class BybitMainnetReadOnlySnapshot:
    api_key: BybitMainnetApiKeyInfo
    account: BybitMainnetAccountInfo
    wallet: BybitMainnetWalletBalance
    positions: tuple[BybitMainnetPosition, ...]
    environment: str = "BYBIT_MAINNET_READONLY"
    live_mainnet_order_routing_allowed: bool = False
    order_writes_supported: bool = False

    def validate(self) -> None:
        self.api_key.validate()
        self.account.validate()
        self.wallet.validate()
        for position in self.positions:
            position.validate()
        if self.environment != "BYBIT_MAINNET_READONLY":
            raise ValueError("Bybit mainnet read-only snapshot environment is invalid")
        if self.live_mainnet_order_routing_allowed or self.order_writes_supported:
            raise ValueError("Bybit mainnet read-only snapshot cannot grant order writes")

    def to_safe_dict(self) -> dict[str, object]:
        """Return operational state without raw API credentials or secret material."""

        self.validate()
        return {
            "environment": self.environment,
            "live_mainnet_order_routing_allowed": False,
            "order_writes_supported": False,
            "credential_safety": {
                "read_only_verified": self.api_key.read_only,
                "ip_binding_verified": bool(self.api_key.ip_bindings),
                "ip_binding_count": len(self.api_key.ip_bindings),
                "api_key_fingerprint_sha256": self.api_key.key_fingerprint_sha256,
                "key_type": self.api_key.key_type,
                "note": self.api_key.note,
                "permissions": self.api_key.permissions,
            },
            "account": {
                "margin_mode": self.account.margin_mode,
                "unified_margin_status": self.account.unified_margin_status,
                "updated_time_ms": self.account.updated_time_ms,
            },
            "wallet": {
                "total_equity_usd": _decimal_text(self.wallet.total_equity_usd),
                "total_wallet_balance_usd": _decimal_text(
                    self.wallet.total_wallet_balance_usd
                ),
                "total_margin_balance_usd": _decimal_text(
                    self.wallet.total_margin_balance_usd
                ),
                "total_available_balance_usd": _decimal_text(
                    self.wallet.total_available_balance_usd
                ),
                "total_perp_upl_usd": _decimal_text(self.wallet.total_perp_upl_usd),
                "total_initial_margin_usd": _decimal_text(
                    self.wallet.total_initial_margin_usd
                ),
                "total_maintenance_margin_usd": _decimal_text(
                    self.wallet.total_maintenance_margin_usd
                ),
                "usdt_wallet_balance": _optional_decimal_text(
                    self.wallet.usdt_wallet_balance
                ),
            },
            "positions": [
                {
                    "symbol": position.symbol,
                    "side": position.side,
                    "size": _decimal_text(position.size),
                    "position_idx": position.position_idx,
                    "average_price": _optional_decimal_text(position.average_price),
                    "mark_price": _optional_decimal_text(position.mark_price),
                    "position_value": _optional_decimal_text(position.position_value),
                    "unrealised_pnl": _optional_decimal_text(position.unrealised_pnl),
                    "liquidation_price": _optional_decimal_text(
                        position.liquidation_price
                    ),
                    "leverage": _optional_decimal_text(position.leverage),
                }
                for position in self.positions
            ],
        }


def probe_bybit_mainnet_readonly_connection(
    client: BybitMainnetReadOnlyClient,
) -> BybitMainnetReadOnlySnapshot:
    """Prove credential safety first, then take a consistent read-only account snapshot."""

    if client.live_mainnet_order_routing_allowed or client.order_writes_supported:
        raise RuntimeError("Bybit mainnet probe rejected a mutation-capable client")
    api_key = client.verify_read_only_api_key(require_ip_binding=True)
    account = client.get_account_info()
    wallet = client.get_wallet_balance()
    positions = client.get_positions(settle_coin="USDT")
    snapshot = BybitMainnetReadOnlySnapshot(
        api_key=api_key,
        account=account,
        wallet=wallet,
        positions=positions,
    )
    snapshot.validate()
    return snapshot


def build_bybit_mainnet_readonly_client_from_env(
    env: Mapping[str, str] | None = None,
) -> BybitMainnetReadOnlyClient:
    return BybitMainnetReadOnlyCredentials.from_env(env).build_client()


def main() -> int:
    client = build_bybit_mainnet_readonly_client_from_env()
    snapshot = probe_bybit_mainnet_readonly_connection(client)
    print(json.dumps(snapshot.to_safe_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def _validate_secret_env_value(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise BybitMainnetReadOnlyConfigError(f"{name} is required")
    if value != value.strip() or any(character in value for character in "\r\n\t"):
        raise BybitMainnetReadOnlyConfigError(
            f"{name} contains surrounding or control whitespace"
        )
    if value.lower() in {"changeme", "placeholder", "your_api_key", "your_api_secret"}:
        raise BybitMainnetReadOnlyConfigError(f"{name} cannot use a placeholder")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


if __name__ == "__main__":
    raise SystemExit(main())
