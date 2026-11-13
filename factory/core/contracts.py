"""Versioned invocation, result and event contracts.

These dataclasses are the boundary between the controller and a harness
adapter. They are deliberately dependency-free (standard library only) so a
controller run never needs the harness's own packages.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUEST_SCHEMA = "factory.invocation.request/1"
RESULT_SCHEMA = "factory.invocation.result/1"
EVENT_SCHEMA = "factory.invocation.event/1"

STATUSES = ("completed", "failed", "blocked", "cancelled", "timed_out")
EVENT_TYPES = ("started", "progress", "usage", "artifact", "finished")

_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "cost_kind",
    "currency",
    "pricing_source",
    "pricing_date",
    "calculation_version",
)


class ContractError(ValueError):
    """A request, result or event does not satisfy the contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_invocation_id() -> str:
    return uuid.uuid4().hex


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    _require(isinstance(value, str), f"{name} must be a string")
    _require(allow_empty or value.strip() != "", f"{name} is required")
    return value


def _absolute(value: Any, name: str) -> str:
    _string(value, name)
    _require(Path(value).is_absolute(), f"{name} must be an absolute path")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool), f"{name} must be an integer")
    _require(value >= 0, f"{name} must not be negative")
    return value


@dataclass
class Artifact:
    path: str
    sha256: str | None = None
    role: str | None = None

    def validate(self) -> None:
        _absolute(self.path, "artifact.path")
        if self.sha256 is not None:
            _string(self.sha256, "artifact.sha256")
            _require(re.fullmatch(r"[0-9a-f]{64}", self.sha256) is not None,
                     "artifact.sha256 must be 64 lowercase hex characters")

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "Artifact":
        return cls(path=data["path"], sha256=data.get("sha256"), role=data.get("role"))


