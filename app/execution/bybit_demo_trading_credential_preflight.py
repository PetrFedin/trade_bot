from __future__ import annotations

import hashlib
import hmac
import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo_account_reader import BybitDemoAccountingClient
from app.execution.bybit_rest_policy import BybitRestProtocolError

_REQUIRED_CONTRACT_PERMISSIONS = frozenset({"Order", "Position"})


class BybitDemoTradingCredentialPreflightStatus(StrEnum):
    READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL = (
        "READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL"
    )
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class BybitDemoTradingApiKeyInfo:
    read_write_enabled: bool
    ip_binding_present: bool
    ip_binding_count: int
    personal_key_type: bool
    uta_enabled: bool
    contract_order_permission: bool
    contract_position_permission: bool
    least_privilege_contract_only: bool
    authenticated_get_only: bool = True
    order_write_performed: bool = False
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def validate(self) -> None:
        if self.ip_binding_count < 0:
            raise ValueError("Bybit Demo API-key IP binding count cannot be negative")
        if self.ip_binding_present != (self.ip_binding_count > 0):
            raise ValueError("Bybit Demo API-key IP binding metadata is inconsistent")
        if not self.authenticated_get_only:
            raise ValueError("Bybit Demo credential preflight must remain GET-only")
        if self.order_write_performed or self.order_writes_supported:
            raise ValueError("Bybit Demo credential preflight cannot perform order writes")
        if self.live_mainnet_order_routing_allowed:
            raise ValueError("Bybit Demo credential preflight cannot route mainnet orders")


@dataclass(frozen=True)
class BybitDemoTradingCredentialPreflightResult:
    status: BybitDemoTradingCredentialPreflightStatus
    reasons: tuple[str, ...]
    write_enabled_verified: bool
    ip_binding_present: bool
    personal_key_type_verified: bool
    uta_enabled: bool
    contract_order_permission: bool
    contract_position_permission: bool
    least_privilege_contract_only: bool
    distinct_from_demo_readonly_key: bool
    distinct_from_mainnet_readonly_key: bool
    authenticated_get_only: bool = True
    order_write_performed: bool = False
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    @property
    def passed(self) -> bool:
        ready = (
            BybitDemoTradingCredentialPreflightStatus.READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL
        )
        return self.status is ready

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "BYBIT_DEMO_TRADING_CREDENTIAL_PREFLIGHT_V1",
            "status": self.status.value,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "write_enabled_verified": self.write_enabled_verified,
            "ip_binding_present": self.ip_binding_present,
            "personal_key_type_verified": self.personal_key_type_verified,
            "uta_enabled": self.uta_enabled,
            "contract_order_permission": self.contract_order_permission,
            "contract_position_permission": self.contract_position_permission,
            "least_privilege_contract_only": self.least_privilege_contract_only,
            "distinct_from_demo_readonly_key": self.distinct_from_demo_readonly_key,
            "distinct_from_mainnet_readonly_key": self.distinct_from_mainnet_readonly_key,
            "authenticated_get_only": self.authenticated_get_only,
            "order_write_performed": self.order_write_performed,
            "order_writes_supported": self.order_writes_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


