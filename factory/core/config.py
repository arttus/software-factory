"""Versioned factory configuration: execution profiles and resolved routing.

A profile names a harness adapter, a model selector and a permission profile.
Risk and surface routing select profiles; role defaults cover the stages that
run once per job. The legacy Claude path resolves the same profiles back to
model aliases, so there is one source of truth for every backend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
BASE_ROLES = ("intake", "spec", "builder", "approver")
REVIEW_ROLES = ("code", "security", "ux", "ui")
RISKS = ("low", "medium", "high")
SURFACES = ("ui", "security")
WRITE_SCOPES = ("output_only", "worktree_and_output", "worktree_only")


class ConfigError(ValueError):
    """The factory configuration does not satisfy schema version 2."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


@dataclass(frozen=True)
class AdapterConfig:
    name: str
    executable: str | None
    enabled: bool
    version_command: tuple[str, ...] = ()
    verified_versions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PermissionProfile:
    name: str
    shell: bool
    network: bool
    write_scope: str


@dataclass(frozen=True)
class Profile:
    name: str
    adapter: str
    model: str
    permission_profile: str
    provider: str | None = None
    reasoning: tuple[tuple[str, Any], ...] = ()
    options: tuple[tuple[str, Any], ...] = ()


@dataclass
class FactoryConfig:
    path: Path
    raw: dict
    schema_version: int
    base_branch: str
    labels: dict
    adapters: dict[str, AdapterConfig]
    permission_profiles: dict[str, PermissionProfile]
    profiles: dict[str, Profile]
    roles: dict[str, str]
    routing: dict[str, dict[str, str]]
    surface_reviewers: dict[str, dict[str, str]]
    limits: dict
    merge_policy: str
    role_set: str
    protected_paths: list[str] = field(default_factory=list)

    def profile(self, name: str) -> Profile:
        try:
            return self.profiles[name]
        except KeyError:
            raise ConfigError(f"unknown profile {name!r}") from None

    def adapter(self, name: str) -> AdapterConfig:
        try:
            return self.adapters[name]
        except KeyError:
            raise ConfigError(f"unknown adapter {name!r}") from None

    def permission(self, name: str) -> PermissionProfile:
        try:
            return self.permission_profiles[name]
        except KeyError:
            raise ConfigError(f"unknown permission_profile {name!r}") from None

    def role_profile(self, role: str) -> Profile:
        _require(role in BASE_ROLES, f"unknown base role {role!r}")
        _require(role in self.roles, f"config.roles has no entry for {role!r}")
        return self.profile(self.roles[role])

    def reviewers_for(self, risk: str, surfaces: list[str] | None = None) -> dict[str, Profile]:
        _require(risk in self.routing, f"risk {risk!r} is not routed")
        names = dict(self.routing[risk])
        for surface in surfaces or []:
            _require(surface in self.surface_reviewers,
                     f"surface {surface!r} is not routed")
            names.update(self.surface_reviewers[surface])
        return {role: self.profile(name) for role, name in names.items()}

    def profile_names_in_use(self) -> set[str]:
        names = set(self.roles.values())
        for table in (self.routing, self.surface_reviewers):
            for roles in table.values():
                names.update(roles.values())
        return names

    def enabled_adapters(self) -> dict[str, AdapterConfig]:
        return {name: adapter for name, adapter in self.adapters.items() if adapter.enabled}


def load_config(path: str | Path, role_set: str | None = None) -> FactoryConfig:
    path = Path(path)
    raw = json.loads(path.read_text())
    return build_config(raw, path, role_set=role_set)


