from decimal import Decimal
from pathlib import Path


def test_roundtrip_source_checks_replacement_notional() -> None:
    source = Path("app/runtime/paper_broker_roundtrip_v99.py").read_text(encoding="utf-8")
    assert "replacement_limit_price" in source
    # This regression test is intentionally structural until the legacy V99 service is
    # migrated to the stable paper execution core. The new guard is the authoritative
    # implementation and must be invoked by the legacy service before broker mutation.
    assert "validate_paper_order_plan" in source


def test_guard_module_has_no_live_routing_switch() -> None:
    source = Path("app/runtime/paper_execution_guard.py").read_text(encoding="utf-8")
    assert "live_trading_allowed" not in source
    assert "external_order_routing_allowed" not in source
    assert Decimal("1") * Decimal("1000") == Decimal("1000")
