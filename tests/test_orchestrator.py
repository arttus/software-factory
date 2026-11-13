import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory.core import orchestrator as orchestrator_module
from factory.core.config import load_config
from factory.core.contracts import Artifact, InvocationResult, utc_now
from factory.core.github import FakeGitHub
from factory.core.orchestrator import Orchestrator

REPO = Path(__file__).resolve().parents[1]


class FakeState:
    """In-memory stand-in for factory/bin/{job,gate} for sequencing tests."""

    def __init__(self, root: Path, job: dict):
        self.root = root
        self.job = job
        self.calls: list[tuple[str, str]] = []
        self.write()

    @property
    def path(self) -> Path:
        return self.root / "factory" / "jobs" / self.job["id"] / "job.json"

    @property
    def job_dir(self) -> Path:
        return self.root / "factory" / "jobs" / self.job["id"]

    def write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.job, indent=2) + "\n")

    def _header(self, name):
        path = self.job_dir / name
        if not path.exists():
            return {}
        fields = {}
        for line in path.read_text().splitlines():
            if not line.strip():
                break
            key, _, value = line.partition(":")
            fields[key.strip().upper()] = value.strip()
        return fields

    def sync(self, job_id):
        self.calls.append((job_id, "sync"))
        intake = self._header("intake.md")
        if intake:
            self.job["intake"] = {
                "decision": intake.get("DECISION"),
                "rescoped": intake.get("RESCOPED") == "yes",
                "risk": intake.get("RISK"),
                "surfaces": [s.strip() for s in intake.get("SURFACES", "").split(",")
                             if s.strip() and s.strip() != "none"],
            }
        if self.job["round"]:
            rd = self.job_dir / f"round-{self.job['round']}"
            checks = self._header(f"round-{self.job['round']}/checks.md")
            self.job["gate"] = checks.get("GATE")
            roles = (self.job.get("agents") or {}).get("reviewers", {})
            self.job["reviews"] = {}
            for role in roles:
                path = rd / f"review-{role}.md"
                first = path.read_text().split("\n", 1)[0].strip() if path.exists() else None
                self.job["reviews"][role] = (first.split(":", 1)[1].strip()
                                             if first and ":" in first else "pending")
        self.write()

    def agents(self, job_id, agents):
        self.calls.append((job_id, "agents"))
        self.job["agents"] = agents
        self.write()

    def advance(self, job_id, stage):
        self.calls.append((job_id, stage))
        if stage == "build":
            self.job["round"] += 1
            (self.job_dir / f"round-{self.job['round']}").mkdir(parents=True, exist_ok=True)
        self.job["stage"] = stage
        self.write()

    def gate(self, job_id):
        self.calls.append((job_id, "gate"))
        rd = self.job_dir / f"round-{self.job['round']}"
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "checks.md").write_text("GATE: PASS\nCOMMIT: abc\n\n## Summary\n- ok\n")
        return 0

    def merge(self, job_id):
        self.calls.append((job_id, "merge"))
        self.job["stage"] = "merged"
        self.write()


