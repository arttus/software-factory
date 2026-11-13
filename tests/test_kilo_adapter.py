import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from factory.adapters.kilo import KiloAdapter
from factory.core.contracts import InvocationRequest, new_invocation_id, utc_now

REPO = Path(__file__).resolve().parents[1]

FAKE_KILO = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time

argv = sys.argv[1:]
if "--version" in argv:
    print("kilo 9.9.9")
    sys.exit(0)

log = os.environ.get("FAKE_LOG")
if log:
    pathlib.Path(log).write_text(json.dumps({"argv": argv,
                                            "xdg": os.environ.get("XDG_CONFIG_HOME")}))

mode = os.environ.get("FAKE_MODE", "ok")
session = "ses_fake"
if mode == "sleep":
    print(json.dumps({"type": "step_start", "sessionID": session, "part": {}}), flush=True)
    time.sleep(30)
    sys.exit(0)
if mode == "fail":
    print(json.dumps({"type": "error", "sessionID": session,
                      "error": {"name": "APIError", "data": {"message": "boom"}}}), flush=True)
    sys.exit(1)

out = pathlib.Path(os.environ["FAKE_OUTPUT"])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(os.environ.get("FAKE_BODY", "VERDICT: PASS\n"))
print(json.dumps({"type": "step_start", "sessionID": session, "part": {}}), flush=True)
print(json.dumps({"type": "text", "sessionID": session, "part": {"text": "ok"}}), flush=True)
print(json.dumps({"type": "step_finish", "sessionID": session, "part": {
    "tokens": {"input": 100, "output": 5, "reasoning": 2, "cache": {"read": 10, "write": 3}},
    "cost": 0.0123}}), flush=True)
