"""Local controller skeleton.

It runs one invocation through an adapter, records request/result/events under
``factory/jobs/<id>/runs/<invocation-id>/``, and validates the terminal
contract. It does not decide a job's stage or merge anything: those stay with
the authoritative state machine in ``factory/bin/job``.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..adapters import Adapter, build_registry
from .artifacts import atomic_write
from .capabilities import check_capabilities, check_permission_profile
from .config import FactoryConfig
from .contracts import Event, InvocationRequest, InvocationResult
from .leases import LeaseError, invocation_lease


class ControllerError(RuntimeError):
    """The controller cannot start or record an invocation."""


class LimitTracker:
    """In-process worker and per-adapter limits."""

    def __init__(self, limits: dict):
        self.workers = int(limits["workers"])
        self.per_adapter = dict(limits.get("per_adapter", {}))
        self._total = 0
        self._by_adapter: dict[str, int] = {}
        self._lock = threading.Lock()

    @contextmanager
    def slot(self, adapter: str):
        with self._lock:
            if self._total >= self.workers:
                raise ControllerError(f"worker limit {self.workers} reached")
            cap = self.per_adapter.get(adapter, self.workers)
            if self._by_adapter.get(adapter, 0) >= cap:
                raise ControllerError(f"adapter {adapter!r} limit {cap} reached")
            self._total += 1
            self._by_adapter[adapter] = self._by_adapter.get(adapter, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._total -= 1
                self._by_adapter[adapter] -= 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"total": self._total, "per_adapter": dict(self._by_adapter)}


@dataclass
class Controller:
    config: FactoryConfig
    factory_root: Path
    adapters: dict[str, Adapter]
    limits: LimitTracker

    @classmethod
    def from_config(cls, config: FactoryConfig, factory_root: str | Path,
                    adapters: dict[str, Adapter] | None = None) -> "Controller":
        factory_root = Path(factory_root)
        return cls(
            config=config,
            factory_root=factory_root,
            adapters=adapters if adapters is not None else build_registry(factory_root),
            limits=LimitTracker(config.limits),
        )

    def invocation_dir(self, request: InvocationRequest) -> Path:
        return self.factory_root / "jobs" / request.job_id / "runs" / request.invocation_id

    def run(self, request: InvocationRequest, adapter: Adapter | None = None) -> InvocationResult:
        request.validate()
        adapter = adapter or self.adapters.get(request.adapter)
        if adapter is None:
            raise ControllerError(f"no adapter registered for {request.adapter!r}")

        report = adapter.probe()
        problems = check_capabilities(request.role, report, browser=request.browser is not None)
        if problems:
            raise ControllerError("; ".join(problems))
        permission = self.config.permission(request.permission_profile)
        permission_problems = check_permission_profile(request.role, permission)
        if permission_problems:
            raise ControllerError("; ".join(permission_problems))

        invocation_dir = self.invocation_dir(request)
        invocation_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(invocation_dir / "request.json",
                     json.dumps(request.to_dict(), indent=2) + "\n")

        events_path = invocation_dir / "events.jsonl"
        try:
            with self.limits.slot(request.adapter):
                with invocation_lease(Path(request.job_dir), request.stage, request.role):
                    handle = adapter.start(request)
                    with events_path.open("w") as stream:
                        for event in adapter.events(handle):
                            self._validate_event(event, request)
                            stream.write(json.dumps(event.to_dict()) + "\n")
                            stream.flush()
                    result = handle.result
        except LeaseError as exc:
            raise ControllerError(str(exc)) from None

        result.invocation_id = request.invocation_id
        result.raw_event_log = str(events_path)
        if not result.started_at:
            result.started_at = request.created_at
        if not result.ended_at:
            raise ControllerError("adapter returned no ended_at timestamp")
        result.validate()
        atomic_write(invocation_dir / "result.json",
                     json.dumps(result.to_dict(), indent=2) + "\n")
        return result

    @staticmethod
    def _validate_event(event: Event, request: InvocationRequest) -> None:
        if event.invocation_id != request.invocation_id:
            raise ControllerError("adapter event has the wrong invocation_id")
        event.validate()
