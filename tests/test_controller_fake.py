import json
import tempfile
import unittest
from pathlib import Path

from factory.adapters.fake import FakeAdapter
from factory.core.config import load_config
from factory.core.controller import Controller, ControllerError, LimitTracker
from factory.core.contracts import InvocationRequest, new_invocation_id, utc_now

REPO = Path(__file__).resolve().parents[1]


def make_request(root: Path, **overrides) -> InvocationRequest:
    job_dir = root / "jobs" / "smoke"
    job_dir.mkdir(parents=True, exist_ok=True)
    data = dict(
        invocation_id=new_invocation_id(),
        job_id="smoke",
        stage="build",
        round=1,
        attempt=1,
        role="builder",
        controller_root=str(REPO),
        job_dir=str(job_dir),
        instruction_path="factory/roles/builder.md",
        instruction_digest="b" * 64,
        outputs=["build.md"],
        profile="fake-builder",
        adapter="fake",
        model="fake-model",
        required_capabilities=["shell_control"],
        permission_profile="builder",
        created_at=utc_now(),
    )
    data.update(overrides)
    return InvocationRequest(**data)


class ControllerFakeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = load_config(REPO / "factory" / "config.json")
        self.adapter = FakeAdapter(self.root)
        self.controller = Controller.from_config(
            self.config, self.root, adapters={"fake": self.adapter})

    def test_run_records_evidence_and_returns_completed(self):
        request = make_request(self.root)
        result = self.controller.run(request)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.invocation_id, request.invocation_id)

        run_dir = self.root / "jobs" / "smoke" / "runs" / request.invocation_id
        self.assertTrue((run_dir / "request.json").is_file())
        self.assertTrue((run_dir / "result.json").is_file())
        events = (run_dir / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(events), 4)
        self.assertEqual(json.loads(events[0])["type"], "started")
        self.assertEqual(json.loads(events[-1])["type"], "finished")

        artifact = self.root / "jobs" / "smoke" / "build.md"
        self.assertEqual(artifact.read_text().splitlines()[0], "STATUS: READY")
        self.assertTrue(result.artifacts[0].sha256)

        saved = json.loads((run_dir / "result.json").read_text())
        self.assertEqual(saved["schema"], "factory.invocation.result/1")

    def test_cancel_marks_the_result_cancelled(self):
        handle = self.adapter.start(make_request(self.root))
        self.adapter.cancel(handle)
        self.assertEqual(handle.result.status, "cancelled")
        self.assertIsNone(handle.result.exit_code)

    def test_unknown_adapter_is_refused(self):
        request = make_request(self.root, adapter="missing")
        with self.assertRaises(ControllerError):
            self.controller.run(request)

    def test_capability_gap_is_refused_before_launch(self):
        request = make_request(self.root, role="code", permission_profile="reviewer",
                               outputs=["round-1/review-code.md"])
        # The fake adapter supports everything, so break its report.
        report = self.adapter.probe()
        report.capabilities["fs_restrictions"] = "none"
        self.adapter.probe = lambda: report
        with self.assertRaises(ControllerError):
            self.controller.run(request)


class LimitTrackerTests(unittest.TestCase):
    def test_per_adapter_limit(self):
        limits = LimitTracker({"workers": 5, "per_adapter": {"fake": 1}})
        with limits.slot("fake"):
            with self.assertRaises(ControllerError):
                with limits.slot("fake"):
                    pass
        with limits.slot("fake"):
            pass


if __name__ == "__main__":
    unittest.main()