class BybitDemoTradingCredentialReadOnlyInspector(BybitDemoAccountingClient):
    """Inspect a write-capable Demo key through authenticated GET only.

    The inspected credential may itself have order permissions. This client deliberately exposes no
    mutation method: it inherits the exact-host GET-only Demo accounting transport and calls only
    ``GET /v5/user/query-api``.
    """

    environment = "BYBIT_DEMO_TRADING_CREDENTIAL_PREFLIGHT"
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    order_submission_supported = False
    authenticated_get_only = True

    @property
    def api_key_fingerprint_sha256(self) -> str:
        return hashlib.sha256(self._api_key.encode("utf-8")).hexdigest()

    def inspect(self) -> BybitDemoTradingApiKeyInfo:
        result = self._private_get_result(path="/v5/user/query-api", query={})
        returned_key = result.get("apiKey")
        if not isinstance(returned_key, str) or not returned_key:
            raise BybitRestProtocolError(
                "Bybit Demo API-key information is missing apiKey identity",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        if not hmac.compare_digest(returned_key, self._api_key):
            raise BybitRestProtocolError(
                "Bybit Demo API-key identity does not match configured credential",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        secret_marker = result.get("secret")
        if not isinstance(secret_marker, str):
            raise BybitRestProtocolError(
                "Bybit Demo API-key information returned invalid secret marker",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        if secret_marker:
            raise BybitRestProtocolError(
                "Bybit Demo API-key information unexpectedly exposed secret material",
                retryable_read=False,
                ambiguous_mutation=False,
            )

        read_only_value = _strict_int(result.get("readOnly"), field="readOnly")
        if read_only_value not in {0, 1}:
            raise BybitRestProtocolError(
                "Bybit Demo API-key information returned unsupported readOnly flag",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        key_type = _strict_int(result.get("type"), field="type")
        uta = _strict_int(result.get("uta"), field="uta")
        if key_type not in {1, 2}:
            raise BybitRestProtocolError(
                "Bybit Demo API-key information returned unsupported key type",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        if uta not in {0, 1}:
            raise BybitRestProtocolError(
                "Bybit Demo API-key information returned unsupported UTA flag",
                retryable_read=False,
                ambiguous_mutation=False,
            )

        ip_bindings = _normalize_ip_bindings(result.get("ips"))
        permissions = _normalize_permissions(result.get("permissions"))
        contract = permissions.get("ContractTrade", ())
        contract_set = frozenset(contract)
        contract_exact = (
            contract_set == _REQUIRED_CONTRACT_PERMISSIONS
            and len(contract) == len(_REQUIRED_CONTRACT_PERMISSIONS)
        )
        other_permission_present = any(
            values for name, values in permissions.items() if name != "ContractTrade"
        )
        info = BybitDemoTradingApiKeyInfo(
            read_write_enabled=read_only_value == 0,
            ip_binding_present=bool(ip_bindings),
            ip_binding_count=len(ip_bindings),
            personal_key_type=key_type == 1,
            uta_enabled=uta == 1,
            contract_order_permission="Order" in contract_set,
            contract_position_permission="Position" in contract_set,
            least_privilege_contract_only=(
                contract_exact and not other_permission_present
            ),
        )
        info.validate()
        return info

    def matches_api_key_fingerprint(self, candidate_sha256: str) -> bool:
        _validate_sha256(candidate_sha256, label="reference API-key fingerprint")
        return hmac.compare_digest(self.api_key_fingerprint_sha256, candidate_sha256)


def run_bybit_demo_trading_credential_preflight(
    client: BybitDemoTradingCredentialReadOnlyInspector,
    *,
    demo_readonly_api_key_sha256: str,
    mainnet_readonly_api_key_sha256: str,
) -> BybitDemoTradingCredentialPreflightResult:
    """Prove credential shape and namespace isolation without any order mutation."""

    _validate_inspector_capabilities(client)
    _validate_sha256(
        demo_readonly_api_key_sha256,
        label="Demo read-only API-key fingerprint",
    )
    _validate_sha256(
        mainnet_readonly_api_key_sha256,
        label="mainnet read-only API-key fingerprint",
    )

    info = client.inspect()
    distinct_demo = not client.matches_api_key_fingerprint(
        demo_readonly_api_key_sha256
    )
    distinct_mainnet = not client.matches_api_key_fingerprint(
        mainnet_readonly_api_key_sha256
    )

    reasons: list[str] = []
    if not info.read_write_enabled:
        reasons.append("DEMO_TRADING_KEY_IS_READ_ONLY")
    if not info.ip_binding_present:
        reasons.append("DEMO_TRADING_KEY_HAS_NO_IP_BINDING")
    if not info.personal_key_type:
        reasons.append("DEMO_TRADING_KEY_IS_NOT_PERSONAL")
    if not info.uta_enabled:
        reasons.append("DEMO_TRADING_KEY_ACCOUNT_IS_NOT_UTA")
    if not info.contract_order_permission:
        reasons.append("DEMO_TRADING_KEY_MISSING_CONTRACT_ORDER_PERMISSION")
    if not info.contract_position_permission:
        reasons.append("DEMO_TRADING_KEY_MISSING_CONTRACT_POSITION_PERMISSION")
    if not info.least_privilege_contract_only:
        reasons.append("DEMO_TRADING_KEY_IS_NOT_CONTRACT_ONLY_LEAST_PRIVILEGE")
    if not distinct_demo:
        reasons.append("DEMO_TRADING_KEY_REUSES_DEMO_READONLY_KEY")
    if not distinct_mainnet:
        reasons.append("DEMO_TRADING_KEY_REUSES_MAINNET_READONLY_KEY")

    status = (
        BybitDemoTradingCredentialPreflightStatus.READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL
        if not reasons
        else BybitDemoTradingCredentialPreflightStatus.BLOCKED
    )
    result = BybitDemoTradingCredentialPreflightResult(
        status=status,
        reasons=tuple(reasons),
        write_enabled_verified=info.read_write_enabled,
        ip_binding_present=info.ip_binding_present,
        personal_key_type_verified=info.personal_key_type,
        uta_enabled=info.uta_enabled,
        contract_order_permission=info.contract_order_permission,
        contract_position_permission=info.contract_position_permission,
        least_privilege_contract_only=info.least_privilege_contract_only,
        distinct_from_demo_readonly_key=distinct_demo,
        distinct_from_mainnet_readonly_key=distinct_mainnet,
    )
    _validate_result_capabilities(result)
    return result


def _normalize_ip_bindings(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise BybitRestProtocolError(
            "Bybit Demo API-key IP bindings must be an array",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    normalized: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value or value == "*":
            raise BybitRestProtocolError(
                "Bybit Demo API-key IP binding is invalid",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise BybitRestProtocolError(
                "Bybit Demo API-key IP binding is invalid",
                retryable_read=False,
                ambiguous_mutation=False,
            ) from exc
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise BybitRestProtocolError(
            "Bybit Demo API-key IP bindings contain duplicates",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return tuple(normalized)


def _normalize_permissions(raw: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, Mapping):
        raise BybitRestProtocolError(
            "Bybit Demo API-key permissions must be an object",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    permissions: dict[str, tuple[str, ...]] = {}
    for raw_name, raw_values in raw.items():
        if not isinstance(raw_name, str) or not raw_name:
            raise BybitRestProtocolError(
                "Bybit Demo API-key permission category is invalid",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values,
            (str, bytes, bytearray),
        ):
            raise BybitRestProtocolError(
                "Bybit Demo API-key permission values must be arrays",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        values: list[str] = []
        for value in raw_values:
            if not isinstance(value, str) or not value:
                raise BybitRestProtocolError(
                    "Bybit Demo API-key permission value is invalid",
                    retryable_read=False,
                    ambiguous_mutation=False,
                )
            values.append(value)
        if len(set(values)) != len(values):
            raise BybitRestProtocolError(
                "Bybit Demo API-key permission values contain duplicates",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        permissions[raw_name] = tuple(values)
    return permissions


def _strict_int(raw: Any, *, field: str) -> int:
    if isinstance(raw, bool) or raw is None:
        raise BybitRestProtocolError(
            f"Bybit Demo API-key information has invalid {field}",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise BybitRestProtocolError(
            f"Bybit Demo API-key information has invalid {field}",
            retryable_read=False,
            ambiguous_mutation=False,
        ) from exc
    return value


def _validate_sha256(value: str, *, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} must be lowercase SHA-256 hex")


def _validate_inspector_capabilities(client: Any) -> None:
    if getattr(client, "environment", None) != "BYBIT_DEMO_TRADING_CREDENTIAL_PREFLIGHT":
        raise ValueError("Bybit Demo trading credential inspector environment is invalid")
    if getattr(client, "host", None) != "api-demo.bybit.com":
        raise ValueError("Bybit Demo trading credential inspector host is invalid")
    if getattr(client, "authenticated_get_only", False) is not True:
        raise ValueError("Bybit Demo trading credential inspector must be GET-only")
    if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("Bybit Demo trading credential inspector cannot route mainnet orders")
    if getattr(client, "order_writes_supported", True) is not False:
        raise ValueError("Bybit Demo trading credential inspector cannot write orders")
    if getattr(client, "order_submission_supported", True) is not False:
        raise ValueError("Bybit Demo trading credential inspector cannot submit orders")
    for mutation_name in (
        "place_market_order",
        "cancel_order",
        "amend_order",
        "set_trading_stop",
    ):
        if callable(getattr(client, mutation_name, None)):
            raise ValueError(
                "Bybit Demo trading credential inspector unexpectedly exposes mutation method"
            )


def _validate_result_capabilities(result: BybitDemoTradingCredentialPreflightResult) -> None:
    if not result.authenticated_get_only:
        raise ValueError("Bybit Demo trading credential preflight result must remain GET-only")
    if result.order_write_performed or result.order_writes_supported:
        raise ValueError("Bybit Demo trading credential preflight result cannot grant order writes")
    if result.live_mainnet_order_routing_allowed:
        raise ValueError(
            "Bybit Demo trading credential preflight result cannot grant mainnet routing"
        )


__all__ = [
    "BybitDemoTradingApiKeyInfo",
    "BybitDemoTradingCredentialPreflightResult",
    "BybitDemoTradingCredentialPreflightStatus",
    "BybitDemoTradingCredentialReadOnlyInspector",
    "run_bybit_demo_trading_credential_preflight",
]
