"""Adapter registry.

The fake adapter is always available. Process-backed adapters register when
their executable is found: Kilo and Command Code today, Codex and the Claude
compatibility adapter in later migration steps.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import Adapter, AdapterError, Handle
from .command_code import CommandCodeAdapter
from .fake import FakeAdapter
from .kilo import KiloAdapter

__all__ = ["Adapter", "AdapterError", "Handle", "FakeAdapter", "KiloAdapter",
           "CommandCodeAdapter", "build_registry"]


def build_registry(factory_root: str | Path) -> dict[str, Adapter]:
    adapters: dict[str, Adapter] = {"fake": FakeAdapter(factory_root)}
    if shutil.which("kilo"):
        adapters["kilo"] = KiloAdapter(factory_root)
    if shutil.which("cmd"):
        adapters["command-code"] = CommandCodeAdapter(factory_root)
    return adapters
