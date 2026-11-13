"""Stage orchestrator: drive a factory job through the real state machine.

The controller runs role invocations through an adapter; ``factory/bin/job``
remains the only writer of ``job.json`` and the only decider of legal moves,
and ``factory/bin/gate`` remains the only decider of checks.

This module adds the missing loop between them: intake -> route -> worktree ->
spec -> build -> gate -> review -> approve -> merge, with bounded retries,
reviewer fan-out and resumption from the recorded stage.
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..adapters import Adapter, build_registry
from . import browser as browser_evidence
from .artifacts import canonical_path
from .config import FactoryConfig, Profile
from .contracts import Artifact, InvocationRequest, InvocationResult, new_invocation_id, sha256_file, utc_now
from .controller import Controller
from .github import GhCliGitHub, GitHub
from .roles import Role, load_roles

ROLE_FILES = {
    "intake": "intake", "spec": "spec-writer", "builder": "builder", "approver": "approver",
    "code": "reviewer-code", "security": "reviewer-security",
    "ux": "reviewer-ux", "ui": "reviewer-ui",
}

DEADLINES = {
    "intake": 600, "spec": 900, "builder": 1800,
    "code": 900, "security": 900, "ux": 900, "ui": 900, "approver": 900,
}

TERMINAL = {"merged", "rejected", "needs-info", "needs-human"}


class OrchestratorError(RuntimeError):
    """A stage could not be completed; the job remains resumable."""


class AwaitingHuman(Exception):
    """The job reached a human approval point and stopped cleanly."""


@dataclass
class JobState:
    """Thin wrapper around the authoritative scripts."""

    factory_root: Path

    @property
    def job_bin(self) -> Path:
        return self.factory_root / "factory" / "bin" / "job"

    @property
    def gate_bin(self) -> Path:
        return self.factory_root / "factory" / "bin" / "gate"

    def run(self, command: list[str], stdin: str | None = None) -> str:
        result = subprocess.run(command, input=stdin, capture_output=True, text=True,
                                cwd=self.factory_root)
        if result.returncode != 0:
            raise OrchestratorError(
                f"`{' '.join(command)}` failed: {(result.stderr or result.stdout).strip()}")
        return result.stdout

    def job(self, *args: str, stdin: str | None = None) -> str:
        return self.run([sys.executable, str(self.job_bin), *args], stdin=stdin)

    def gate(self, job_id: str) -> int:
        result = subprocess.run([sys.executable, str(self.gate_bin), job_id],
                                capture_output=True, text=True, cwd=self.factory_root)
        return result.returncode

    def queue(self) -> list[dict]:
        rows = []
        for line in self.job("queue").splitlines():
            if not line.strip():
                continue
            action, issue, job, stage, note = (line.split("\t") + ["", "", "", "", ""])[:5]
            rows.append({"action": action, "issue": issue, "job": job,
                         "stage": stage, "note": note})
        return rows

    def create(self, issue: str) -> str:
        return self.job("create", issue).strip()

    def advance(self, job_id: str, stage: str) -> None:
        self.job("advance", job_id, stage)

    def agents(self, job_id: str, agents: dict) -> None:
        self.job("agents", job_id, json.dumps(agents))

    def sync(self, job_id: str) -> None:
        self.job("sync", job_id)

    def merge(self, job_id: str) -> None:
        self.job("merge", job_id)

    def summary(self, ids: list[str]) -> str:
        return self.job("summary", *ids)


@dataclass
class Orchestrator:
    factory_root: Path
    config: FactoryConfig
    adapters: dict[str, Adapter]
    state: JobState
    roles: dict[str, Role] = field(init=False)
    controller: Controller = field(init=False)
    log: Callable[[str], None] = print
    github: GitHub = field(default_factory=GhCliGitHub)
    allow_merge: bool = False

    def __post_init__(self):
        self.roles = load_roles(self.factory_root)
        self.controller = Controller.from_config(self.config, self.factory_root,
                                                 adapters=self.adapters)

    # ---- request construction -------------------------------------------

    def _profile(self, role: str, reviewers: dict[str, Profile] | None = None) -> Profile:
        if role in ("intake", "spec", "builder", "approver"):
            return self.config.role_profile(role)
        if reviewers is None:
            raise OrchestratorError(f"no reviewer profile resolved for {role!r}")
        try:
            return reviewers[role]
        except KeyError:
            raise OrchestratorError(f"role {role!r} was not routed") from None

    def _role(self, role: str) -> Role:
        try:
            return self.roles[ROLE_FILES[role]]
        except KeyError:
            raise OrchestratorError(f"unknown role {role!r}") from None

    def _job_json(self, job_id: str) -> dict:
        path = self.factory_root / "factory" / "jobs" / job_id / "job.json"
        if not path.exists():
            raise OrchestratorError(f"no job {job_id}")
        return json.loads(path.read_text())

    def _inputs(self, job_dir: Path, exclude: Path | None = None) -> list[Artifact]:
        artifacts = []
        names = ["issue.md", "intake.md", "spec.md", "build.md", "decision.md"]
        for pattern in ("feedback-*.md", "round-*/checks.md", "round-*/review-*.md"):
            names.extend(sorted(p.relative_to(job_dir).as_posix() for p in job_dir.glob(pattern)))
        for name in names:
            path = job_dir / name
            if path.is_file() and (exclude is None or path.resolve() != exclude.resolve()):
                artifacts.append(Artifact(path=str(path), sha256=sha256_file(path)))
        return artifacts

    def build_request(self, job: dict, role: str, *, reviewers: dict[str, Profile] | None = None,
                      browser: dict | None = None,
                      extra_inputs: list[Artifact] | None = None) -> InvocationRequest:
        profile = self._profile(role, reviewers)
        instruction = self._role(role)
        job_dir = self.factory_root / "factory" / "jobs" / job["id"]
        output = canonical_path(job_dir, instruction, job["round"])
        # Intake runs before the worktree exists.
        worktree = None if role == "intake" else job.get("worktree")
        inputs = self._inputs(job_dir, exclude=output) + list(extra_inputs or [])
        return InvocationRequest(
            invocation_id=new_invocation_id(),
            job_id=job["id"],
            stage=job["stage"],
            round=job["round"],
            attempt=1,
            role=role,
            controller_root=str(self.factory_root),
            job_dir=str(job_dir),
            worktree=worktree,
            branch=job.get("branch"),
            base_branch=self.config.base_branch,
            instruction_path=f"factory/roles/{instruction.name}.md",
            instruction_digest=instruction.digest,
            inputs=inputs,
            outputs=[output.relative_to(job_dir).as_posix()],
            profile=profile.name,
            adapter=profile.adapter,
            provider=profile.provider,
            model=profile.model,
            reasoning=dict(profile.reasoning),
            required_capabilities=list(instruction.capabilities),
            permission_profile=profile.permission_profile,
            deadline_seconds=DEADLINES.get(role),
            browser=browser,
            created_at=utc_now(),
        )

    def _run_role(self, job: dict, role: str, *,
                  reviewers: dict[str, Profile] | None = None,
                  extra_inputs: list[Artifact] | None = None) -> InvocationResult:
        request = self.build_request(job, role, reviewers=reviewers,
                                     extra_inputs=extra_inputs)
        self.log(f"{job['id']}: {job['stage']} -> {role} ({request.profile} "
                 f"{request.adapter}:{request.model})")
        result = self.controller.run(request)
        if result.status != "completed":
            detail = (result.error or {}).get("message") or result.status
            raise OrchestratorError(f"{job['id']}: {role} {result.status}: {detail}")
        return result

    # ---- reviewers ------------------------------------------------------

    def reviewers_for(self, job: dict) -> dict[str, Profile]:
        intake = job.get("intake") or {}
        return self.config.reviewers_for(intake["risk"], intake.get("surfaces") or [])

    def _record_agents(self, job: dict, reviewers: dict[str, Profile]) -> None:
        agents = {
            "reviewers": {role: profile.name for role, profile in reviewers.items()},
            "profiles": {role: {"adapter": p.adapter, "model": p.model,
                                "permission_profile": p.permission_profile}
                         for role, p in reviewers.items()},
        }
        self.state.agents(job["id"], agents)

    def _run_reviewers(self, job: dict, reviewers: dict[str, Profile],
                       extra_inputs: list[Artifact] | None = None) -> None:
        with ThreadPoolExecutor(max_workers=max(1, len(reviewers))) as pool:
            futures = {pool.submit(self._run_role, job, role, reviewers=reviewers,
                                   extra_inputs=extra_inputs): role
                       for role in reviewers}
            errors = []
            for future in as_completed(futures):
                role = futures[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - report every reviewer
                    errors.append(f"{role}: {exc}")
            if errors:
                raise OrchestratorError("; ".join(errors))

    # ---- stage handlers -------------------------------------------------

    def _stage_intake(self, job: dict) -> None:
        self._run_role(job, "intake")
        self.state.sync(job["id"])
        job = self._job_json(job["id"])
        decision = (job.get("intake") or {}).get("decision")
        if decision == "ACCEPT":
            risk = (job.get("intake") or {}).get("risk")
            if risk not in self.config.routing:
                raise OrchestratorError(
                    f"{job['id']}: intake.md is missing a routed RISK header "
                    f"(got {risk!r}); expected one of {', '.join(self.config.routing)}")
            for surface in (job.get("intake") or {}).get("surfaces") or []:
                if surface not in self.config.surface_reviewers:
                    raise OrchestratorError(
                        f"{job['id']}: intake.md names unknown surface {surface!r}")
            reviewers = self.reviewers_for(job)
            self._record_agents(job, reviewers)
            self.state.advance(job["id"], "spec")
        elif decision == "NEEDS-INFO":
            self.state.advance(job["id"], "needs-info")
        elif decision == "REJECT":
            self.state.advance(job["id"], "rejected")
        else:
            raise OrchestratorError(f"{job['id']}: intake produced no decision")

    def _stage_spec(self, job: dict) -> None:
        self._run_role(job, "spec")
        self.state.advance(job["id"], "build")

    def _ensure_pr(self, job: dict) -> None:
        """A READY build must have a draft PR.

        The builder is asked to open it, but a worker must not be able to strand
        a job by forgetting. This is deterministic and idempotent.
        """
        if job.get("pr"):
            return
        branch = job["branch"]
        if self.github.open_pr(branch):
            return
        title = job.get("title") or f"Factory job {job['id']}"
        body = f"Closes #{job['issue']}\n\nOpened by the factory controller for {job['id']}."
        pr = self.github.create_pr(branch, self.config.base_branch, title, body, draft=True)
        self.log(f"{job['id']}: opened draft PR {pr.get('url', '')}".rstrip())

    def _stage_build(self, job: dict) -> None:
        self._run_role(job, "builder")
        build = (self.factory_root / "factory" / "jobs" / job["id"] / "build.md").read_text()
        status = build.split("\n", 1)[0].strip()
        if status == "STATUS: READY":
            self._ensure_pr(job)
            self.state.advance(job["id"], "gate")
        elif status == "STATUS: BLOCKED":
            self.state.advance(job["id"], "needs-human")
        else:
            raise OrchestratorError(f"{job['id']}: build.md starts with {status!r}")

    def _stage_gate(self, job: dict) -> None:
        code = self.state.gate(job["id"])
        if code == 0:
            self.state.advance(job["id"], "review")
            return
        round_limit = job["round_limit"]
        if job["round"] >= round_limit:
            self.state.advance(job["id"], "needs-human")
        else:
            self.state.advance(job["id"], "build")

    def _browser_port(self, job: dict) -> int:
        ports = self.config.raw.get("ports", {})
        base = int(ports.get("base", 5100))
        step = int(ports.get("step", 10))
        number = int(job["id"][:3]) if job["id"][:3].isdigit() else 1
        return base + step * number + 9

    def _browser_evidence(self, job: dict) -> list[Artifact]:
        surfaces = (job.get("intake") or {}).get("surfaces") or []
        if "ui" not in surfaces:
            return []
        settings = browser_evidence.settings_from(self.config.browser)
        if not settings.configured():
            raise OrchestratorError(
                f"{job['id']}: UI review needs browser evidence but "
                "config.browser.start_command and config.browser.url are not set")
        job_dir = self.factory_root / "factory" / "jobs" / job["id"]
        return browser_evidence.capture(
            job_dir=job_dir, worktree=job["worktree"], job=job, settings=settings,
            port=self._browser_port(job), log=self.log)

    def _stage_review(self, job: dict) -> None:
        reviewers = self.reviewers_for(job)
        evidence = self._browser_evidence(job)
        self._run_reviewers(job, reviewers, extra_inputs=evidence)
        self.state.sync(job["id"])
        job = self._job_json(job["id"])
        verdicts = job.get("reviews") or {}
        changes = [role for role, verdict in verdicts.items() if verdict == "CHANGES"]
        invalid = [role for role, verdict in verdicts.items()
                   if verdict not in ("PASS", "CHANGES")]
        if invalid:
            raise OrchestratorError(f"{job['id']}: invalid verdicts: {', '.join(invalid)}")
        if not changes:
            self.state.advance(job["id"], "approve")
            return
        if job["round"] >= job["round_limit"]:
            self.state.advance(job["id"], "needs-human")
        else:
            self.state.advance(job["id"], "build")

    def _stage_approve(self, job: dict) -> None:
        self._run_role(job, "approver")
        decision = self.factory_root / "factory" / "jobs" / job["id"] / "decision.md"
        first = decision.read_text().split("\n", 1)[0].strip()
        if first == "APPROVE":
            if self.allow_merge:
                self.state.merge(job["id"])
                return
            if job.get("pr"):
                self.github.ready_pr(job["pr"])
            raise AwaitingHuman(
                f"{job['id']}: PR #{job.get('pr')} is ready; merge it to approve "
                f"(or rerun with --merge to let the approver merge)")
        elif first == "ESCALATE":
            self.state.advance(job["id"], "needs-human")
        else:
            raise OrchestratorError(f"{job['id']}: decision.md starts with {first!r}")

    def _stage_merging(self, job: dict) -> None:
        self.state.merge(job["id"])

    # ---- driver ---------------------------------------------------------

    def run_job(self, job_id: str, *, max_steps: int = 40) -> dict:
        job = self._job_json(job_id)
        for _ in range(max_steps):
            stage = job["stage"]
            if stage in TERMINAL:
                self.log(f"{job_id}: {stage}")
                return job
            handler = getattr(self, f"_stage_{stage}", None)
            if handler is None:
                raise OrchestratorError(f"{job_id}: no handler for stage {stage!r}")
            try:
                handler(job)
            except AwaitingHuman as stop:
                self.log(str(stop))
                return job
            updated = self._job_json(job_id)
            if updated["stage"] == stage and stage not in TERMINAL:
                raise OrchestratorError(f"{job_id}: stage {stage} did not advance")
            job = updated
        raise OrchestratorError(f"{job_id}: exceeded {max_steps} stage steps")

    def resolve_job(self, reference: str) -> str:
        jobs_dir = self.factory_root / "factory" / "jobs"
        if reference.isdigit():
            issue = int(reference)
            if jobs_dir.exists():
                for path in sorted(jobs_dir.iterdir()):
                    meta = path / "job.json"
                    if meta.exists():
                        job = json.loads(meta.read_text())
                        if job.get("issue") == issue:
                            return job["id"]
            return self.state.create(reference)
        return reference

    def run_queue(self) -> list[dict]:
        """One drain pass over the queue, respecting the job limit."""
        rows = self.state.queue()
        runnable = [row for row in rows if row["action"] in ("new", "resume")]
        if not runnable:
            return rows
        ids = []
        for row in runnable:
            ids.append(self.state.create(row["issue"]) if row["action"] == "new"
                       else row["job"])
        results = []
        with ThreadPoolExecutor(max_workers=int(self.config.limits["jobs"])) as pool:
            futures = {pool.submit(self.run_job, job_id): job_id for job_id in ids}
            for future in as_completed(futures):
                job_id = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    self.log(f"{job_id}: {exc}")
        return rows


def build_orchestrator(factory_root: str | Path, config: FactoryConfig,
                       adapters: dict[str, Adapter] | None = None,
                       state: JobState | None = None,
                       log: Callable[[str], None] = print,
                       allow_merge: bool = False) -> Orchestrator:
    root = Path(factory_root)
    return Orchestrator(
        factory_root=root,
        config=config,
        adapters=adapters if adapters is not None else build_registry(root),
        state=state or JobState(root),
        log=log,
        allow_merge=allow_merge,
    )
