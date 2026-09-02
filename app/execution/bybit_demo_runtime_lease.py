from __future__ import annotations

from dataclasses import dataclass


_OWNER_TOKEN_LENGTH = 64
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class BybitDemoRuntimeLease:
    """Durable identity of the single canonical Bybit Demo writer runtime.

    This record is intentionally strategy- and broker-independent. It carries only the
    ownership identity and fail-closed capability flags needed by the durable lease layer.
    It does not expire automatically and it cannot authorize an order.
    """

    owner_token: str
    created_time_ms: int
    process_id: int
    automatic_stale_takeover_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False
    order_writes_supported: bool = False

    def __post_init__(self) -> None:
        validate_owner_token(self.owner_token)
        if (
            isinstance(self.created_time_ms, bool)
            or not isinstance(self.created_time_ms, int)
            or self.created_time_ms < 0
        ):
            raise ValueError("demo runtime lease created time must be a non-negative integer")
        if (
            isinstance(self.process_id, bool)
            or not isinstance(self.process_id, int)
            or self.process_id <= 0
        ):
            raise ValueError("demo runtime lease process id must be a positive integer")
        if self.automatic_stale_takeover_allowed is not False:
            raise ValueError("demo runtime lease cannot allow automatic stale takeover")
        if self.live_mainnet_order_routing_allowed is not False:
            raise ValueError("demo runtime lease cannot permit live mainnet routing")
        if self.order_writes_supported is not False:
            raise ValueError("runtime lease contract cannot itself support order writes")


def validate_owner_token(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _OWNER_TOKEN_LENGTH
        or any(character not in _HEX for character in value)
    ):
        raise ValueError("demo runtime lease owner token must be 32-byte lowercase hex")


__all__ = ["BybitDemoRuntimeLease", "validate_owner_token"]
