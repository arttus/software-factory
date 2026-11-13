import copy
import json
import unittest
from pathlib import Path

from factory.core.config import ConfigError, build_config, load_config

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "factory" / "config.json"


class RepoConfigTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(CONFIG.read_text())
        self.config = load_config(CONFIG)

    def build(self, raw):
        return build_config(copy.deepcopy(raw), CONFIG)

    def test_schema_and_roles_resolve(self):
        self.assertEqual(self.config.schema_version, 2)
        self.assertEqual(self.config.role_profile("builder").name, "claude-builder")
        self.assertEqual(self.config.role_profile("intake").adapter, "claude")

    def test_routing_resolves_to_profiles(self):
        reviewers = self.config.reviewers_for("low")
        self.assertEqual(set(reviewers), {"code"})
        self.assertEqual(reviewers["code"].name, "claude-code-haiku")
        ui = self.config.reviewers_for("medium", ["ui"])
        self.assertEqual(set(ui), {"code", "ux", "ui"})

    def test_unknown_profile_in_routing_is_rejected(self):
        raw = self.raw
        raw["routing"]["low"]["code"] = "missing-profile"
        with self.assertRaises(ConfigError):
            self.build(raw)

    def test_profile_on_disabled_adapter_is_rejected(self):
        raw = self.raw
        raw["profiles"]["codex-builder"] = {
            "adapter": "codex", "model": "some-model", "permission_profile": "builder"
        }
        raw["roles"]["builder"] = "codex-builder"
        with self.assertRaises(ConfigError):
            self.build(raw)

    def test_profile_needs_a_model(self):
        raw = self.raw
        del raw["profiles"]["claude-builder"]["model"]
        with self.assertRaises(ConfigError):
            self.build(raw)

    def test_permission_profile_must_exist(self):
        raw = self.raw
        raw["profiles"]["claude-builder"]["permission_profile"] = "missing"
        with self.assertRaises(ConfigError):
            self.build(raw)

    def test_wrong_schema_version_is_rejected(self):
        raw = self.raw
        raw["schema_version"] = 1
        with self.assertRaises(ConfigError):
            self.build(raw)

    def test_limits_must_be_positive(self):
        raw = self.raw
        raw["limits"]["workers"] = 0
        with self.assertRaises(ConfigError):
            self.build(raw)

    def test_command_code_role_set_resolves(self):
        config = load_config(CONFIG, role_set="command-code")
        self.assertEqual(config.role_set, "command-code")
        self.assertEqual(config.role_profile("builder").adapter, "command-code")
        self.assertEqual(config.reviewers_for("low")["code"].adapter, "command-code")

    def test_unknown_role_set_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(CONFIG, role_set="nope")


if __name__ == "__main__":
    unittest.main()
