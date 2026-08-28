from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo_account_reader import BybitDemoAccountingClient
from app.execution.bybit_rest_policy import BybitRestProtocolError


@dataclass(frozen=True)
class BybitDemoApiAccountIdentity:
    user_id: int
    parent_uid: int
    is_master: bool

    def validate(self) -> None:
        if isinstance(self.user_id, bool) or self.user_id <= 0:
            raise ValueError("Bybit Demo account identity user ID must be positive")
        if isinstance(self.parent_uid, bool) or self.parent_uid < 0:
            raise ValueError("Bybit Demo account identity parent UID cannot be negative")
        if not isinstance(self.is_master, bool):
            raise ValueError("Bybit Demo account identity master flag must be boolean")
        if self.is_master and self.parent_uid != 0:
            raise ValueError("Bybit Demo master account identity cannot have parent UID")
        if not self.is_master and self.parent_uid == 0:
            raise ValueError("Bybit Demo sub-account identity requires parent UID")


class BybitDemoSameAccountStatus(StrEnum):
    VERIFIED_SAME_ACCOUNT = "VERIFIED_SAME_ACCOUNT"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class BybitDemoSameAccountProof:
    status: BybitDemoSameAccountStatus
    reasons: tuple[str, ...]
    same_user_id: bool
    same_parent_uid: bool
    same_master_scope: bool
    authenticated_get_only: bool = True
    order_write_performed: bool = False
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    @property
    def passed(self) -> bool:
        return self.status is BybitDemoSameAccountStatus.VERIFIED_SAME_ACCOUNT

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "BYBIT_DEMO_SAME_ACCOUNT_PREFLIGHT_V1",
            "status": self.status.value,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "same_user_id": self.same_user_id,
            "same_parent_uid": self.same_parent_uid,
            "same_master_scope": self.same_master_scope,
            "authenticated_get_only": self.authenticated_get_only,
            "order_write_performed": self.order_write_performed,
            "order_writes_supported": self.order_writes_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


class BybitDemoAccountIdentityInspector(BybitDemoAccountingClient):
    """Read one API key's Demo account identity through authenticated GET only."""

    environment = "BYBIT_DEMO_ACCOUNT_IDENTITY_PREFLIGHT"
    authenticated_get_only = True
    order_submission_supported = False

    def inspect(self) -> BybitDemoApiAccountIdentity:
        result = self._private_get_result(  # noqa: SLF001 - bounded GET-only identity read.
            path="/v5/user/query-api",
            query={},
        )
        returned_key = result.get("apiKey")
        if not isinstance(returned_key, str) or not returned_key:
            raise BybitRestProtocolError(
                "Bybit Demo account identity response is missing API-key identity",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        if not hmac.compare_digest(returned_key, self._api_key):  # noqa: SLF001
            raise BybitRestProtocolError(
                "Bybit Demo account identity API key does not match configured credential",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        secret_marker = result.get("secret")
        if not isinstance(secret_marker, str) or secret_marker:
            raise BybitRestProtocolError(
                "Bybit Demo account identity response has invalid secret marker",
                retryable_read=False,
                ambiguous_mutation=False,
            )

        identity = BybitDemoApiAccountIdentity(
            user_id=_positive_int(result.get("userID"), field="userID"),
            parent_uid=_non_negative_int(result.get("parentUid"), field="parentUid"),
            is_master=_strict_bool(result.get("isMaster"), field="isMaster"),
        )
        identity.validate()
        return identity


def prove_same_bybit_demo_account(
    readonly_inspector: BybitDemoAccountIdentityInspector,
    trading_inspector: BybitDemoAccountIdentityInspector,
) -> BybitDemoSameAccountProof:
    """Prove read-only accounting and write-capable key resolve to one exact Demo account."""

    _validate_inspector(readonly_inspector, label="read-only")
    _validate_inspector(trading_inspector, label="trading")
    readonly = readonly_inspector.inspect()
    trading = trading_inspector.inspect()

    same_user = readonly.user_id == trading.user_id
    same_parent = readonly.parent_uid == trading.parent_uid
    same_master = readonly.is_master is trading.is_master
    reasons: list[str] = []
    if not same_user:
        reasons.append("DEMO_CREDENTIAL_USER_ID_MISMATCH")
    if not same_parent:
        reasons.append("DEMO_CREDENTIAL_PARENT_UID_MISMATCH")
    if not same_master:
        reasons.append("DEMO_CREDENTIAL_MASTER_SCOPE_MISMATCH")
    status = (
        BybitDemoSameAccountStatus.VERIFIED_SAME_ACCOUNT
        if not reasons
        else BybitDemoSameAccountStatus.BLOCKED
    )
    return BybitDemoSameAccountProof(
        status=status,
        reasons=tuple(reasons),
        same_user_id=same_user,
        same_parent_uid=same_parent,
        same_master_scope=same_master,
    )


def require_same_bybit_demo_account(
    readonly_inspector: BybitDemoAccountIdentityInspector,
    trading_inspector: BybitDemoAccountIdentityInspector,
) -> BybitDemoSameAccountProof:
    proof = prove_same_bybit_demo_account(readonly_inspector, trading_inspector)
    if not proof.passed:
        raise RuntimeError(
            "Bybit Demo read-only and trading credentials belong to different accounts"
        )
    return proof


def _validate_inspector(client: Any, *, label: str) -> None:
    if not isinstance(client, BybitDemoAccountIdentityInspector):
        raise ValueError(f"Bybit Demo {label} account identity requires exact inspector")
    if getattr(client, "host", None) != "api-demo.bybit.com":
        raise ValueError(f"Bybit Demo {label} account identity rejected non-demo host")
    if getattr(client, "authenticated_get_only", False) is not True:
        raise ValueError(f"Bybit Demo {label} account identity must be GET-only")
    if getattr(client, "order_writes_supported", True) is not False:
        raise ValueError(f"Bybit Demo {label} account identity cannot support order writes")
    if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError(f"Bybit Demo {label} account identity cannot route mainnet orders")
    for name in ("place_order", "place_market_order", "cancel_order", "amend_order"):
        if callable(getattr(client, name, None)):
            raise ValueError(f"Bybit Demo {label} account identity exposes mutation method")


def _positive_int(value: Any, *, field: str) -> int:
    parsed = _integer(value, field=field)
    if parsed <= 0:
        raise BybitRestProtocolError(
            f"Bybit Demo account identity returned invalid {field}",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return parsed


def _non_negative_int(value: Any, *, field: str) -> int:
    parsed = _integer(value, field=field)
    if parsed < 0:
        raise BybitRestProtocolError(
            f"Bybit Demo account identity returned invalid {field}",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return parsed


def _integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise BybitRestProtocolError(
            f"Bybit Demo account identity returned invalid {field}",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value and value.isdigit():
        return int(value)
    raise BybitRestProtocolError(
        f"Bybit Demo account identity returned invalid {field}",
        retryable_read=False,
        ambiguous_mutation=False,
    )


def _strict_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise BybitRestProtocolError(
            f"Bybit Demo account identity returned invalid {field}",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return value


__all__ = [
    "BybitDemoAccountIdentityInspector",
    "BybitDemoApiAccountIdentity",
    "BybitDemoSameAccountProof",
    "BybitDemoSameAccountStatus",
    "prove_same_bybit_demo_account",
    "require_same_bybit_demo_account",
]
