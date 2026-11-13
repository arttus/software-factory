"""Canonical artifact paths, validation and atomic promotion.

Workers write into a per-attempt directory. Only a validated artifact may be
promoted to the canonical path the state machine reads, and promotion is an
atomic rename so a half-written file never becomes evidence.
"""

from __future__ import annotations

import os
from pathlib import Path

from .contracts import sha256_file
from .roles import Role


class ArtifactError(ValueError):
    """An artifact is missing, stale or does not match its role contract."""


def canonical_path(job_dir: str | Path, role: Role, round: int = 0) -> Path:
    job_dir = Path(job_dir)
    relative = role.output_for(round) if "{round}" in role.output else role.output
    path = job_dir / relative
    if not path.resolve().is_relative_to(job_dir.resolve()):
        raise ArtifactError(f"{role.name}: output escapes the job directory")
    return path


def validate_output(role: Role, text: str) -> None:
    if not role.matches_first_line(text):
        first = text.split("\n", 1)[0].strip() if text.strip() else "(empty)"
        raise ArtifactError(f"{role.name}: first line {first!r} does not match "
                            f"{role.first_line!r}")


def validate_file(role: Role, path: str | Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise ArtifactError(f"{role.name}: missing output {path}")
    text = path.read_text()
    validate_output(role, text)
    return text


def atomic_write(path: str | Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)
    return path


def promote(staged: str | Path, canonical: str | Path) -> Path:
    """Move a validated staged artifact to its canonical path atomically."""
    staged, canonical = Path(staged), Path(canonical)
    if not staged.is_file():
        raise ArtifactError(f"missing staged artifact {staged}")
    canonical.parent.mkdir(parents=True, exist_ok=True)
    tmp = canonical.with_name(f".{canonical.name}.promote-{os.getpid()}")
    os.replace(staged, tmp)
    os.replace(tmp, canonical)
    _fsync_dir(canonical.parent)
    return canonical


def digest(path: str | Path) -> str:
    return sha256_file(path)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
