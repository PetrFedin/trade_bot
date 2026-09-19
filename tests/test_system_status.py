from __future__ import annotations

import unittest

from tools import system_status


class SystemStatusContractTests(unittest.TestCase):
    def test_tracked_status_is_fail_closed(self) -> None:
        status = system_status.build_tracked_status()

        self.assertEqual(
            status["repository"]["observed_checkout_sha"],
            system_status.RUNTIME_SHA_MARKER,
        )
        manifest_path, manifest = system_status._latest_engineering_qualification()
        self.assertEqual(
            status["repository"]["latest_engineering_qualified_main_sha"],
            manifest["subject_sha"],
        )
        self.assertEqual(
            status["repository"]["latest_engineering_qualification_manifest"],
            str(manifest_path.relative_to(system_status.ROOT)),
        )
        self.assertEqual(
            status["repository"]["engineering_qualification_scope"],
            "ENGINEERING_CI_ONLY",
        )
        self.assertEqual(status["strategy"]["status"], "PROFITABILITY_NOT_PROVEN")
        self.assertFalse(status["strategy"]["promotion_allowed"])
        self.assertEqual(
            status["release"]["status"],
            "SOURCE_AND_CI_QUALIFICATION_ONLY",
        )
        for value in status["trading_authority"].values():
            if isinstance(value, bool):
                self.assertFalse(value)

    def test_runtime_render_replaces_only_runtime_identity(self) -> None:
        tracked = system_status.build_tracked_status()
        observed = "a" * 40
        runtime = system_status.render_runtime_status(
            tracked,
            observed_sha=observed,
            relation="CHANGED_SINCE_LAST_ENGINEERING_QUALIFICATION",
        )

        self.assertEqual(runtime["repository"]["observed_checkout_sha"], observed)
        self.assertEqual(
            runtime["repository"]["checkout_relation"],
            "CHANGED_SINCE_LAST_ENGINEERING_QUALIFICATION",
        )
        self.assertEqual(
            tracked["repository"]["observed_checkout_sha"],
            system_status.RUNTIME_SHA_MARKER,
        )
        self.assertEqual(tracked["repository"]["checkout_relation"], "<runtime:computed>")

    def test_generated_readme_block_has_single_authoritative_status_surface(self) -> None:
        status = system_status.build_tracked_status()
        block = system_status._readme_block(status)

        self.assertEqual(block.count(system_status.README_BEGIN), 1)
        self.assertEqual(block.count(system_status.README_END), 1)
        self.assertIn(
            status["repository"]["latest_engineering_qualified_main_sha"],
            block,
        )
        self.assertIn("PROFITABILITY_NOT_PROVEN", block)
        gate = status["engineering_qualification"]["next_vertical_gate"]
        self.assertIn(f"issue #{gate['issue']}", block)
        self.assertIn(gate["name"], block)
        self.assertIn("external routing, Demo, mainnet and live remain", block)

    def test_committed_generated_artifacts_are_current(self) -> None:
        system_status.check()


if __name__ == "__main__":
    unittest.main()