@dataclass
class InvocationRequest:
    """Everything an adapter needs to start one role invocation."""

    schema: str = REQUEST_SCHEMA
    invocation_id: str = ""
    job_id: str = ""
    stage: str = ""
    round: int = 0
    attempt: int = 1
    role: str = ""
    controller_root: str = ""
    job_dir: str = ""
    worktree: str | None = None
    branch: str | None = None
    base_branch: str | None = None
    input_commit: str | None = None
    instruction_path: str = ""
    instruction_digest: str = ""
    inputs: list[Artifact] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    profile: str = ""
    adapter: str = ""
    provider: str | None = None
    model: str = ""
    reasoning: dict = field(default_factory=dict)
    required_capabilities: list[str] = field(default_factory=list)
    permission_profile: str = ""
    permissions: dict = field(default_factory=dict)
    deadline_seconds: int | None = None
    cancel_grace_seconds: int = 5
    browser: dict | None = None
    session_id: str | None = None
    auth_ref: str | None = None
    created_at: str = ""

    def validate(self) -> None:
        _require(self.schema == REQUEST_SCHEMA, f"schema must be {REQUEST_SCHEMA}")
        _require(re.fullmatch(r"[0-9a-f]{32}", self.invocation_id) is not None
                 if self.invocation_id else False, "invocation_id must be a uuid4 hex string")
        for name in ("job_id", "stage", "role", "profile", "adapter", "model", "permission_profile"):
            _string(getattr(self, name), name)
        _absolute(self.controller_root, "controller_root")
        _absolute(self.job_dir, "job_dir")
        if self.worktree is not None:
            _absolute(self.worktree, "worktree")
        if self.base_branch is not None:
            _string(self.base_branch, "base_branch")
        _string(self.instruction_path, "instruction_path")
        _require(not Path(self.instruction_path).is_absolute(),
                 "instruction_path must be relative to the controller root")
        _string(self.instruction_digest, "instruction_digest")
        _non_negative_int(self.round, "round")
        _non_negative_int(self.attempt, "attempt")
        _require(self.attempt >= 1, "attempt starts at 1")
        for artifact in self.inputs:
            artifact.validate()
        for output in self.outputs:
            _string(output, "outputs[]")
            _require(not Path(output).is_absolute(), "outputs must be relative to the job directory")
        _require(isinstance(self.reasoning, dict), "reasoning must be an object")
        _require(isinstance(self.required_capabilities, list), "required_capabilities must be a list")
        _require(isinstance(self.permissions, dict), "permissions must be an object")
        if self.permissions:
            for key in ("shell", "network"):
                _require(isinstance(self.permissions.get(key), bool),
                         f"permissions.{key} must be a boolean")
            _require(self.permissions.get("write_scope") in
                     ("output_only", "worktree_and_output", "worktree_only"),
                     "permissions.write_scope must be a known scope")
        if self.deadline_seconds is not None:
            _non_negative_int(self.deadline_seconds, "deadline_seconds")
        _non_negative_int(self.cancel_grace_seconds, "cancel_grace_seconds")
        if self.browser is not None:
            _require(isinstance(self.browser, dict), "browser must be an object")
        _string(self.created_at, "created_at")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["inputs"] = [a.to_dict() for a in self.inputs]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "InvocationRequest":
        data = dict(data)
        data["inputs"] = [Artifact.from_dict(a) for a in data.get("inputs", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class InvocationResult:
    """The normalized terminal outcome of one adapter invocation."""

    schema: str = RESULT_SCHEMA
    invocation_id: str = ""
    status: str = ""
    exit_code: int | None = None
    session_id: str | None = None
    adapter: str = ""
    harness_version: str | None = None
    provider: str | None = None
    model_requested: str | None = None
    model_observed: str | None = None
    started_at: str = ""
    ended_at: str = ""
    artifacts: list[Artifact] = field(default_factory=list)
    commit: str | None = None
    usage: dict | None = None
    error: dict | None = None
    raw_event_log: str | None = None

    def validate(self) -> None:
        _require(self.schema == RESULT_SCHEMA, f"schema must be {RESULT_SCHEMA}")
        _string(self.invocation_id, "invocation_id")
        _string(self.adapter, "adapter")
        _require(self.status in STATUSES, f"status must be one of {', '.join(STATUSES)}")
        if self.exit_code is not None:
            _require(isinstance(self.exit_code, int) and not isinstance(self.exit_code, bool),
                     "exit_code must be an integer")
        if self.status == "completed":
            _require(self.exit_code == 0, "a completed result must have exit_code 0")
        _string(self.started_at, "started_at")
        _string(self.ended_at, "ended_at")
        for artifact in self.artifacts:
            artifact.validate()
        if self.usage is not None:
            _require(isinstance(self.usage, dict), "usage must be an object")
            for key, value in self.usage.items():
                _require(key in _USAGE_KEYS, f"unknown usage field {key!r}")
                if value is not None and key != "cost_kind" and not isinstance(value, (int, float)):
                    raise ContractError(f"usage.{key} must be numeric or null")
        if self.error is not None:
            _require(isinstance(self.error, dict), "error must be an object")
            _require(str(self.error.get("message", "")).strip() != "", "error.message is required")
        if self.raw_event_log is not None:
            _absolute(self.raw_event_log, "raw_event_log")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["artifacts"] = [a.to_dict() for a in self.artifacts]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "InvocationResult":
        data = dict(data)
        data["artifacts"] = [Artifact.from_dict(a) for a in data.get("artifacts", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Event:
    """One normalized adapter event. Raw events are kept beside these."""

    schema: str = EVENT_SCHEMA
    invocation_id: str = ""
    type: str = ""
    timestamp: str = ""
    data: dict = field(default_factory=dict)

    def validate(self) -> None:
        _require(self.schema == EVENT_SCHEMA, f"schema must be {EVENT_SCHEMA}")
        _string(self.invocation_id, "invocation_id")
        _require(self.type in EVENT_TYPES, f"event type must be one of {', '.join(EVENT_TYPES)}")
        _string(self.timestamp, "timestamp")
        _require(isinstance(self.data, dict), "event data must be an object")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        return cls(schema=data.get("schema", EVENT_SCHEMA), invocation_id=data.get("invocation_id", ""),
                   type=data.get("type", ""), timestamp=data.get("timestamp", ""),
                   data=data.get("data", {}))


def normalize_usage(raw: dict | None) -> dict | None:
    """Turn adapter usage into the contract shape. Unknown stays null, not zero."""
    if raw is None:
        return None
    usage = {key: None for key in _USAGE_KEYS}
    for key in _USAGE_KEYS:
        if key in raw:
            usage[key] = raw[key]
    if usage["cost_kind"] is None and usage["cost_usd"] is not None:
        usage["cost_kind"] = "unknown"
    return usage
