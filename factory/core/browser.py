"""Controller-provided browser evidence for UI review.

The controller, not the worker, starts the app and drives the browser. That
keeps UI roles read-only: they receive screenshots and accessibility snapshots
as evidence instead of getting shell access.

Configuration (``config.json``)::

    "browser": {
      "command": "agent-browser",
      "start_command": ["npm", "run", "dev", "--", "--port", "{port}"],
      "url": "http://127.0.0.1:{port}/",
      "ready_timeout_seconds": 60,
      "viewports": [
        {"name": "desktop", "width": 1440, "height": 900},
        {"name": "mobile", "width": 390, "height": 844}
      ]
    }
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .contracts import Artifact, sha256_file

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_TAIL = 40


class BrowserError(RuntimeError):
    """Browser evidence could not be produced."""


@dataclass(frozen=True)
class Viewport:
    name: str
    width: int
    height: int


@dataclass(frozen=True)
class BrowserSettings:
    command: str
    start_command: tuple[str, ...] | None
    url: str | None
    ready_timeout_seconds: int
    viewports: tuple[Viewport, ...]

    def configured(self) -> bool:
        return bool(self.start_command and self.url)


def settings_from(raw: dict | None) -> BrowserSettings:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise BrowserError("config.browser must be an object")
    command = raw.get("command", "agent-browser")
    if not isinstance(command, str) or not command.strip():
        raise BrowserError("config.browser.command must be a command name")
    start = raw.get("start_command")
    if start is not None:
        if not isinstance(start, list) or not all(isinstance(a, str) for a in start):
            raise BrowserError("config.browser.start_command must be a list of strings")
        start = tuple(start)
    url = raw.get("url")
    if url is not None and not isinstance(url, str):
        raise BrowserError("config.browser.url must be a string")
    timeout = raw.get("ready_timeout_seconds", 60)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise BrowserError("config.browser.ready_timeout_seconds must be a positive integer")
    viewports = []
    for item in raw.get("viewports", []):
        if not isinstance(item, dict):
            raise BrowserError("config.browser.viewports entries must be objects")
        try:
            viewports.append(Viewport(str(item["name"]), int(item["width"]), int(item["height"])))
        except (KeyError, TypeError, ValueError):
            raise BrowserError("each viewport needs name, width and height") from None
    if not viewports:
        viewports = [Viewport("desktop", 1440, 900), Viewport("mobile", 390, 844)]
    return BrowserSettings(command=command, start_command=start, url=url,
                           ready_timeout_seconds=timeout, viewports=tuple(viewports))


def available(settings: BrowserSettings) -> bool:
    return shutil.which(settings.command) is not None


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 60) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrowserError(f"{command[0]} failed: {exc}") from None
    if result.returncode != 0:
        raise BrowserError(f"`{' '.join(command)}` failed: "
                           f"{(result.stderr or result.stdout).strip()}")
    return result.stdout


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        if _port_free(port):
            return port
    raise BrowserError(f"no free port near {preferred}")


def _wait_ready(url: str, timeout: int, server: subprocess.Popen) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise BrowserError(f"dev server exited with {server.returncode} before {url} was ready")
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.5)
    raise BrowserError(f"{url} was not ready within {timeout}s")


def _session_name(job_id: str, viewport: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", f"factory-{job_id}-{viewport}")


def _existing(evidence_dir: Path) -> list[Artifact]:
    files = sorted(p for p in evidence_dir.iterdir()
                   if p.is_file() and (p.suffix.lower() in IMAGE_SUFFIXES or p.suffix == ".txt"))
    return [Artifact(path=str(p), sha256=sha256_file(p)) for p in files]


def capture(*, job_dir: str | Path, worktree: str | Path, job: dict,
            settings: BrowserSettings, port: int,
            log=print) -> list[Artifact]:
    """Start the app, capture each viewport, and stop everything we own."""
    if not settings.configured():
        raise BrowserError("browser evidence is not configured: set "
                           "config.browser.start_command and config.browser.url")
    if not available(settings):
        raise BrowserError(f"browser command {settings.command!r} is not installed")

    job_dir, worktree = Path(job_dir), Path(worktree)
    evidence_dir = job_dir / f"round-{job['round']}" / "browser"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    existing = _existing(evidence_dir)
    if any(p.suffix.lower() in IMAGE_SUFFIXES for p in map(lambda a: Path(a.path), existing)):
        log(f"{job['id']}: reusing browser evidence in {evidence_dir}")
        return existing

    port = free_port(port)
    url = (settings.url or "").replace("{port}", str(port))
    command = [arg.replace("{port}", str(port)) for arg in settings.start_command or ()]
    env = os.environ.copy()
    env["PORT"] = str(port)
    server_log = (evidence_dir / "server.log").open("w")
    server = subprocess.Popen(command, cwd=worktree, stdout=server_log,
                              stderr=subprocess.STDOUT, start_new_session=True, env=env)
    try:
        _wait_ready(url, settings.ready_timeout_seconds, server)
        for viewport in settings.viewports:
            session = _session_name(job["id"], viewport.name)
            base = [settings.command, "--session", session]
            _run(base + ["set", "viewport", str(viewport.width), str(viewport.height)])
            _run(base + ["open", url])
            shot = evidence_dir / f"{viewport.name}.png"
            snap = evidence_dir / f"{viewport.name}.txt"
            _run(base + ["screenshot", str(shot)])
            snap.write_text(_run(base + ["snapshot", "-i", "--json"]))
            _run(base + ["close"])
            log(f"{job['id']}: captured {viewport.name} browser evidence")
    finally:
        try:
            os.killpg(os.getpgid(server.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(server.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        server_log.close()
        log_text = (evidence_dir / "server.log").read_text(errors="replace")
        if server.returncode not in (0, -signal.SIGTERM):
            (evidence_dir / "server.log").write_text(
                log_text + f"\n... dev server exited {server.returncode} ...\n")
    return _existing(evidence_dir)


def summary(artifacts: list[Artifact]) -> str:
    images = [Path(a.path).name for a in artifacts if Path(a.path).suffix.lower() in IMAGE_SUFFIXES]
    return "browser evidence: " + (", ".join(images) if images else "none")
