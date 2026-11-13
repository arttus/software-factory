import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from factory.adapters.command_code import CommandCodeAdapter
from factory.core.contracts import InvocationRequest, new_invocation_id, utc_now

REPO = Path(__file__).resolve().parents[1]

FAKE_CMD = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time

argv = sys.argv[1:]
if "--version" in argv:
    print("1.53.1")
    sys.exit(0)

log = os.environ.get("FAKE_LOG")
if log:
    pathlib.Path(log).write_text(json.dumps(argv))

mode = os.environ.get("FAKE_MODE", "ok")
session = "sess_fake"
if mode == "sleep":
    print(json.dumps({"type": "event", "event": {"type": "run_start", "sessionId": session}}), flush=True)
    time.sleep(30)
    sys.exit(0)
if mode == "fail":
    print(json.dumps({"type": "event", "event": {"type": "run_start", "sessionId": session}}), flush=True)
    print(json.dumps({"type": "event", "event": {"type": "run_error",
                                                 "error": {"message": "boom"}}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "error", "sessionId": session,
                      "error": "boom", "usage": {}}), flush=True)
    sys.exit(4)

body = os.environ.get("FAKE_FINAL", "DECISION: ACCEPT\n")
out = os.environ.get("FAKE_OUTPUT")
if out and os.environ.get("FAKE_WRITE_FILE") == "1":
    path = pathlib.Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)

usage = {"inputTokens": 100, "outputTokens": 10, "cacheReadTokens": 5, "cacheWriteTokens": 2}
print(json.dumps({"type": "event", "event": {"type": "run_start", "sessionId": session}}), flush=True)
print(json.dumps({"type": "event", "event": {"type": "text_delta", "text": "working"}}), flush=True)
print(json.dumps({"type": "event", "event": {"type": "tool_queued", "tool": "write_file"}}), flush=True)
print(json.dumps({"type": "event", "event": {"type": "run_end", "result": {
    "finalText": body, "usage": usage, "stopReason": "end_turn"}}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "sessionId": session,
                  "usage": usage, "finalText": body}), flush=True)