class TestOrchestrator(Orchestrator):
    def _run_role(self, job, role, *, reviewers=None, extra_inputs=None):
        self.log(f"role {role}")
        job_dir = self.factory_root / "factory" / "jobs" / job["id"]
        bodies = {
            "intake": ("intake.md", "DECISION: ACCEPT\nRISK: low\nSURFACES: none\nRESCOPED: no\n\n## Summary\n- ok\n"),
            "spec": ("spec.md", "SPEC: demo\n"),
            "builder": ("build.md", "STATUS: READY\n\n## Summary\n- ok\n"),
            "approver": ("decision.md", "APPROVE\n\n## Summary\n- ok\n"),
        }
        if role in bodies:
            name, text = bodies[role]
        else:
            name, text = f"round-{job['round']}/review-{role}.md", "VERDICT: PASS\n\n## Summary\n- ok\n"
        path = job_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return InvocationResult(invocation_id="x", status="completed", exit_code=0,
                                adapter="fake", started_at=utc_now(), ended_at=utc_now())


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "factory" / "roles").mkdir(parents=True)
        for path in (REPO / "factory" / "roles").glob("*.md"):
            shutil.copy2(path, self.root / "factory" / "roles" / path.name)
        self.config = load_config(REPO / "factory" / "config.json", role_set="command-code")
        self.job = {
            "id": "001-demo", "issue": 1, "stage": "intake", "round": 0,
            "round_limit": 3, "agents": None, "intake": None, "gate": None,
            "reviews": {}, "worktree": str(self.root / ".worktrees" / "001-demo"),
            "branch": "factory/001-demo",
        }
        self.state = FakeState(self.root, self.job)
        self.github = FakeGitHub()
        self.orchestrator = TestOrchestrator(
            factory_root=self.root, config=self.config, adapters={},
            state=self.state, log=lambda _: None, github=self.github,
            allow_merge=True)

    def test_full_job_flow(self):
        final = self.orchestrator.run_job("001-demo")
        self.assertEqual(final["stage"], "merged")
        stages = [stage for _, stage in self.state.calls]
        for expected in ("agents", "spec", "build", "gate", "review", "approve", "merge"):
            self.assertIn(expected, stages)
        self.assertEqual(final["round"], 1)
        self.assertEqual(final["reviews"], {"code": "PASS"})
        self.assertEqual(len(self.github.pulls), 1)
        self.assertEqual(next(iter(self.github.pulls.values()))["head"], "factory/001-demo")

    def test_intake_request_has_no_worktree(self):
        request = self.orchestrator.build_request(self.job, "intake")
        self.assertIsNone(request.worktree)
        self.assertEqual(request.controller_root, str(self.root))

    def test_request_for_builder(self):
        self.job["stage"] = "build"
        self.job["round"] = 1
        self.job["intake"] = {"risk": "low", "surfaces": [], "decision": "ACCEPT"}
        request = self.orchestrator.build_request(self.job, "builder")
        self.assertEqual(request.outputs, ["build.md"])
        self.assertEqual(request.adapter, "command-code")
        self.assertEqual(request.profile, "cc-builder")
        self.assertEqual(request.base_branch, "main")
        self.assertEqual(request.permission_profile, "builder")
        self.assertTrue(request.instruction_digest)

    def test_reviewer_profiles_follow_risk_and_surfaces(self):
        self.job["intake"] = {"risk": "medium", "surfaces": ["ui"]}
        reviewers = self.orchestrator.reviewers_for(self.job)
        self.assertEqual(set(reviewers), {"code", "ux", "ui"})
        self.assertTrue(all(p.adapter == "command-code" for p in reviewers.values()))

    def test_reviewer_request_uses_round_output(self):
        self.job.update({"stage": "review", "round": 2,
                         "intake": {"risk": "low", "surfaces": []}})
        reviewers = self.orchestrator.reviewers_for(self.job)
        request = self.orchestrator.build_request(self.job, "code", reviewers=reviewers)
        self.assertEqual(request.outputs, ["round-2/review-code.md"])

    def test_extra_inputs_reach_the_request(self):
        artifact = Artifact(path=str(self.root / "shot.png"), sha256="a" * 64)
        request = self.orchestrator.build_request(self.job, "intake",
                                                  extra_inputs=[artifact])
        self.assertIn(artifact, request.inputs)

    def test_ui_review_captures_browser_evidence(self):
        self.orchestrator.config.browser = {
            "command": "agent-browser",
            "start_command": ["true", "{port}"],
            "url": "http://127.0.0.1:{port}/",
            "viewports": [{"name": "desktop", "width": 1440, "height": 900}],
        }
        self.job.update({"stage": "review", "round": 1,
                         "intake": {"risk": "low", "surfaces": ["ui"]},
                         "agents": {"reviewers": {"code": "cc-code-haiku",
                                                  "ux": "cc-ux-sonnet",
                                                  "ui": "cc-ui-haiku"}}})
        rd = self.root / "factory" / "jobs" / "001-demo" / "round-1"
        rd.mkdir(parents=True)
        (rd / "checks.md").write_text("GATE: PASS\nCOMMIT: abc\n")
        evidence = Artifact(path=str(rd / "browser" / "desktop.png"), sha256="a" * 64)
        with mock.patch.object(orchestrator_module.browser_evidence, "capture",
                               return_value=[evidence]) as captured:
            self.orchestrator._stage_review(self.job)
        captured.assert_called_once()
        self.assertEqual(self.job["stage"], "approve")

    def test_ui_review_without_browser_config_is_refused(self):
        self.job.update({"stage": "review", "round": 1,
                         "intake": {"risk": "low", "surfaces": ["ui"]}})
        with self.assertRaises(Exception):
            self.orchestrator._stage_review(self.job)

    def test_human_merge_stops_at_a_ready_pr(self):
        self.orchestrator.allow_merge = False
        self.job["pr"] = 7
        final = self.orchestrator.run_job("001-demo")
        self.assertEqual(final["stage"], "approve")
        self.assertEqual(self.github.ready, [7])
        self.assertEqual(self.github.merged, [])


if __name__ == "__main__":
    unittest.main()