def build_config(raw: dict, path: str | Path = Path("config.json"),
                 role_set: str | None = None) -> FactoryConfig:
    _require(isinstance(raw, dict), "config must be a JSON object")
    _require(raw.get("schema_version") == SCHEMA_VERSION,
             f"config.schema_version must be {SCHEMA_VERSION}")

    base_branch = raw.get("base_branch")
    _require(isinstance(base_branch, str) and base_branch.strip() != "", "base_branch is required")

    labels = raw.get("labels")
    _require(isinstance(labels, dict), "labels must be an object")
    for key in ("queue", "needs_info", "needs_human"):
        _require(isinstance(labels.get(key), str) and labels[key].strip() != "",
                 f"labels.{key} is required")

    adapters = _build_adapters(raw.get("adapters"))
    permission_profiles = _build_permissions(raw.get("permission_profiles"))
    profiles = _build_profiles(raw.get("profiles"), adapters, permission_profiles)

    roles, routing, surface_reviewers, selected = _select_role_set(
        raw, profiles, adapters, role_set)

    limits = raw.get("limits")
    _require(isinstance(limits, dict), "limits must be an object")
    for key in ("jobs", "workers"):
        value = limits.get(key)
        _require(isinstance(value, int) and not isinstance(value, bool) and value > 0,
                 f"limits.{key} must be a positive integer")
    per_adapter = limits.get("per_adapter", {})
    _require(isinstance(per_adapter, dict), "limits.per_adapter must be an object")
    for name, value in per_adapter.items():
        _require(name in adapters, f"limits.per_adapter has unknown adapter {name!r}")
        _require(isinstance(value, int) and not isinstance(value, bool) and value > 0,
                 f"limits.per_adapter.{name} must be a positive integer")

    merge_policy = raw.get("merge_policy", "human")
    _require(merge_policy in ("human", "auto"), "merge_policy must be 'human' or 'auto'")

    protected = raw.get("protected_paths", [])
    _require(isinstance(protected, list) and all(isinstance(p, str) for p in protected),
             "protected_paths must be a list of strings")

    return FactoryConfig(
        path=Path(path), raw=raw, schema_version=SCHEMA_VERSION,
        base_branch=base_branch, labels=labels, adapters=adapters,
        permission_profiles=permission_profiles, profiles=profiles, roles=roles,
        routing=routing, surface_reviewers=surface_reviewers, limits=limits,
        merge_policy=merge_policy, role_set=selected, protected_paths=protected,
    )


def _select_role_set(raw: dict, profiles: dict[str, Profile],
                     adapters: dict[str, AdapterConfig],
                     role_set: str | None) -> tuple[dict, dict, dict, str]:
    """Choose the role/routing tables.

    ``role_sets`` holds named alternatives (for example the Command Code
    backend) while the top-level tables remain the working Claude path. A
    selected set replaces roles, routing and surface reviewers together.
    """
    role_sets = raw.get("role_sets", {})
    _require(isinstance(role_sets, dict), "role_sets must be an object")
    selected = role_set or raw.get("default_role_set") or "claude"
    _require(isinstance(selected, str) and selected, "default_role_set must be a string")

    if selected in role_sets:
        source = role_sets[selected]
        _require(isinstance(source, dict), f"role_sets.{selected} must be an object")
    else:
        _require(selected == "claude", f"unknown role set {selected!r}")
        source = {"roles": raw.get("roles"), "routing": raw.get("routing"),
                  "surface_reviewers": raw.get("surface_reviewers")}

    roles = source.get("roles")
    _require(isinstance(roles, dict), f"role set {selected!r} needs roles")
    for role in BASE_ROLES:
        name = roles.get(role)
        _require(isinstance(name, str) and name in profiles,
                 f"role set {selected!r}: roles.{role} must reference a profile")
        _require(adapters[profiles[name].adapter].enabled,
                 f"role set {selected!r}: roles.{role} uses profile {name!r} on disabled "
                 f"adapter {profiles[name].adapter!r}")

    routing = _build_routing(source.get("routing"), profiles, adapters,
                             f"role set {selected!r} routing", RISKS)
    surface = _build_routing(source.get("surface_reviewers"), profiles, adapters,
                             f"role set {selected!r} surface_reviewers", SURFACES)
    return roles, routing, surface, selected


