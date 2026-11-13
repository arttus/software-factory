"""Capability and permission-profile checks.

An adapter reports what its harness can enforce. A role requires capabilities;
a profile names a permission profile. We refuse to start an invocation when a
required capability is missing or, worse, merely prompt-enforced while the role
treats it as a boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import BASE_ROLES, ConfigError, FactoryConfig, PermissionProfile, Profile

CAPABILITY_NAMES = (
    "noninteractive",
    "machine_readable_events",
    "model_selection",
    "session_resume",
    "usage_reporting",
    "cancellation",
    "fs_restrictions",
    "shell_control",
    "network_control",
    "browser_tooling",
)

ENFORCEMENTS = ("native", "external", "prompt_only", "none")

# Capabilities the factory treats as a real boundary. If a role needs one of
# these, prompt-only instructions are not good enough.
ENFORCEABLE = ("fs_restrictions", "shell_control", "network_control")

# Baseline every invocation needs. Browser and shell are added per role/routing.
ROLE_CAPABILITIES = {
    "intake": {"noninteractive", "machine_readable_events", "model_selection", "cancellation"},
    "spec": {"noninteractive", "machine_readable_events", "model_selection", "cancellation"},
    "builder": {"noninteractive", "machine_readable_events", "model_selection", "cancellation",
                "shell_control", "network_control"},
    "approver": {"noninteractive", "machine_readable_events", "model_selection", "cancellation"},
    "code": {"noninteractive", "machine_readable_events", "cancellation", "fs_restrictions"},
    "security": {"noninteractive", "machine_readable_events", "cancellation", "fs_restrictions"},
    "ux": {"noninteractive", "machine_readable_events", "cancellation", "fs_restrictions",
           "browser_tooling"},
    "ui": {"noninteractive", "machine_readable_events", "cancellation", "fs_restrictions",
           "browser_tooling"},
}


class CapabilityError(ValueError):
    """A profile cannot satisfy the capabilities its role requires."""


@dataclass(frozen=True)
class CapabilityReport:
    adapter: str
    version: str | None
    capabilities: dict[str, str]

    def enforcement(self, name: str) -> str:
        return self.capabilities.get(name, "none")


def required_for(role: str, *, browser: bool = False) -> set[str]:
    try:
        required = set(ROLE_CAPABILITIES[role])
    except KeyError:
        raise CapabilityError(f"unknown role {role!r}") from None
    if role in BASE_ROLES:
        if role == "builder" and browser:
            required.add("browser_tooling")
    return required


def check_capabilities(role: str, report: CapabilityReport,
                       *, browser: bool = False) -> list[str]:
    problems = []
    for name in sorted(required_for(role, browser=browser)):
        if name not in CAPABILITY_NAMES:
            problems.append(f"{role}: unknown capability {name!r}")
            continue
        enforcement = report.enforcement(name)
        if enforcement not in ENFORCEMENTS:
            problems.append(f"{role}: adapter {report.adapter} reports invalid enforcement "
                            f"{enforcement!r} for {name}")
        elif enforcement == "none":
            problems.append(f"{role}: adapter {report.adapter} does not support {name}")
        elif name in ENFORCEABLE and enforcement == "prompt_only":
            problems.append(f"{role}: adapter {report.adapter} only prompt-enforces {name}; "
                            f"that is not a boundary")
    return problems


def check_permission_profile(role: str, permission: PermissionProfile) -> list[str]:
    problems = []
    if role == "intake":
        if permission.shell:
            problems.append("intake must not have shell access")
        if permission.network:
            problems.append("intake must not have network access")
        if permission.write_scope != "output_only":
            problems.append("intake may write only its output artifact")
    elif role in ("code", "security", "ux", "ui"):
        if permission.write_scope != "output_only":
            problems.append(f"reviewer {role} may write only its review artifact")
        if permission.shell:
            problems.append(f"reviewer {role} must not have shell access")
    elif role == "builder":
        if permission.write_scope != "worktree_and_output":
            problems.append("builder must be able to write the worktree and its output")
        if not permission.shell:
            problems.append("builder needs shell access")
    return problems


def check_profile(config: FactoryConfig, profile: Profile, report: CapabilityReport,
                  *, browser: bool = False) -> list[str]:
    """Check one profile against its role's needs. Unknown roles are skipped."""
    role = role_for_profile(config, profile)
    if role is None:
        return []
    permission = config.permission(profile.permission_profile)
    problems = check_permission_profile(role, permission)
    problems.extend(check_capabilities(role, report, browser=browser))
    return problems


def role_for_profile(config: FactoryConfig, profile: Profile) -> str | None:
    for role, name in config.roles.items():
        if name == profile.name:
            return role
    for table in (config.routing, config.surface_reviewers):
        for role, name in table.items():
            if name == profile.name:
                return role
    return None
