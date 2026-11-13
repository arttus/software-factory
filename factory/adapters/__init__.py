"""Adapter registry.

Process-backed adapters (Kilo, Codex, Claude) land in later migration steps.
The fake adapter is always available so the controller and its tests have a
deterministic backend.
"""

from __future__ import annotations

from pathlib import Path

from .base import Adapter, AdapterError, Handle
from .fake import FakeAdapter

__all__ = ["Adapter", "AdapterError", "Handle", "FakeAdapter", "build_registry"]


def build_registry(factory_root: str | Path) -> dict[str, Adapter]:
    return {"fake": FakeAdapter(factory_root)}
