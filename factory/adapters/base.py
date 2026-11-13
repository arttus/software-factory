"""The adapter interface every harness backend implements.

Adapters own executable discovery, argument generation, prompt delivery,
permission translation, event normalization and process lifecycle. They never
decide a job's stage, merge a PR or invent a successful result.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator

from ..core.capabilities import CapabilityReport
from ..core.contracts import Event, InvocationRequest, InvocationResult, utc_now


class AdapterError(RuntimeError):
    """An adapter could not launch, stream or terminate an invocation."""


@dataclass
class Handle:
    """A live invocation. ``result`` is filled in before the stream ends."""

    request: InvocationRequest
    result: InvocationResult
    native: object = None
    metadata: dict = field(default_factory=dict)


class Adapter(ABC):
    name: str = ""

    @abstractmethod
    def probe(self) -> CapabilityReport:
        """Report the harness version and enforceable capabilities."""

    @abstractmethod
    def start(self, request: InvocationRequest) -> Handle:
        """Start one invocation and return a live handle."""

    @abstractmethod
    def events(self, handle: Handle) -> Iterator[Event]:
        """Yield normalized events until the invocation finishes."""

    def cancel(self, handle: Handle, grace_seconds: int = 5) -> None:
        """Stop the owned work. A late result must not advance a job."""
        from ..core.contracts import InvocationResult
        handle.result.status = "cancelled"
        handle.result.exit_code = None
        handle.result.ended_at = utc_now()
        handle.result.error = {"message": "cancelled by the controller"}

    def resume(self, request: InvocationRequest, session_id: str) -> Handle:
        """Continue a native session when the harness supports it."""
        request.session_id = session_id
        return self.start(request)
