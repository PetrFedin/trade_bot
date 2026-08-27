from __future__ import annotations

import pytest

from app.execution.bybit_demo_operational_entry import (
    BybitDemoOperationalProtectionReconciliation,
    BybitDemoOperationalProtectionStatus,
)


@pytest.mark.parametrize(
    "reconciliation",
    (
        BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.CANONICAL_RUNTIME_RECONCILED,
            completed=True,
            entry_execution_confirmed=False,
            safety_mutation_performed=False,
        ),
        BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.NO_EXECUTION_CONFIRMED,
            completed=True,
            entry_execution_confirmed=None,
            safety_mutation_performed=False,
        ),
        BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.RECOVERED_PROTECTED,
            completed=True,
            entry_execution_confirmed=True,
            safety_mutation_performed=False,
        ),
        BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.UNRESOLVED,
            completed=False,
            entry_execution_confirmed=None,
            safety_mutation_performed=True,
        ),
        BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.UNRESOLVED,
            completed=False,
            entry_execution_confirmed=1,  # type: ignore[arg-type]
            safety_mutation_performed=False,
        ),
    ),
)
def test_reconciliation_rejects_status_outcome_contradictions(
    reconciliation: BybitDemoOperationalProtectionReconciliation,
) -> None:
    with pytest.raises(ValueError):
        reconciliation.validate()


@pytest.mark.parametrize(
    "reconciliation",
    (
        BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.CANONICAL_RUNTIME_RECONCILED,
            completed=True,
            entry_execution_confirmed=True,
            safety_mutation_performed=False,
        ),
        BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.NO_ENTRY_AUTHORIZATION,
            completed=True,
            entry_execution_confirmed=False,
            safety_mutation_performed=False,
        ),
        BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.NO_EXECUTION_CONFIRMED,
            completed=True,
            entry_execution_confirmed=False,
            safety_mutation_performed=False,
        ),
        BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.RECOVERED_PROTECTED,
            completed=True,
            entry_execution_confirmed=True,
            safety_mutation_performed=True,
        ),
        BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.RECOVERED_FLATTENED,
            completed=True,
            entry_execution_confirmed=True,
            safety_mutation_performed=True,
        ),
        BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.UNRESOLVED,
            completed=False,
            entry_execution_confirmed=None,
            safety_mutation_performed=False,
        ),
        BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.UNRESOLVED,
            completed=False,
            entry_execution_confirmed=True,
            safety_mutation_performed=False,
        ),
    ),
)
def test_reconciliation_accepts_only_coherent_production_shapes(
    reconciliation: BybitDemoOperationalProtectionReconciliation,
) -> None:
    reconciliation.validate()
