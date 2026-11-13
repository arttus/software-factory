import importlib.util
import json
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]


def load_job_module():
    path = REPO / "factory" / "bin" / "job"
    loader = SourceFileLoader("factory_job", str(path))
    spec = importlib.util.spec_from_loader("factory_job", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeGitResult:
    returncode = 0
    stderr = ""

    def __init__(self, stdout=""):
        self.stdout = stdout


class JobTransitionTests(unittest.TestCase):
    def setUp(self):
        self.job = load_job_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs_root = Path(self.tmp.name) / "jobs"
        self.jobs_root.mkdir()
        self.original_jobs = self.job.JOBS
        self.job.JOBS = self.jobs_root

    def tearDown(self):
        self.job.JOBS = self.original_jobs

    def make_job(self, job_id="001-demo", **overrides):
        directory = self.jobs_root / job_id
        directory.mkdir(exist_ok=True)
        job = {
            "id": job_id, "issue": 1, "stage": "spec", "round": 0,
            "round_limit": 3, "agents": {"reviewers": {"code": "haiku"}},
            "reviews": {}, "gate": None, "pr": None, "pr_url": None,
            "worktree": str(Path(self.tmp.name) / "worktrees" / job_id),
            "branch": f"factory/{job_id}", "intake": None, "cost_usd": 0.0,
            "started": "2026-01-01T00:00:00Z", "ended": None,
        }
        job.update(overrides)
        return job

    def test_pipeline_transitions_match_design(self):
        expected = {
            "intake": {"needs-info", "rejected", "spec"},
            "needs-info": {"intake", "rejected"},
            "spec": {"build"},
            "build": {"gate", "needs-human"},
            "gate": {"review", "build", "needs-human"},
            "review": {"approve", "build", "needs-human"},
            "approve": {"merging", "needs-human"},
            "merging": {"needs-human"},
            "needs-human": {"build", "rejected"},
        }
        self.assertEqual(self.job.TRANSITIONS, expected)

    def test_artifact_header_parsing(self):
        path = Path(self.tmp.name) / "intake.md"
        path.write_text("DECISION: ACCEPT\nRISK: low\nSURFACES: ui, security\n"
                        "RESCOPED: yes\n\n## Summary\n- one\n")
        header = self.job.header(path)
        self.assertEqual(header["DECISION"], "ACCEPT")
        self.assertEqual(header["RISK"], "low")
        self.assertEqual(self.job.first_line(path), "DECISION: ACCEPT")
        self.assertEqual(self.job.section(path, "Summary"), "- one")

    def test_sync_parses_artifacts_without_cost(self):
        job = self.make_job(stage="review", round=1)
        directory = self.jobs_root / job["id"]
        (directory / "intake.md").write_text(
            "DECISION: ACCEPT\nRISK: low\nSURFACES: ui, security\nRESCOPED: yes\n\n"
            "## Summary\n- one\n")
        round_dir = directory / "round-1"
        round_dir.mkdir()
        (round_dir / "checks.md").write_text("GATE: PASS\nCOMMIT: abc123\n\n## Summary\n- ok\n")
        (round_dir / "review-code.md").write_text("VERDICT: PASS\n\n## Summary\n- ok\n")
        with mock.patch.object(self.job, "cost_usd", side_effect=AssertionError("cost called")):
            synced = self.job.sync(job)
        self.assertEqual(synced["intake"]["decision"], "ACCEPT")
        self.assertEqual(synced["intake"]["surfaces"], ["ui", "security"])
        self.assertTrue(synced["intake"]["rescoped"])
        self.assertEqual(synced["gate"], "PASS")
        self.assertEqual(synced["reviews"], {"code": "PASS"})

    def test_spec_to_build_starts_a_round(self):
        job = self.make_job(stage="spec", round=0)
        (self.jobs_root / job["id"] / "spec.md").write_text("SPEC: x\n")
        moved = self.job.advance(job, "build")
        self.assertEqual(moved["stage"], "build")
        self.assertEqual(moved["round"], 1)
        self.assertTrue((self.jobs_root / job["id"] / "round-1").is_dir())

    def test_gate_to_review_requires_the_same_commit(self):
        job = self.make_job(stage="gate", round=1)
        round_dir = self.jobs_root / job["id"] / "round-1"
        round_dir.mkdir()
        (round_dir / "checks.md").write_text("GATE: PASS\nCOMMIT: abc123\n\n## Summary\n- ok\n")
        with mock.patch.object(self.job, "git", return_value=FakeGitResult("abc123\n")):
            moved = self.job.advance(job, "review")
        self.assertEqual(moved["stage"], "review")
        self.assertEqual(moved["reviews"], {"code": "pending"})

    def test_gate_to_review_refuses_a_stale_gate(self):
        job = self.make_job(stage="gate", round=1)
        round_dir = self.jobs_root / job["id"] / "round-1"
        round_dir.mkdir()
        (round_dir / "checks.md").write_text("GATE: PASS\nCOMMIT: old\n\n## Summary\n- ok\n")
        with mock.patch.object(self.job, "git", return_value=FakeGitResult("new\n")):
            with self.assertRaises(self.job.Refused):
                self.job.advance(job, "review")

    def test_approve_to_merging_reads_the_decision(self):
        job = self.make_job(stage="approve", round=1)
        (self.jobs_root / job["id"] / "decision.md").write_text("APPROVE\n\n## Summary\n- ok\n")
        moved = self.job.advance(job, "merging")
        self.assertEqual(moved["stage"], "merging")

    def test_illegal_transition_is_refused(self):
        job = self.make_job(stage="build", round=1)
        with self.assertRaises(self.job.Refused):
            self.job.advance(job, "approve")

    def test_job_json_is_valid_after_save(self):
        job = self.make_job(stage="spec", round=0)
        self.job.save(job)
        raw = json.loads((self.jobs_root / job["id"] / "job.json").read_text())
        self.assertEqual(raw["stage"], "spec")


if __name__ == "__main__":
    unittest.main()
