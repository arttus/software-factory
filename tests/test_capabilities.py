import unittest
from pathlib import Path

from factory.adapters.fake import FakeAdapter
from factory.core.capabilities import (
    CapabilityReport,
    check_capabilities,
    check_permission_profile,
)
from factory.core.config import PermissionProfile, load_config

REPO = Path(__file__).resolve().parents[1]


class CapabilityTests(unittest.TestCase):
    def test_fake_adapter_satisfies_every_role(self):
        report = FakeAdapter(REPO).probe()
        for role in ("intake", "spec", "builder", "approver", "code", "security", "ux", "ui"):
            with self.subTest(role=role):
                self.assertEqual(check_capabilities(role, report), [])

    def test_prompt_only_boundary_is_not_accepted(self):
        report = CapabilityReport(adapter="x", version="1", capabilities={
            "noninteractive": "native",
            "machine_readable_events": "native",
            "model_selection": "native",
            "cancellation": "native",
            "fs_restrictions": "prompt_only",
        })
        problems = check_capabilities("code", report)
        self.assertTrue(any("prompt-enforces" in p for p in problems))

    def test_missing_capability_is_reported(self):
        report = CapabilityReport(adapter="x", version="1", capabilities={
            "noninteractive": "native",
            "machine_readable_events": "native",
            "model_selection": "native",
            "cancellation": "native",
        })
        problems = check_capabilities("builder", report)
        self.assertTrue(any("shell_control" in p for p in problems))
        self.assertTrue(any("network_control" in p for p in problems))


class PermissionProfileTests(unittest.TestCase):
    def test_repo_permissions_match_roles(self):
        config = load_config(REPO / "factory" / "config.json")
        self.assertEqual(check_permission_profile("intake", config.permission("intake")), [])
        self.assertEqual(check_permission_profile("builder", config.permission("builder")), [])
        self.assertEqual(check_permission_profile("code", config.permission("reviewer")), [])

    def test_intake_may_not_have_a_shell(self):
        permission = PermissionProfile("intake", shell=True, network=False,
                                       write_scope="output_only")
        self.assertTrue(check_permission_profile("intake", permission))

    def test_reviewer_may_not_write_the_worktree(self):
        permission = PermissionProfile("reviewer", shell=False, network=False,
                                       write_scope="worktree_and_output")
        self.assertTrue(check_permission_profile("code", permission))


if __name__ == "__main__":
    unittest.main()
