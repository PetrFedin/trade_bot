from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.ci_action_policy import (
    APPROVED_ACTION_REFS,
    audit_repository,
    audit_workflow,
    is_operational_workflow,
)


class CiActionPolicyTests(unittest.TestCase):
    def test_main_workflow_with_approved_sha_passes(self) -> None:
        checkout = APPROVED_ACTION_REFS["actions/checkout"]
        text = f"""name: current\non:\n  push:\n    branches: [main]\njobs:\n  x:\n    steps:\n      - uses: actions/checkout@{checkout}\n"""
        self.assertTrue(is_operational_workflow(text))
        self.assertEqual(audit_workflow(Path("current.yml"), text), [])

    def test_unrestricted_pull_request_is_operational_and_floating_ref_fails(self) -> None:
        text = """name: pr\non:\n  pull_request:\njobs:\n  x:\n    steps:\n      - uses: actions/checkout@v4\n"""
        violations = audit_workflow(Path("pr.yml"), text)
        self.assertTrue(is_operational_workflow(text))
        self.assertEqual(len(violations), 1)
        self.assertIn("exact 40-hex SHA", violations[0].message)

    def test_feature_branch_only_historical_workflow_is_not_operational(self) -> None:
        text = """name: old\non:\n  pull_request:\n    branches: [agent/schema-old]\njobs:\n  x:\n    steps:\n      - uses: actions/checkout@v4\n"""
        self.assertFalse(is_operational_workflow(text))
        self.assertEqual(audit_workflow(Path("old.yml"), text), [])

    def test_manual_workflow_is_operational(self) -> None:
        text = """name: manual\non:\n  workflow_dispatch:\njobs:\n  x:\n    steps:\n      - uses: actions/setup-python@v5\n"""
        self.assertTrue(is_operational_workflow(text))
        self.assertEqual(len(audit_workflow(Path("manual.yml"), text)), 1)

    def test_old_exact_node20_pin_is_rejected(self) -> None:
        text = """name: old-pin\non:\n  push:\n    branches: [main]\njobs:\n  x:\n    steps:\n      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262\n"""
        violations = audit_workflow(Path("old-pin.yml"), text)
        self.assertEqual(len(violations), 1)
        self.assertIn("approved Node 24 SHA", violations[0].message)

    def test_local_action_is_allowed(self) -> None:
        text = """name: local\non:\n  push:\n    branches: [main]\njobs:\n  x:\n    steps:\n      - uses: ./.github/actions/local\n"""
        self.assertEqual(audit_workflow(Path("local.yml"), text), [])

    def test_dynamic_pip_upgrade_is_rejected(self) -> None:
        text = """name: pip\non:\n  push:\n    branches: [main]\njobs:\n  x:\n    steps:\n      - run: python -m pip install --upgrade pip\n"""
        violations = audit_workflow(Path("pip.yml"), text)
        self.assertEqual(len(violations), 1)
        self.assertIn("must not upgrade pip dynamically", violations[0].message)

    def test_repository_audit_only_checks_operational_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "active.yml").write_text(
                "name: a\non:\n  push:\n    branches: [main]\njobs:\n  x:\n    steps:\n      - uses: actions/checkout@v4\n",
                encoding="utf-8",
            )
            (workflows / "historical.yml").write_text(
                "name: h\non:\n  push:\n    branches: [agent/old]\njobs:\n  x:\n    steps:\n      - uses: actions/checkout@v4\n",
                encoding="utf-8",
            )
            operational, violations = audit_repository(root)
            self.assertEqual([path.name for path in operational], ["active.yml"])
            self.assertEqual(len(violations), 1)


if __name__ == "__main__":
    unittest.main()
