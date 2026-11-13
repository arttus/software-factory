"""A narrow GitHub boundary so the controller can be tested without network.

The existing ``factory/bin/job`` script still calls ``gh`` directly. New
controller code goes through this interface, and tests use ``FakeGitHub``.
"""

from __future__ import annotations

import json
import subprocess
from typing import Protocol


class GitHubError(RuntimeError):
    """A GitHub operation failed or returned malformed data."""


class GitHub(Protocol):
    def issue(self, number: int) -> dict: ...
    def comment(self, kind: str, number: int, body: str) -> None: ...
    def label(self, kind: str, number: int, *, add: str | None = None,
              remove: str | None = None) -> None: ...
    def pr_state(self, number: int) -> str: ...
    def merge_pr(self, number: int, method: str = "merge") -> None: ...


class GhCliGitHub:
    """The real boundary, backed by an authenticated ``gh`` executable."""

    def __init__(self, executable: str = "gh"):
        self.executable = executable

    def _run(self, *args: str) -> str:
        result = subprocess.run([self.executable, *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise GitHubError(f"gh {' '.join(args)} failed: "
                              f"{(result.stderr or result.stdout).strip()}")
        return result.stdout

    def issue(self, number: int) -> dict:
        raw = self._run("issue", "view", str(number), "--json",
                        "number,title,body,author,labels,state,url,comments")
        return json.loads(raw)

    def comment(self, kind: str, number: int, body: str) -> None:
        self._run(kind, "comment", str(number), "--body", body)

    def label(self, kind: str, number: int, *, add: str | None = None,
              remove: str | None = None) -> None:
        args = [kind, "edit", str(number)]
        if add:
            args += ["--add-label", add]
        if remove:
            args += ["--remove-label", remove]
        self._run(*args)

    def pr_state(self, number: int) -> str:
        return json.loads(self._run("pr", "view", str(number), "--json", "state"))["state"]

    def merge_pr(self, number: int, method: str = "merge") -> None:
        self._run("pr", "merge", str(number), f"--{method}")


class FakeGitHub:
    """Deterministic in-memory GitHub for controller and transition tests."""

    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str | None, str | None]] = []
        self.pr_states: dict[int, str] = {}
        self.merged: list[tuple[int, str]] = []
        self.operator = "factory-bot"

    def add_issue(self, number: int, **fields) -> dict:
        issue = {"number": number, "title": fields.get("title", f"Issue {number}"),
                 "body": fields.get("body", ""), "author": {"login": fields.get("author", "alice")},
                 "labels": fields.get("labels", []), "state": fields.get("state", "OPEN"),
                 "url": fields.get("url", f"https://example.test/issues/{number}"),
                 "comments": fields.get("comments", [])}
        self.issues[number] = issue
        return issue

    def issue(self, number: int) -> dict:
        if number not in self.issues:
            raise GitHubError(f"no issue {number}")
        return self.issues[number]

    def comment(self, kind: str, number: int, body: str) -> None:
        self.comments.append((kind, number, body))

    def label(self, kind: str, number: int, *, add: str | None = None,
              remove: str | None = None) -> None:
        self.labels.append((kind, number, add, remove))

    def pr_state(self, number: int) -> str:
        return self.pr_states.get(number, "OPEN")

    def merge_pr(self, number: int, method: str = "merge") -> None:
        self.pr_states[number] = "MERGED"
        self.merged.append((number, method))
