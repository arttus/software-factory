"""Portable role definitions.

Canonical instructions live in ``factory/roles/*.md``. The frontmatter carries
only portable metadata: an output path, a first-line pattern, generic
capabilities and whether the role needs a browser. No harness tool names, model
aliases or hooks belong here; adapters supply those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .contracts import sha256_file

ROLE_NAMES = (
    "intake",
    "spec-writer",
    "builder",
    "reviewer-code",
    "reviewer-security",
    "reviewer-ux",
    "reviewer-ui",
    "approver",
)

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


class RoleError(ValueError):
    """A role file is missing or malformed."""


@dataclass(frozen=True)
class Role:
    name: str
    output: str
    first_line: str
    capabilities: tuple[str, ...]
    browser: bool
    path: Path
    digest: str
    instructions: str

    def output_for(self, round: int) -> str:
        return self.output.format(round=round)

    def matches_first_line(self, text: str) -> bool:
        first = text.split("\n", 1)[0].strip()
        return re.fullmatch(self.first_line, first) is not None


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER.match(text)
    if not match:
        raise RoleError(f"{path}: missing frontmatter")
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise RoleError(f"{path}: bad frontmatter line {line!r}")
        fields[key.strip()] = value.strip()
    return fields, text[match.end():]


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _list(value: str) -> tuple[str, ...]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        raise RoleError(f"expected a list, got {value!r}")
    inner = value[1:-1].strip()
    if not inner:
        return ()
    return tuple(_unquote(item.strip()) for item in inner.split(",") if item.strip())


def load_role(path: str | Path) -> Role:
    path = Path(path)
    fields, instructions = _split_frontmatter(path.read_text(), path)
    for key in ("name", "output", "first_line", "capabilities"):
        if key not in fields:
            raise RoleError(f"{path}: frontmatter needs {key}")
    name = _unquote(fields["name"])
    if name != path.stem:
        raise RoleError(f"{path}: name {name!r} must match the file name")
    try:
        re.compile(_unquote(fields["first_line"]))
    except re.error as exc:
        raise RoleError(f"{path}: invalid first_line pattern: {exc}") from exc
    return Role(
        name=name,
        output=_unquote(fields["output"]),
        first_line=_unquote(fields["first_line"]),
        capabilities=_list(fields["capabilities"]),
        browser=fields.get("browser", "false").strip().lower() == "true",
        path=path,
        digest=sha256_file(path),
        instructions=instructions.strip() + "\n",
    )


def load_roles(root: str | Path) -> dict[str, Role]:
    """Load every role under ``<root>/factory/roles`` and require the full set."""
    roles_dir = Path(root) / "factory" / "roles"
    roles = {}
    for path in sorted(roles_dir.glob("*.md")):
        role = load_role(path)
        roles[role.name] = role
    missing = [name for name in ROLE_NAMES if name not in roles]
    if missing:
        raise RoleError(f"missing role files: {', '.join(missing)}")
    return roles