def _build_adapters(raw: Any) -> dict[str, AdapterConfig]:
    _require(isinstance(raw, dict) and raw, "adapters must be a non-empty object")
    adapters = {}
    for name, spec in raw.items():
        _require(isinstance(spec, dict), f"adapters.{name} must be an object")
        _require(isinstance(spec.get("enabled"), bool), f"adapters.{name}.enabled must be a boolean")
        executable = spec.get("executable")
        _require(executable is None or (isinstance(executable, str) and executable.strip() != ""),
                 f"adapters.{name}.executable must be a string or null")
        version_command = spec.get("version_command", [])
        _require(isinstance(version_command, list)
                 and all(isinstance(a, str) for a in version_command),
                 f"adapters.{name}.version_command must be a list of strings")
        verified = spec.get("verified_versions", [])
        _require(isinstance(verified, list) and all(isinstance(v, str) for v in verified),
                 f"adapters.{name}.verified_versions must be a list of strings")
        adapters[name] = AdapterConfig(
            name=name, executable=executable, enabled=spec["enabled"],
            version_command=tuple(version_command), verified_versions=tuple(verified),
        )
    _require(any(a.enabled for a in adapters.values()), "at least one adapter must be enabled")
    return adapters


def _build_permissions(raw: Any) -> dict[str, PermissionProfile]:
    _require(isinstance(raw, dict) and raw, "permission_profiles must be a non-empty object")
    profiles = {}
    for name, spec in raw.items():
        _require(isinstance(spec, dict), f"permission_profiles.{name} must be an object")
        _require(isinstance(spec.get("shell"), bool), f"permission_profiles.{name}.shell must be a boolean")
        _require(isinstance(spec.get("network"), bool),
                 f"permission_profiles.{name}.network must be a boolean")
        _require(spec.get("write_scope") in WRITE_SCOPES,
                 f"permission_profiles.{name}.write_scope must be one of {', '.join(WRITE_SCOPES)}")
        profiles[name] = PermissionProfile(
            name=name, shell=spec["shell"], network=spec["network"],
            write_scope=spec["write_scope"],
        )
    return profiles


def _build_profiles(raw: Any, adapters: dict[str, AdapterConfig],
                    permissions: dict[str, PermissionProfile]) -> dict[str, Profile]:
    _require(isinstance(raw, dict) and raw, "profiles must be a non-empty object")
    profiles = {}
    for name, spec in raw.items():
        _require(isinstance(spec, dict), f"profiles.{name} must be an object")
        adapter = spec.get("adapter")
        _require(isinstance(adapter, str) and adapter in adapters,
                 f"profiles.{name}.adapter must name a configured adapter")
        model = spec.get("model")
        _require(isinstance(model, str) and model.strip() != "",
                 f"profiles.{name}.model is required")
        permission = spec.get("permission_profile")
        _require(isinstance(permission, str) and permission in permissions,
                 f"profiles.{name}.permission_profile must name a configured permission profile")
        provider = spec.get("provider")
        _require(provider is None or (isinstance(provider, str) and provider.strip() != ""),
                 f"profiles.{name}.provider must be a string or null")
        reasoning = spec.get("reasoning", {})
        _require(isinstance(reasoning, dict), f"profiles.{name}.reasoning must be an object")
        options = spec.get("options", {})
        _require(isinstance(options, dict), f"profiles.{name}.options must be an object")
        profiles[name] = Profile(
            name=name, adapter=adapter, model=model, permission_profile=permission,
            provider=provider, reasoning=tuple(reasoning.items()), options=tuple(options.items()),
        )
    return profiles


def _build_routing(raw: Any, profiles: dict[str, Profile],
                   adapters: dict[str, AdapterConfig], label: str,
                   required_keys: tuple[str, ...]) -> dict[str, dict[str, str]]:
    _require(isinstance(raw, dict), f"{label} must be an object")
    for key in required_keys:
        _require(isinstance(raw.get(key), dict) and raw[key],
                 f"{label}.{key} must be a non-empty object")
    routing = {}
    for key, roles in raw.items():
        _require(isinstance(roles, dict) and roles, f"{label}.{key} must be a non-empty object")
        routing[key] = {}
        for role, profile_name in roles.items():
            _require(isinstance(role, str) and role, f"{label}.{key} has an empty role name")
            _require(isinstance(profile_name, str) and profile_name in profiles,
                     f"{label}.{key}.{role} must reference a profile")
            adapter = profiles[profile_name].adapter
            _require(adapters[adapter].enabled,
                     f"{label}.{key}.{role} uses profile {profile_name!r} on disabled adapter "
                     f"{adapter!r}")
            routing[key][role] = profile_name
    return routing
