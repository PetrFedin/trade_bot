from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

_BYBIT_DEMO_REST = "https://api-demo.bybit.com"
_BYBIT_DEMO_PRIVATE_WS = "wss://stream-demo.bybit.com/v5/private"
_BYBIT_PUBLIC_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"
_ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class BybitProductConfigError(ValueError):
    """Raised when runtime configuration violates the production safety boundary."""


@dataclass(frozen=True)
class BybitProductConfig:
    """Canonical environment contract for the Bybit product runtime.

    The currently qualified broker adapter is demo-only. This object deliberately refuses any
    mainnet/private-trading endpoint so live routing cannot be enabled through ordinary config.
    A future real-money adapter requires a separate audited code path and release decision.
    """

    environment: str
    broker: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    database_url: str = field(repr=False)
    rest_url: str = _BYBIT_DEMO_REST
    private_ws_url: str = _BYBIT_DEMO_PRIVATE_WS
    public_ws_url: str = _BYBIT_PUBLIC_LINEAR_WS
    trading_writes_enabled: bool = False
    mainnet_enabled: bool = False
    poll_interval_ms: int = 1000
    shutdown_grace_seconds: int = 15
    log_level: str = "INFO"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        require_credentials: bool = True,
        require_database: bool = True,
    ) -> BybitProductConfig:
        source = os.environ if env is None else env
        api_key = source.get("BYBIT_API_KEY", "").strip()
        api_secret = source.get("BYBIT_API_SECRET", "").strip()
        database_url = source.get("DATABASE_URL", "").strip()
        if require_credentials and (not api_key or not api_secret):
            raise BybitProductConfigError("BYBIT_API_KEY and BYBIT_API_SECRET are required")
        if require_database and not database_url:
            raise BybitProductConfigError("DATABASE_URL is required")

        config = cls(
            environment=source.get("ASTRA_ENV", "demo").strip().lower(),
            broker=source.get("ASTRA_BROKER", "bybit").strip().lower(),
            api_key=api_key,
            api_secret=api_secret,
            database_url=database_url,
            rest_url=source.get("BYBIT_REST_URL", _BYBIT_DEMO_REST).strip(),
            private_ws_url=source.get(
                "BYBIT_PRIVATE_WS_URL", _BYBIT_DEMO_PRIVATE_WS
            ).strip(),
            public_ws_url=source.get("BYBIT_PUBLIC_WS_URL", _BYBIT_PUBLIC_LINEAR_WS).strip(),
            trading_writes_enabled=_parse_bool(
                source.get("TRADING_WRITES_ENABLED", "false"),
                name="TRADING_WRITES_ENABLED",
            ),
            mainnet_enabled=_parse_bool(
                source.get("MAINNET_ENABLED", "false"),
                name="MAINNET_ENABLED",
            ),
            poll_interval_ms=_parse_int(
                source.get("ASTRA_POLL_INTERVAL_MS", "1000"),
                name="ASTRA_POLL_INTERVAL_MS",
                minimum=100,
                maximum=60_000,
            ),
            shutdown_grace_seconds=_parse_int(
                source.get("ASTRA_SHUTDOWN_GRACE_SECONDS", "15"),
                name="ASTRA_SHUTDOWN_GRACE_SECONDS",
                minimum=1,
                maximum=300,
            ),
            log_level=source.get("LOG_LEVEL", "INFO").strip().upper(),
        )
        config.validate(
            require_credentials=require_credentials,
            require_database=require_database,
        )
        return config

    def validate(
        self,
        *,
        require_credentials: bool = True,
        require_database: bool = True,
    ) -> None:
        if self.environment != "demo":
            raise BybitProductConfigError(
                "only ASTRA_ENV=demo is qualified; real-money promotion requires a separate adapter"
            )
        if self.broker != "bybit":
            raise BybitProductConfigError("ASTRA_BROKER must be bybit for the Bybit product runtime")
        if self.mainnet_enabled:
            raise BybitProductConfigError("MAINNET_ENABLED must remain false")
        if self.rest_url != _BYBIT_DEMO_REST:
            raise BybitProductConfigError("Bybit REST endpoint must remain api-demo.bybit.com")
        if self.private_ws_url != _BYBIT_DEMO_PRIVATE_WS:
            raise BybitProductConfigError(
                "Bybit private WebSocket endpoint must remain stream-demo.bybit.com/v5/private"
            )
        if self.public_ws_url != _BYBIT_PUBLIC_LINEAR_WS:
            raise BybitProductConfigError(
                "Bybit public linear WebSocket endpoint must remain stream.bybit.com/v5/public/linear"
            )
        if require_credentials and (not self.api_key.strip() or not self.api_secret.strip()):
            raise BybitProductConfigError("Bybit credentials are required")
        if require_database:
            _validate_postgres_url(self.database_url)
        elif self.database_url:
            _validate_postgres_url(self.database_url)
        if isinstance(self.poll_interval_ms, bool) or not 100 <= self.poll_interval_ms <= 60_000:
            raise BybitProductConfigError("poll interval must be within [100, 60000] ms")
        if (
            isinstance(self.shutdown_grace_seconds, bool)
            or not 1 <= self.shutdown_grace_seconds <= 300
        ):
            raise BybitProductConfigError("shutdown grace must be within [1, 300] seconds")
        if self.log_level not in _ALLOWED_LOG_LEVELS:
            raise BybitProductConfigError("LOG_LEVEL is invalid")

    @property
    def live_mainnet_order_routing_allowed(self) -> bool:
        return False

    @property
    def demo_order_writes_allowed(self) -> bool:
        return self.trading_writes_enabled

    def redacted(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "environment": self.environment,
                "broker": self.broker,
                "database_configured": bool(self.database_url),
                "rest_url": self.rest_url,
                "private_ws_url": self.private_ws_url,
                "public_ws_url": self.public_ws_url,
                "trading_writes_enabled": self.trading_writes_enabled,
                "mainnet_enabled": self.mainnet_enabled,
                "poll_interval_ms": self.poll_interval_ms,
                "shutdown_grace_seconds": self.shutdown_grace_seconds,
                "log_level": self.log_level,
                "credentials_configured": bool(self.api_key and self.api_secret),
                "live_mainnet_order_routing_allowed": False,
            }
        )


def _parse_bool(raw: str, *, name: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise BybitProductConfigError(f"{name} must be an explicit boolean")


def _parse_int(raw: str, *, name: str, minimum: int, maximum: int) -> int:
    try:
        value = int(raw.strip())
    except (TypeError, ValueError) as exc:
        raise BybitProductConfigError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise BybitProductConfigError(f"{name} must be within [{minimum}, {maximum}]")
    return value


def _validate_postgres_url(database_url: str) -> None:
    if not database_url.strip():
        raise BybitProductConfigError("DATABASE_URL is required")
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise BybitProductConfigError("DATABASE_URL must use PostgreSQL")
    if not parsed.hostname or not parsed.path or parsed.path == "/":
        raise BybitProductConfigError("DATABASE_URL must include host and database name")
