"""Deterministic fake harness.

It writes contract-valid artifacts and emits the normalized event sequence
without calling a model. Tests use it to drive every transition, and
``factory/bin/run --smoke`` uses it to prove the controller wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..core.artifacts import atomic_write
from ..core.capabilities import CAPABILITY_NAMES, CapabilityReport
from ..core.contracts import (
    Artifact,
    Event,
    InvocationRequest,
    InvocationResult,
    normalize_usage,
    sha256_file,
    utc_now,
)
from .base import Adapter, AdapterError, Handle

_BODIES = {
    "intake": "DECISION: ACCEPT\nRISK: low\nSURFACES: none\nRESCOPED: no\n\n"
              "## Summary\n- Fake intake accepted the request\n",
    "spec": "SPEC: Fake feature\n\n# Fake feature\n\n## Summary\n- Fake spec\n",
    "spec-writer": "SPEC: Fake feature\n\n# Fake feature\n\n## Summary\n- Fake spec\n",
    "builder": "STATUS: READY\n\n# Build: Fake feature\n\n## Summary\n- Fake build\n",
    "code": "VERDICT: PASS\n\n## Summary\n- Fake code review passed\n",
    "security": "VERDICT: PASS\n\n## Summary\n- Fake security review passed\n",
    "ux": "VERDICT: PASS\n\n## Summary\n- Fake UX review passed\n",
    "ui": "VERDICT: PASS\n\n## Summary\n- Fake UI review passed\n",
    "approver": "APPROVE\n\n## Summary\n- Fake approval\n",
}

_DEFAULT_OUTPUT = {
    "intake": "intake.md",
    "spec": "spec.md",
    "spec-writer": "spec.md",
    "builder": "build.md",
    "code": "round-{round}/review-code.md",
    "security": "round-{round}/review-security.md",
    "ux": "round-{round}/review-ux.md",
    "ui": "round-{round}/review-ui.md",
    "approver": "decision.md",
}


class FakeAdapter(Adapter):
    name = "fake"

    def __init__(self, factory_root: str | Path, *, now: str | None = None):
        self.factory_root = Path(factory_root)
        self._now = now

    def probe(self) -> CapabilityReport:
        return CapabilityReport(
            adapter=self.name,
            version="fake-1",
            capabilities={name: "native" for name in CAPABILITY_NAMES},
        )

    def _output(self, request: InvocationRequest) -> str:
        if request.outputs:
            return request.outputs[0]
        try:
            return _DEFAULT_OUTPUT[request.role].format(round=request.round)
        except KeyError:
            raise AdapterError(f"fake adapter has no output for role {request.role!r}") from None

    def start(self, request: InvocationRequest) -> Handle:
        request.validate()
        if request.role not in _BODIES:
            raise AdapterError(f"fake adapter has no body for role {request.role!r}")
        job_dir = Path(request.job_dir)
        relative = Path(self._output(request))
        if relative.is_absolute() or not (job_dir / relative).resolve().is_relative_to(job_dir.resolve()):
            raise AdapterError("fake output must stay inside the job directory")
        path = job_dir / relative
        text = _BODIES[request.role]
        atomic_write(path, text)
        artifact = Artifact(path=str(path), sha256=sha256_file(path), role=request.role)
        started = self._now or utc_now()
        result = InvocationResult(
            invocation_id=request.invocation_id,
            status="completed",
            exit_code=0,
            session_id=f"fake-{request.invocation_id[:8]}",
            adapter=self.name,
            harness_version="fake-1",
            provider=request.provider,
            model_requested=request.model,
            model_observed=request.model,
            started_at=started,
            ended_at=self._now or utc_now(),
            artifacts=[artifact],
            usage=normalize_usage({"input_tokens": 10, "output_tokens": 5,
                                   "cost_usd": None, "cost_kind": "unknown"}),
        )
        return Handle(request=request, result=result, metadata={"artifact": artifact})

    def events(self, handle: Handle) -> Iterator[Event]:
        now = self._now or utc_now()
        yield Event(invocation_id=handle.request.invocation_id, type="started",
                    timestamp=now, data={"adapter": self.name})
        if handle.result.usage is not None:
            yield Event(invocation_id=handle.request.invocation_id, type="usage",
                        timestamp=now, data=handle.result.usage)
        for artifact in handle.result.artifacts:
            yield Event(invocation_id=handle.request.invocation_id, type="artifact",
                        timestamp=now, data={"path": artifact.path,
                                             "sha256": artifact.sha256})
        yield Event(invocation_id=handle.request.invocation_id, type="finished",
                    timestamp=now, data={"status": handle.result.status})
