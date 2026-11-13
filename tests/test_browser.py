import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from factory.core.browser import BrowserError, BrowserSettings, Viewport, available, capture

FAKE_BROWSER = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
argv = sys.argv[1:]
log = os.environ.get("FAKE_BROWSER_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(argv) + "\n")
# argv: --session <name> <command> ...
i = argv.index("--session") if "--session" in argv else -1
cmd = argv[i+2:] if i >= 0 else argv
if cmd[:1] == ["set"]:
    pass
elif cmd[:1] == ["open"]:
    pass
elif cmd[:1] == ["screenshot"]:
    pathlib.Path(cmd[1]).write_bytes(b"\x89PNG\r\n\x1a\nfake")
elif cmd[:1] == ["snapshot"]:
    print(json.dumps({"tree": "ok"}))
elif cmd[:1] == ["close"]:
    pass
else:
    sys.exit(f"unsupported {cmd}")
'''


class BrowserCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        (self.worktree / "index.html").write_text("<html><body><h1>Hello</h1></body></html>")
        self.job_dir = self.root / "job"
        self.browser = self.root / "agent-browser"
        self.browser.write_text(FAKE_BROWSER)
        self.browser.chmod(0o755)
        self.log = self.root / "browser.log"
        self._env = {k: os.environ.get(k) for k in ("FAKE_BROWSER_LOG",)}
        os.environ["FAKE_BROWSER_LOG"] = str(self.log)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def settings(self):
        return BrowserSettings(
            command=str(self.browser),
            start_command=(sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"),
            url="http://127.0.0.1:{port}/",
            ready_timeout_seconds=20,
            viewports=(Viewport("desktop", 1440, 900), Viewport("mobile", 390, 844)),
        )

    def test_capture_produces_screenshots_and_snapshots(self):
        artifacts = capture(job_dir=self.job_dir, worktree=self.worktree,
                            job={"id": "001-demo", "round": 1},
                            settings=self.settings(), port=5800, log=lambda _: None)
        names = sorted(Path(a.path).name for a in artifacts)
        self.assertEqual(names, ["desktop.png", "desktop.txt", "mobile.png", "mobile.txt"])
        for artifact in artifacts:
            self.assertTrue(Path(artifact.path).is_file())
            self.assertTrue(artifact.sha256)
        commands = self.log.read_text().splitlines()
        joined = "\n".join(commands)
        self.assertIn('"set", "viewport", "1440", "900"', joined)
        self.assertIn('"set", "viewport", "390", "844"', joined)
        self.assertEqual(joined.count('"screenshot"'), 2)
        self.assertEqual(joined.count('"snapshot"'), 2)
        self.assertIn('"close"', joined)

    def test_reuses_existing_evidence(self):
        first = capture(job_dir=self.job_dir, worktree=self.worktree,
                        job={"id": "001-demo", "round": 1},
                        settings=self.settings(), port=5800, log=lambda _: None)
        self.log.unlink()
        second = capture(job_dir=self.job_dir, worktree=self.worktree,
                         job={"id": "001-demo", "round": 1},
                         settings=self.settings(), port=5800, log=lambda _: None)
        self.assertEqual([a.path for a in first], [a.path for a in second])
        self.assertFalse(self.log.exists())

    def test_unconfigured_reports_clearly(self):
        settings = BrowserSettings(command=str(self.browser), start_command=None,
                                   url=None, ready_timeout_seconds=5, viewports=())
        with self.assertRaises(BrowserError):
            capture(job_dir=self.job_dir, worktree=self.worktree,
                    job={"id": "001-demo", "round": 1}, settings=settings, port=5800)

    def test_missing_command_reports_clearly(self):
        settings = self.settings()
        settings = BrowserSettings(command=str(self.root / "nope"), start_command=settings.start_command,
                                   url=settings.url, ready_timeout_seconds=5,
                                   viewports=settings.viewports)
        self.assertFalse(available(settings))
        with self.assertRaises(BrowserError):
            capture(job_dir=self.job_dir, worktree=self.worktree,
                    job={"id": "001-demo", "round": 1}, settings=settings, port=5800)


if __name__ == "__main__":
    unittest.main()