'''


class KiloAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.script = self.root / "kilo"
        self.script.write_text(FAKE_KILO)
        self.script.chmod(0o755)
        self.adapter = KiloAdapter(self.root, executable=str(self.script))
        self.job_dir = self.root / "jobs" / "001-demo"
        self.job_dir.mkdir(parents=True)
        self.worktree = self.root / ".worktrees" / "001-demo"
        self.worktree.mkdir(parents=True)
        self._old_env = {k: os.environ.get(k) for k in
                         ("FAKE_LOG", "FAKE_MODE", "FAKE_OUTPUT", "FAKE_BODY")}

    def tearDown(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def request(self, **overrides):
        data = dict(
            invocation_id=new_invocation_id(),
            job_id="001-demo",
            stage="review",
            round=1,
            attempt=1,
            role="code",
            controller_root=str(REPO),
            job_dir=str(self.job_dir),
            worktree=str(self.worktree),
            branch="factory/001-demo",
            instruction_path="factory/roles/reviewer-code.md",
            instruction_digest="c" * 64,
            outputs=["round-1/review-code.md"],
            profile="kilo-code-haiku",
            adapter="kilo",
            provider="openrouter",
            model="openrouter/anthropic/claude-haiku-4.5",
            required_capabilities=["fs_restrictions"],
            permission_profile="reviewer",
            permissions={"shell": False, "network": False, "write_scope": "output_only"},
            created_at=utc_now(),
        )
        data.update(overrides)
        return InvocationRequest(**data)

    def configure_fake(self, mode="ok", body=None):
        os.environ["FAKE_MODE"] = mode
        os.environ["FAKE_OUTPUT"] = str(
            self.job_dir / "round-1" / "review-code.md")
        if body is not None:
            os.environ["FAKE_BODY"] = body
        else:
            os.environ.pop("FAKE_BODY", None)

    def config_path(self, request):
        return (self.job_dir / "runs" / request.invocation_id / "harness" / "xdg"
                / "kilo" / "kilo.jsonc")

    def test_probe_reports_kilo_capabilities(self):
        report = self.adapter.probe()
        self.assertEqual(report.version, "kilo 9.9.9")
        self.assertEqual(report.enforcement("machine_readable_events"), "native")
        self.assertEqual(report.enforcement("browser_tooling"), "none")

    def test_run_normalizes_events_and_artifact(self):
        request = self.request()
        self.configure_fake()
        handle = self.adapter.start(request)
        events = list(self.adapter.events(handle))
        result = handle.result
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.session_id, "ses_fake")
        self.assertEqual([e.type for e in events][-2:], ["artifact", "finished"])
        usage = result.usage
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertEqual(usage["reasoning_tokens"], 2)
        self.assertEqual(usage["cache_read_tokens"], 10)
        self.assertEqual(usage["cache_write_tokens"], 3)
        self.assertEqual(usage["cost_usd"], 0.0123)
        self.assertEqual(usage["cost_kind"], "provider_reported")
        self.assertEqual(result.artifacts[0].path,
                         str(self.job_dir / "round-1" / "review-code.md"))

    def test_generated_config_isolates_reviewer(self):
        request = self.request()
        self.configure_fake()
        handle = self.adapter.start(request)
        config = json.loads(self.config_path(request).read_text())
        self.assertEqual(config["mcp"], {})
        self.assertEqual(config["plugins"], [])
        agent = config["agent"]["factory-code"]
        permission = agent["permission"]
        self.assertEqual(permission["bash"], "deny")
        self.assertEqual(permission["webfetch"], "deny")
        self.assertEqual(permission["websearch"], "deny")
        self.assertEqual(permission["skill"], "deny")
        self.assertEqual(permission["edit"]["*"], "deny")
        self.assertEqual(permission["edit"][str(self.job_dir / "round-1" / "review-code.md")],
                         "allow")
        self.assertIn(str(self.job_dir) + "/*", permission["external_directory"])
        self.assertEqual(agent["prompt"], (REPO / "factory/roles/reviewer-code.md")
                         .read_text().split("---\n", 2)[-1].strip() + "\n")
        list(self.adapter.events(handle))

    def test_builder_gets_shell_network_and_worktree_edits(self):
        request = self.request(
            role="builder", stage="build", outputs=["build.md"],
            profile="kilo-builder", permission_profile="builder",
            permissions={"shell": True, "network": True,
                         "write_scope": "worktree_and_output"})
        os.environ["FAKE_OUTPUT"] = str(self.job_dir / "build.md")
        os.environ["FAKE_BODY"] = "STATUS: READY\n"
        os.environ["FAKE_MODE"] = "ok"
        handle = self.adapter.start(request)
        config = json.loads(self.config_path(request).read_text())
        permission = config["agent"]["factory-builder"]["permission"]
        self.assertEqual(permission["bash"], "allow")
        self.assertEqual(permission["webfetch"], "allow")
        self.assertEqual(permission["edit"], "allow")
        list(self.adapter.events(handle))

    def test_intake_has_no_shell_and_writes_only_its_output(self):
        request = self.request(
            role="intake", stage="intake", worktree=None, branch=None,
            outputs=["intake.md"], profile="kilo-intake",
            permission_profile="intake",
            permissions={"shell": False, "network": False,
                         "write_scope": "output_only"})
        os.environ["FAKE_OUTPUT"] = str(self.job_dir / "intake.md")
        os.environ["FAKE_BODY"] = "DECISION: ACCEPT\n"
        os.environ["FAKE_MODE"] = "ok"
        handle = self.adapter.start(request)
        config = json.loads(self.config_path(request).read_text())
        permission = config["agent"]["factory-intake"]["permission"]
        self.assertEqual(permission["bash"], "deny")
        self.assertEqual(permission["webfetch"], "deny")
        self.assertEqual(permission["edit"], {"*": "deny",
                                             str(self.job_dir / "intake.md"): "allow"})
        list(self.adapter.events(handle))

    def test_error_event_yields_failed(self):
        request = self.request()
        self.configure_fake(mode="fail")
        handle = self.adapter.start(request)
        list(self.adapter.events(handle))
        self.assertEqual(handle.result.status, "failed")
        self.assertEqual(handle.result.exit_code, 1)
        self.assertIn("boom", handle.result.error["message"])

    def test_invalid_artifact_first_line_fails(self):
        request = self.request()
        self.configure_fake(body="VERDICT: LGTM\n")
        handle = self.adapter.start(request)
        list(self.adapter.events(handle))
        self.assertEqual(handle.result.status, "failed")
        self.assertIn("VERDICT", handle.result.error["message"])

    def test_missing_artifact_fails(self):
        request = self.request()
        os.environ["FAKE_MODE"] = "ok"
        os.environ["FAKE_OUTPUT"] = str(self.job_dir / "elsewhere.md")
        handle = self.adapter.start(request)
        list(self.adapter.events(handle))
        self.assertEqual(handle.result.status, "failed")
        self.assertIn("without writing", handle.result.error["message"])

    def test_cancel_stops_the_owned_process(self):
        request = self.request()
        os.environ["FAKE_MODE"] = "sleep"
        handle = self.adapter.start(request)
        collector = []
        thread = threading.Thread(target=lambda: collector.extend(self.adapter.events(handle)))
        thread.start()
        self.adapter.cancel(handle, grace_seconds=1)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(handle.result.status, "cancelled")

    def test_deadline_marks_timed_out(self):
        request = self.request(deadline_seconds=1, cancel_grace_seconds=1)
        os.environ["FAKE_MODE"] = "sleep"
        handle = self.adapter.start(request)
        list(self.adapter.events(handle))
        self.assertEqual(handle.result.status, "timed_out")


if __name__ == "__main__":
    unittest.main()
