import tempfile
import unittest
from pathlib import Path

from factory.core.artifacts import (
    ArtifactError,
    atomic_write,
    canonical_path,
    digest,
    promote,
    validate_output,
)
from factory.core.roles import load_roles

REPO = Path(__file__).resolve().parents[1]


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roles = load_roles(REPO)

    def test_canonical_paths(self):
        job = self.root / "jobs" / "001"
        self.assertEqual(canonical_path(job, self.roles["intake"]),
                         job / "intake.md")
        self.assertEqual(canonical_path(job, self.roles["reviewer-code"], 3),
                         job / "round-3" / "review-code.md")
        self.assertEqual(canonical_path(job, self.roles["approver"]),
                         job / "decision.md")

    def test_first_line_validation(self):
        validate_output(self.roles["intake"], "DECISION: ACCEPT\n\n## Summary\n- x\n")
        with self.assertRaises(ArtifactError):
            validate_output(self.roles["intake"], "DECISION: MAYBE\n")
        with self.assertRaises(ArtifactError):
            validate_output(self.roles["reviewer-code"], "VERDICT: LGTM\n")

    def test_atomic_write_and_promote(self):
        staged = self.root / "staged" / "intake.md"
        atomic_write(staged, "DECISION: ACCEPT\n")
        canonical = self.root / "jobs" / "001" / "intake.md"
        promote(staged, canonical)
        self.assertFalse(staged.exists())
        self.assertEqual(canonical.read_text(), "DECISION: ACCEPT\n")

    def test_digest_matches_content(self):
        path = atomic_write(self.root / "x.md", "hello\n")
        self.assertEqual(digest(path), digest(path))
        self.assertEqual(len(digest(path)), 64)


if __name__ == "__main__":
    unittest.main()
