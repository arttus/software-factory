"""Advisory file leases.

One controller lease per repository and one invocation lease per job/stage/role
keep two processes from draining the same slot. ``flock`` gives us an atomic,
crash-safe lock: the kernel releases it when the process exits.
"""

from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from pathlib import Path


class LeaseError(RuntimeError):
    """A lease is already held by another process."""


@dataclass
class Lease:
    path: Path
    _handle: object

    def release(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def acquire(path: str | Path, *, blocking: bool = False) -> Lease:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        handle.close()
        raise LeaseError(f"lease already held: {path}") from None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return Lease(path=path, _handle=handle)


def controller_lease(factory_root: str | Path) -> Lease:
    return acquire(Path(factory_root) / "jobs" / ".controller.lock")


def invocation_lease(job_dir: str | Path, stage: str, role: str) -> Lease:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in f"{stage}-{role}")
    return acquire(Path(job_dir) / "runs" / f".{safe}.lock")
