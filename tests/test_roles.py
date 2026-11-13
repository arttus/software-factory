import unittest
from pathlib import Path

from factory.core.roles import ROLE_NAMES, RoleError, load_role, load_roles

REPO = Path(__file__).resolve().parents[1]

SAMPLES = {
    "intake": "DECISION: ACCEPT",
    "spec-writer": "SPEC: Add subtract",
    "builder": "STATUS: READY",
    "reviewer-code": "VERDICT: PASS",
    "reviewer-security": "VERDICT: CHANGES",
    "reviewer-ux": "VERDICT: PASS",
    "reviewer-ui": "VERDICT: PASS",
    "approver": "APPROVE",
}

FORBIDDEN = (
    "sendmessage",
    "subagent_type",
    "subagentstop",
    "hooks:",
    "agent-browser",
    "claude",
    "opus",
    "haiku",
    "sonnet",
    "fable",
    "tools:",
    "model:",
)


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.roles = load_roles(REPO)

    def test_all_roles_load(self):
        self.assertEqual(set(self.roles), set(ROLE_NAMES))

    def test_first_line_contracts(self):
        for name, sample in SAMPLES.items():
            with self.subTest(role=name):
                self.assertTrue(self.roles[name].matches_first_line(sample))
                self.assertFalse(self.roles[name].matches_first_line("something else"))

    def test_roles_are_portable(self):
        for name, role in self.roles.items():
            text = role.instructions.lower()
            for token in FORBIDDEN:
                with self.subTest(role=name, token=token):
                    self.assertNotIn(token, text)

    def test_reviewer_outputs_are_round_scoped(self):
        self.assertEqual(self.roles["reviewer-code"].output_for(2), "round-2/review-code.md")
        self.assertEqual(self.roles["builder"].output_for(9), "build.md")

    def test_missing_frontmatter_is_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intake.md"
            path.write_text("# no frontmatter\n")
            with self.assertRaises(RoleError):
                load_role(path)

    def test_digest_is_stable(self):
        again = load_roles(REPO)
        for name in ROLE_NAMES:
            self.assertEqual(self.roles[name].digest, again[name].digest)


if __name__ == "__main__":
    unittest.main()