'''


class CommandCodeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.script = self.root / "cmd"
        self.script.write_text(FAKE_CMD)
        self.script.chmod(0o755)
        self.adapter = CommandCodeAdapter(self.root, executable=str(self.script))
        self.job_dir = self.root / "jobs" / "001-demo"
        self.job_dir.mkdir(parents=True)
        self.worktree = self.root / ".worktrees" / "001-demo"
        self.worktree.mkdir(parents=True)
        self._old = {k: os.environ.get(k) for k in
                     ("FAKE_LOG", "FAKE_MODE", "FAKE_FINAL", "FAKE_OUTPUT", "FAKE_WRITE_FILE")}

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def request(self, **overrides):
        data = dict(
            invocation_id=new_invocation_id(), job_id="001-demo", stage="intake",
            round=0, attempt=1, role="intake", controller_root=str(REPO),
            job_dir=str(self.job_dir), worktree=None, branch=None, base_branch="main",
            instruction_path="factory/roles/intake.md", instruction_digest="e" * 64,
            outputs=["intake.md"], profile="cc-intake", adapter="command-code",
            provider="commandcode", model="claude-sonnet-5",
            required_capabilities=["noninteractive"], permission_profile="intake",
            permissions={"shell": False, "network": False, "write_scope": "output_only"},
            created_at=utc_now(),
        )
        data.update(overrides)
        return InvocationRequest(**data)

    def log_argv(self, request):
        log = self.root / "argv.json"
        os.environ["FAKE_LOG"] = str(log)
        return log

    def test_probe_reports_version_and_browser_capability(self):
        report = self.adapter.probe()
        self.assertEqual(report.version, "1.53.1")
        self.assertEqual(report.enforcement("machine_readable_events"), "native")
        self.assertIn(report.enforcement("browser_tooling"), ("none", "external"))
        self.assertEqual(CommandCodeAdapter(self.root, executable=str(self.script),
                                            browser_available=False).probe()
                         .enforcement("browser_tooling"), "none")
        self.assertEqual(CommandCodeAdapter(self.root, executable=str(self.script),
                                            browser_available=True).probe()
                         .enforcement("browser_tooling"), "external")

    def test_read_only_role_writes_artifact_from_final_text(self):
        request = self.request()
        os.environ["FAKE_MODE"] = "ok"
        os.environ["FAKE_FINAL"] = "DECISION: ACCEPT\nRISK: low\n"
        log = self.log_argv(request)
        handle = self.adapter.start(request)
        events = list(self.adapter.events(handle))
        self.assertEqual(handle.result.status, "completed")
        self.assertEqual(handle.result.session_id, "sess_fake")
        self.assertEqual([e.type for e in events][-3:], ["usage", "artifact", "finished"])
        self.assertEqual((self.job_dir / "intake.md").read_text(),
                         "DECISION: ACCEPT\nRISK: low\n")
        argv = json.loads(log.read_text())
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertNotIn("--yolo", argv)
        self.assertNotIn("--tools-all", argv)
        self.assertEqual(handle.result.usage["input_tokens"], 100)
        self.assertEqual(handle.result.usage["cache_read_tokens"], 5)
        self.assertEqual(handle.result.usage["cost_kind"], "unknown")

    def test_builder_runs_with_tools_and_writes_its_output(self):
        request = self.request(
            role="builder", stage="build", round=1, worktree=str(self.worktree),
            branch="factory/001-demo", outputs=["build.md"], profile="cc-builder",
            permission_profile="builder",
            permissions={"shell": True, "network": True,
                         "write_scope": "worktree_and_output"})
        os.environ["FAKE_MODE"] = "ok"
        os.environ["FAKE_WRITE_FILE"] = "1"
        os.environ["FAKE_OUTPUT"] = str(self.job_dir / "build.md")
        os.environ["FAKE_FINAL"] = "STATUS: READY\n"
        log = self.log_argv(request)
        handle = self.adapter.start(request)
        list(self.adapter.events(handle))
        self.assertEqual(handle.result.status, "completed")
        argv = json.loads(log.read_text())
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto-accept")
        self.assertIn("--yolo", argv)
        self.assertIn("--tools-all", argv)
        self.assertTrue((self.job_dir / "build.md").is_file())

    def test_invalid_final_text_fails(self):
        request = self.request()
        os.environ["FAKE_MODE"] = "ok"
        os.environ["FAKE_FINAL"] = "Sure, here is the intake document:\n\n..."
        handle = self.adapter.start(request)
        list(self.adapter.events(handle))
        self.assertEqual(handle.result.status, "failed")
        self.assertIn("DECISION", handle.result.error["message"])

    def test_error_result_fails(self):
        request = self.request()
        os.environ["FAKE_MODE"] = "fail"
        handle = self.adapter.start(request)
        list(self.adapter.events(handle))
        self.assertEqual(handle.result.status, "failed")
        self.assertEqual(handle.result.exit_code, 4)
        self.assertIn("boom", handle.result.error["message"])

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

    def test_read_only_prompt_embeds_the_diff(self):
        worktree = self.worktree
        run = lambda *a: subprocess.run(["git", *a], cwd=worktree, capture_output=True,
                                        text=True, check=True)
        run("init", "-q")
        run("config", "user.email", "t@example.test")
        run("config", "user.name", "T")
        (worktree / "a.txt").write_text("one\n")
        run("add", ".")
        run("commit", "-qm", "base")
        run("update-ref", "refs/remotes/origin/main", "HEAD")
        (worktree / "a.txt").write_text("one\ntwo\n")
        run("commit", "-qam", "change")
        (self.job_dir / "spec.md").write_text("SPEC: demo\n")
        message = self.adapter._message(self.request(role="code", worktree=str(worktree),
                                                    outputs=["round-1/review-code.md"]),
                                        self.adapter._role(self.request(role="code")),
                                        self.job_dir / "round-1" / "review-code.md")
        self.assertIn("Job artifacts", message)
        self.assertIn("SPEC: demo", message)
        self.assertIn("Change under review", message)
        self.assertIn("+two", message)


if __name__ == "__main__":
    unittest.main()
