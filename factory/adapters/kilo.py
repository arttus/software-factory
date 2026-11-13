"""Process-backed Kilo Code adapter.

Kilo 7.8.12 is driven through ``kilo run --format json``. Each invocation gets
an isolated XDG config containing exactly one generated agent, so inherited
MCP servers and plugins do not leak into a worker. Tool permissions come from
the resolved permission profile in the request:

- ``shell`` toggles the bash tool.
- ``network`` toggles webfetch and websearch.
- ``write_scope`` decides whether edit is unrestricted or narrowed to the
  assigned output file (and, for builders, the worktree).

Kilo events are normalized into the factory contract. It is the adapter's job
to translate ``step_start`` / ``text`` / ``tool_use`` / ``step_finish`` /
``error`` and to prove the artifact exists and matches the role's first line.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Iterator

from ..core.artifacts import ArtifactError, atomic_write, canonical_path, validate_output
from ..core.capabilities import CapabilityReport
from ..core.contracts import (
    Artifact,
    Event,
    InvocationRequest,
    InvocationResult,
    normalize_usage,
    sha256_file,
    utc_now,
)
from ..core.roles import Role, load_roles
from .base import Adapter, AdapterError, Handle

ROLE_FILES = {
    "intake": "intake",
    "spec": "spec-writer",
    "builder": "builder",
    "approver": "approver",
    "code": "reviewer-code",
    "security": "reviewer-security",
    "ux": "reviewer-ux",
    "ui": "reviewer-ui",
}

_SCHEMA_URL = "https://app.kilo.ai/config.json"
_TAIL = 40


class KiloAdapter(Adapter):
    name = "kilo"

    def __init__(self, factory_root: str | Path, executable: str | None = None):
        self.factory_root = Path(factory_root)
        self.executable = executable or shutil.which("kilo") or "kilo"
        self._roles: dict[str, Role] | None = None

    def probe(self) -> CapabilityReport:
        version = None
        try:
            result = subprocess.run([self.executable, "--version"], capture_output=True,
                                    text=True, timeout=10)
            if result.returncode == 0:
                version = (result.stdout or result.stderr).strip().splitlines()[-1]
        except (OSError, subprocess.TimeoutExpired):
            pass
        return CapabilityReport(
            adapter=self.name,
            version=version,
            capabilities={
                "noninteractive": "native",
                "machine_readable_events": "native",
                "model_selection": "native",
                "session_resume": "native",
                "usage_reporting": "native",
                "cancellation": "native",
                "fs_restrictions": "native",
                "shell_control": "native",
                "network_control": "native",
                # Kilo has no browser tool in this release; UI roles must block.
                "browser_tooling": "none",
            },
        )

    def _role(self, request: InvocationRequest) -> Role:
        if self._roles is None:
            self._roles = load_roles(request.controller_root)
        try:
            return self._roles[ROLE_FILES[request.role]]
        except KeyError:
            raise AdapterError(f"kilo adapter has no role for {request.role!r}") from None

    def _output(self, request: InvocationRequest, role: Role) -> Path:
        canonical = canonical_path(request.job_dir, role, request.round)
        expected = canonical.relative_to(Path(request.job_dir)).as_posix()
        if request.outputs and request.outputs[0] != expected:
            raise AdapterError(f"output {request.outputs[0]!r} does not match "
                               f"role {role.name!r} ({expected!r})")
        return canonical

    def _permissions(self, request: InvocationRequest, output: Path) -> dict:
        permissions = request.permissions or {
            "shell": False, "network": False, "write_scope": "output_only",
        }
        shell = bool(permissions["shell"])
        network = bool(permissions["network"])
        scope = permissions["write_scope"]
        worktree = request.worktree

        if scope == "worktree_and_output":
            edit: object = "allow"
        else:
            edit = {"*": "deny"}
            if scope == "worktree_only":
                if not worktree:
                    raise AdapterError("worktree_only requires a worktree")
                edit[str(worktree) + "/*"] = "allow"
            else:
                edit[str(output)] = "allow"

        external = {"*": "deny", str(output): "allow",
                    str(Path(request.job_dir)) + "/*": "allow"}
        if worktree:
            external[str(worktree) + "/*"] = "allow"
        for root in {tempfile.gettempdir(), "/tmp", "/private/tmp"}:
            external[str(Path(root)) + "/*"] = "allow"
        tool_output = Path.home() / ".local" / "share" / "kilo" / "tool-output"
        external[str(tool_output) + "/*"] = "allow"

        return {
            "read": {"*": "allow"},
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "edit": edit,
            "bash": "allow" if shell else "deny",
            "webfetch": "allow" if network else "deny",
            "websearch": "allow" if network else "deny",
            "task": "deny",
            "todowrite": "deny",
            "question": "deny",
            "lsp": "deny",
            "skill": "deny",
            "notebook_read": "allow",
            "notebook_edit": "deny",
            "notebook_execute": "deny",
            "external_directory": external,
        }

    def _config(self, request: InvocationRequest, role: Role, output: Path) -> tuple[Path, str]:
        agent = f"factory-{request.role}"
        config = {
            "$schema": _SCHEMA_URL,
            "mcp": {},
            "plugins": [],
            "agent": {
                agent: {
                    "mode": "primary",
                    "description": f"Factory {role.name}",
                    "prompt": role.instructions,
                    "permission": self._permissions(request, output),
                }
            },
        }
        home = Path(request.job_dir) / "runs" / request.invocation_id / "harness" / "xdg"
        path = home / "kilo" / "kilo.jsonc"
        atomic_write(path, json.dumps(config, indent=2) + "\n")
        return home, agent

    def _command(self, request: InvocationRequest, agent: str, output: Path) -> list[str]:
        directory = request.worktree or request.controller_root
        if request.role != "intake" and not request.worktree:
            raise AdapterError(f"{request.role} requires a worktree")
        message = (
            f"Job: {request.job_id}. Job folder: {request.job_dir}. "
            f"Controller checkout: {request.controller_root}. "
            f"Worktree: {request.worktree or request.controller_root}. "
            f"Branch: {request.branch or '-'}. Stage: {request.stage}. "
            f"Round: {request.round}. Output file: {output}. "
            "Follow your role instructions. Write only the output file; do not "
            "write any other factory file, and do not merge or deploy."
        )
        command = [self.executable, "run", message, "--format", "json",
                   "--dir", str(directory), "-m", request.model,
                   "--agent", agent, "--pure", "--auto"]
        if request.session_id:
            command += ["--session", request.session_id]
        return command

    def start(self, request: InvocationRequest) -> Handle:
        request.validate()
        role = self._role(request)
        output = self._output(request, role)
        home, agent = self._config(request, role, output)
        command = self._command(request, agent, output)

        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(home)
        output.parent.mkdir(parents=True, exist_ok=True)
        started = utc_now()
        try:
            process = subprocess.Popen(
                command, cwd=request.controller_root, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
                start_new_session=True, env=env,
            )
        except OSError as exc:
            raise AdapterError(f"could not launch kilo: {exc}") from None

        stderr_tail: deque[str] = deque(maxlen=_TAIL)

        def read_stderr() -> None:
            assert process.stderr is not None
            try:
                for line in process.stderr:
                    stderr_tail.append(line.rstrip())
            except ValueError:
                pass

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()

        result = InvocationResult(
            invocation_id=request.invocation_id, status="failed", exit_code=None,
            adapter=self.name, provider=request.provider,
            model_requested=request.model, started_at=started, ended_at=started,
        )
        metadata = {
            "output": output, "role": role, "process": process,
            "stderr": stderr_tail, "cancel": threading.Event(),
            "timed_out": False, "usage": {}, "seen_usage": False,
            "error": None, "timer": None, "stderr_thread": stderr_thread,
        }
        handle = Handle(request=request, result=result, native=process, metadata=metadata)
        if request.deadline_seconds:
            timer = threading.Timer(request.deadline_seconds, self._deadline, [handle])
            timer.daemon = True
            timer.start()
            metadata["timer"] = timer
        return handle

    def _deadline(self, handle: Handle) -> None:
        handle.metadata["timed_out"] = True
        self.cancel(handle, handle.request.cancel_grace_seconds)

    def _error_message(self, data: dict) -> str:
        error = data.get("error")
        if isinstance(error, dict):
            payload = error.get("data") or error
            if isinstance(payload, dict) and payload.get("message"):
                return str(payload["message"])
            if error.get("name"):
                return str(error["name"])
        return "kilo reported an error"

    def _usage(self, data: dict, metadata: dict) -> None:
        part = data.get("part") or {}
        tokens = part.get("tokens") or {}
        metadata["seen_usage"] = True
        usage = metadata["usage"]
        usage["input_tokens"] = usage.get("input_tokens", 0) + int(tokens.get("input", 0) or 0)
        usage["output_tokens"] = usage.get("output_tokens", 0) + int(tokens.get("output", 0) or 0)
        usage["reasoning_tokens"] = usage.get("reasoning_tokens", 0) + int(tokens.get("reasoning", 0) or 0)
        cache = tokens.get("cache") or {}
        usage["cache_read_tokens"] = usage.get("cache_read_tokens", 0) + int(cache.get("read", 0) or 0)
        usage["cache_write_tokens"] = usage.get("cache_write_tokens", 0) + int(cache.get("write", 0) or 0)
        cost = part.get("cost")
        if cost is not None:
            usage["cost_usd"] = usage.get("cost_usd", 0.0) + float(cost)
            usage["cost_kind"] = "provider_reported"

    def events(self, handle: Handle) -> Iterator[Event]:
        process = handle.metadata["process"]
        assert process.stdout is not None
        started = handle.result.started_at
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    yield Event(invocation_id=handle.request.invocation_id, type="progress",
                                timestamp=utc_now(), data={"raw": line[:500]})
                    continue
                kind = data.get("type", "unknown")
                session = data.get("sessionID")
                if session:
                    handle.result.session_id = session
                if kind == "step_start":
                    yield Event(invocation_id=handle.request.invocation_id, type="progress",
                                timestamp=utc_now(), data={"phase": "step_start"})
                elif kind == "text":
                    text = (data.get("part") or {}).get("text", "")
                    yield Event(invocation_id=handle.request.invocation_id, type="progress",
                                timestamp=utc_now(), data={"phase": "text", "text": text})
                elif kind == "tool_use":
                    part = data.get("part") or {}
                    yield Event(invocation_id=handle.request.invocation_id, type="progress",
                                timestamp=utc_now(), data={"phase": "tool_use",
                                                           "tool": part.get("tool") or part.get("name"),
                                                           "state": (part.get("state") or {}).get("status")})
                elif kind == "step_finish":
                    self._usage(data, handle.metadata)
                    usage = normalize_usage(handle.metadata["usage"])
                    yield Event(invocation_id=handle.request.invocation_id, type="usage",
                                timestamp=utc_now(), data=usage or {})
                elif kind == "error":
                    handle.metadata["error"] = self._error_message(data)
                    yield Event(invocation_id=handle.request.invocation_id, type="progress",
                                timestamp=utc_now(), data={"phase": "error",
                                                           "message": handle.metadata["error"]})
                else:
                    yield Event(invocation_id=handle.request.invocation_id, type="progress",
                                timestamp=utc_now(), data={"phase": kind})
        finally:
            timer = handle.metadata.get("timer")
            if timer is not None:
                timer.cancel()
            returncode = process.wait()
            yield from self._finish(handle, returncode, started)

    def _finish(self, handle: Handle, returncode: int, started: str) -> Iterator[Event]:
        metadata = handle.metadata
        process = metadata["process"]
        thread = metadata.get("stderr_thread")
        if thread is not None:
            thread.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except (AttributeError, ValueError):
                pass
        result = handle.result
        output: Path = metadata["output"]
        role: Role = metadata["role"]
        result.exit_code = returncode
        result.ended_at = utc_now()

        error = metadata["error"]
        artifact = None
        if output.is_file():
            text = output.read_text()
            try:
                validate_output(role, text)
                artifact = Artifact(path=str(output), sha256=sha256_file(output),
                                    role=handle.request.role)
                result.artifacts = [artifact]
            except ArtifactError as exc:
                error = str(exc)
        else:
            error = error or f"kilo finished without writing {output.name}"

        if metadata["timed_out"]:
            result.status = "timed_out"
        elif metadata["cancel"].is_set():
            result.status = "cancelled"
        elif error is not None or returncode != 0:
            result.status = "failed"
        else:
            result.status = "completed"

        if result.status == "completed" and artifact is None:
            result.status = "failed"
            error = error or "no valid artifact"
        if error is not None:
            tail = "\n".join(metadata["stderr"])
            result.error = {"message": error, "exit_code": returncode,
                            "stderr": tail[-2000:] if tail else None}
        if metadata["seen_usage"]:
            result.usage = normalize_usage(metadata["usage"])

        if artifact is not None:
            yield Event(invocation_id=handle.request.invocation_id, type="artifact",
                        timestamp=utc_now(), data={"path": artifact.path,
                                                   "sha256": artifact.sha256})
        yield Event(invocation_id=handle.request.invocation_id, type="finished",
                    timestamp=utc_now(), data={"status": result.status,
                                               "exit_code": returncode})

    def cancel(self, handle: Handle, grace_seconds: int = 5) -> None:
        handle.metadata["cancel"].set()
        process: subprocess.Popen = handle.native
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + max(grace_seconds, 0)
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.1)
        if process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
