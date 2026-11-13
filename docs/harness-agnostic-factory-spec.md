# Harness agnostic software factory

Status: foundation implemented (portable roles, contracts, profiles, capabilities,
fake adapter, GitHub boundary, controller skeleton, deterministic tests);
process-backed harness adapters pending  
Date: 2026-11-13  
Baseline: standalone import commit `5d5fed8`  
Primary initial backends: Kilo Code with OpenRouter, then Codex CLI  
Compatibility backend: Claude Code CLI

## 1. Outcome

Run the same issue → intake → specification → build → gate → review → approval
pipeline through different coding harnesses without rewriting the factory or its
role instructions. Select a harness and model independently for each role.

The first release must support a complete job through Kilo Code using a selected
OpenRouter model, a complete job through Codex CLI, and a job whose builder and
reviewers use different harnesses. Preserve the original Claude workflow until
its replacement passes the same behavioral tests.

A harness provides the coding agent's tool loop, file access, terminal execution,
sessions, and permission system. A provider supplies inference; OpenRouter is a
provider gateway. A model is the selected reasoning engine. Supporting OpenRouter
alone does not make harness behavior portable.

This project supplies orchestration around existing harnesses. It does not build
another coding agent loop or call raw model APIs to implement file editing.

## 2. Verified baseline and limits

These observations come from source inspection at the baseline, not a successful
end-to-end factory run:

- `.claude/skills/factory/SKILL.md` is the controller today. It depends on Claude's
  Agent tool, background completion notifications, and SendMessage.
- Eight role definitions live under `.claude/agents/`. Their bodies describe
  portable work, but their frontmatter uses Claude tool names and model aliases.
- `factory/bin/job` owns `job.json`, legal transitions, GitHub interactions,
  worktree creation, and merging. `route` selects reviewers from risk and surfaces.
- `factory/bin/gate` runs the controller checkout's checks in a job worktree and
  records a commit SHA. Its optional hook parses Claude-specific transcript data.
- Cost accounting in `job` scans Claude's private transcript layout. The dashboard
  expects a numeric `cost_usd`; unknown usage is not represented distinctly.
- The builder commits, pushes, and opens a draft PR. The controller's merge
  script synchronizes the base, gates again, pushes, and calls `gh pr merge`.
- Builder and visual reviewer prompts depend on `agent-browser` and allocated
  ports. Browser availability must therefore be declared separately from harness.
- `factory/checks.sh` is a placeholder that exits 1. The imported project has no
  configured passing validation pipeline.

Local CLI help was inspected without launching model work: Kilo 7.8.12 exposes
`run`, model and agent selection, JSON events, directory selection, and session
continuation. Codex exposes `exec`, JSON events, a working directory, model
selection, sandbox options, and additional writable directories. Claude 2.1.319
exposes noninteractive print mode, structured output options, tools, and resumption.
These surfaces make adapters plausible; authentication, permissions, event shapes,
usage semantics, and end-to-end execution still require adapter qualification.

## 3. Scope

### Required

- A local Python controller that continues independently of a chat turn.
- Shared role definitions and a versioned invocation/result contract.
- Kilo/OpenRouter and Codex adapters, followed by Claude compatibility.
- Durable state, bounded concurrency, cancellation, recovery, and structured logs.
- Existing Markdown deliverables, round limits, labeled GitHub queue, worktree
  isolation, deterministic gates, and one PR per issue.
- Optional thin skills or commands that invoke the same controller from a harness.

### Outside the first release

- Arbitrary model compatibility, model benchmarking, or automatic model selection.
- A new inference gateway, universal tool API, distributed workers, cloud service,
  scheduling platform, or replacement for the harnesses' own coding tools.
- Remote conversation transfer between harnesses.
- Changing the source license, deploying application code, or supporting every
  editor extension. Kilo CLI is the first Kilo automation target.

## 4. Architecture and ownership

The proposed flow is: user or thin skill → factory controller → job state machine
→ adapter → harness → model provider. Workers return evidence to the controller;
only the state machine decides whether the next stage is legal.

The controller owns scheduling, role/profile resolution, worktree and port
allocation, invocation records, stage validation, retries, and recovery. Keep
Python and reuse the existing scripts where their behavior is correct. `job`
remains the only writer of `job.json`; expose additional commands rather than
writing around it. Locks and atomic file replacement must protect state writes.

Adapters own executable discovery, supported-version checks, argument generation,
prompt delivery, environment setup, permission translation, event normalization,
process lifecycle, and optional session resumption. They never decide a job's
stage, merge a PR, choose a fallback model, or invent successful results.

Role definitions own the task and output contract. Store canonical instructions
in `factory/roles/`, without harness tool names, Claude model aliases, stop hooks,
or assumptions about native background agents. Generated harness wrappers are
optional convenience files, never a second source of truth.

The existing GitHub and filesystem integrations remain first-class dependencies.
The controller must not inherit orchestration behavior from whichever chat launched
it. In an embedding environment that requires its own delegation interface, use
an explicit integration instead of bypassing that environment's rules.

## 5. Adapter contract

Expose internal operations `probe()`, `start(request)`, `events(handle)`,
`cancel(handle)`, and optionally `resume(request, session_id)`. These are Python
interfaces, not promised CLI commands. Process-backed adapters may use threads or
async subprocesses; the public contract must not depend on that choice.

A versioned request must contain:

- Invocation UUID, job ID, stage, round, attempt, and role.
- Absolute controller checkout, job directory, and worktree paths; expected branch
  and input commit SHA when a worktree exists. Intake runs before worktree creation.
- Role instruction path and digest; explicit input artifacts and allowed outputs.
- Resolved harness profile, provider/model selector, and supported reasoning options.
- Required capabilities, read/write boundaries, network and GitHub access policy.
- Deadline, cancellation grace period, browser session/port if required, and optional
  harness session ID. Credentials are references to existing auth, never prompt text.

A normalized result must contain invocation identity, terminal status, exit code,
session ID if available, observed harness version/model, start/end timestamps,
artifact paths and hashes, observed code commit, usage when available, and a
structured error when applicable. Status is one of `completed`, `failed`,
`blocked`, `cancelled`, or `timed_out`.

`completed` means the process finished with validated transport output. It does
not mean acceptance criteria passed. The controller must independently validate
role artifacts, freshness, expected commit, and the gate before advancing.
A zero exit code or a final chat message is insufficient evidence.

Normalize events into `started`, `progress`, `usage`, `artifact`, and `finished`,
with timestamps and invocation ID. Preserve a versioned raw event log for diagnosis.
Truncated or malformed streams fail the invocation unless the adapter can establish
a documented terminal result. Unsupported required capabilities fail before launch.

## 6. Configuration and capabilities

Replace hard-coded model aliases in routing with named execution profiles.
Profiles reference an adapter and its model/provider configuration. Risk and surface
routing select profiles; role defaults cover intake, spec writer, builder, and approver.
Snapshot the resolved configuration and instruction hashes for every invocation.

Illustrative configuration, not a runnable configuration or a model recommendation:

```json
{
  "schema_version": 2,
  "profiles": {
    "kilo-builder": {
      "adapter": "kilo",
      "model": "<validated OpenRouter model selector>",
      "permission_profile": "builder"
    },
    "codex-reviewer": {
      "adapter": "codex",
      "model": "<available Codex model>",
      "permission_profile": "reviewer"
    }
  },
  "roles": {"builder": "kilo-builder"},
  "routing": {"low": {"code": "codex-reviewer"}},
  "limits": {"jobs": 3, "workers": 4, "per_adapter": {"kilo": 2, "codex": 2}},
  "merge_policy": "human"
}
```

The complete schema must require profiles for every enabled role and risk route.
Adapter-specific options live in their own namespace and are schema validated;
unsupported options must not be silently ignored. Never assume reasoning settings
or provider selection flags have identical semantics across harnesses.

Probe capabilities including noninteractive execution, machine-readable completion,
model selection, session resumption, usage reporting, cancellation, filesystem
restrictions, shell control, network control, and browser tooling. Distinguish
native enforcement from external isolation and prompt-only instructions.

Profiles that need enforceable restrictions must be rejected if neither the harness
nor a supported external sandbox can enforce them. Do not describe an instruction
such as “never edit code” as a security boundary. Intake must retain its no-shell
restriction. Reviewers need read access to code and write access to their output;
blanket read-only mode alone cannot satisfy both requirements.

## 7. Worker execution and artifacts

Keep existing filenames and first-line formats: `intake.md`, `spec.md`, `build.md`,
`round-N/checks.md`, `round-N/review-<role>.md`, and `decision.md`.
Add invocation metadata under `factory/jobs/<id>/runs/<invocation-id>/`, including
request/result JSON, events, and attempt-scoped output. The controller validates
and promotes completed output to canonical paths atomically, through state-machine
commands. Failed attempts remain diagnostic evidence and cannot satisfy a stage.

Each role receives the same canonical instructions and explicit filesystem context
regardless of harness. Keep issue text in quoted input artifacts, separate from
trusted control instructions. Read the target repository's `AGENTS.md` and other
applicable instructions. Record inherited configuration and instruction sources
where the harness exposes them; unexpected automatic behavior is a qualification risk.

Use explicit process working directories and argument arrays. Never interpolate
issue text, prompts, model selectors, or paths into shell command strings. Workers
must not write `job.json`, gates, other reviewers' files, or controller configuration.
Grant access to both the worktree and assigned output directory where required.

Build roles retain commit/push/draft-PR responsibilities initially. No worker may
merge or deploy. Controller checks must verify the expected repository, branch,
clean tree, pushed commit, and single PR before gating. Do not claim branch or
network restrictions are enforced unless the adapter qualification proves it.

Optional native subagents and stop hooks are optimizations only. They cannot be
required for stage progression, artifact validity, or retries. Explicit controller
execution of the gate replaces reliance on the builder's Claude stop hook.

## 8. Recovery, concurrency, and side effects

- Acquire one controller lease per target repository and one active invocation
  lease per job/stage/role. Use atomic allocation; two controllers must not start
  duplicate work or both drain the same slot.
- Enforce job, global worker, and per-adapter limits independently. Reviewers may
  run concurrently against one pinned commit; builders are exclusive per job.
- Store each attempt before starting its process. After a crash, reconcile process
  identity, validated artifacts, branch state, and GitHub state before restarting.
  An old PID alone is not a valid ownership check.
- Retry transient launch/provider errors under a separate bounded infrastructure
  budget. Code/review failures consume the existing build-round budget. Never
  retry mutations blindly or change provider/model automatically.
- If push, PR creation, comment, or merge has an uncertain outcome, query GitHub
  and reconcile before repeating it. Store operation identities and reuse the
  existing comment marker where appropriate.
- Cancel the owned process group, wait a bounded grace period, then stop its owned
  children. Stop only that worker's browser and server. Cancellation must be
  acknowledged before releasing its write lease or launching another builder.
- A restart may use the original harness session if supported. Otherwise start a
  fresh session from durable artifacts and Git history. Changing harness requires
  an explicit profile selection, a new invocation, and an audit entry; no silent
  fallback. Human approval decisions survive restarts.
- A manually merged PR terminates remaining work after reconciliation. Late
  completions from cancelled or superseded attempts cannot advance the job.

## 9. Validation, review, and merge policy

Run deterministic checks from the trusted controller checkout in the job worktree.
Preserve protected-path checks and bounded rounds. Bind every gate, review, and
approval to the current commit and input-artifact digest. Changed code invalidates
previous evidence. Wait for every routed reviewer; missing, invalid, or stale
reviews are never PASS.

Preserve the existing base synchronization and final gate. If synchronization
changes the code reviewed, invalidate approval and return for review of the new
commit before merging. Reconcile the remote base and head again at merge time;
GitHub branch protection and required checks remain authoritative.

New installations default to human merge approval. Automatic merge is an explicit
repository setting and still requires all deterministic checks, current reviews,
and the approver result. Migration of existing auto-merge settings must be visible
and intentional. This proposed default is a behavior change from the import.

Browser verification is a declared worker capability. Missing browser support
blocks a visual acceptance criterion instead of producing an unverified PASS.
Keep browser sessions and ports isolated across jobs and clean up owned resources.

## 10. Usage and observability

Collect usage from supported invocation events/results rather than scanning private
home-directory transcripts. Record harness, provider, requested and observed model,
tokens, cached-token semantics, duration, and retries per invocation when available.
Unknown fields are null, not zero. Avoid double-counting cumulative stream updates.

Keep reported provider cost distinct from estimated API-equivalent cost and unknown
cost. Estimates record currency, pricing source/date, and calculation version.
Subscription usage estimates are not billing claims. Dashboard and summary output
must support partial/unknown totals and show the harness/model for each role.
Telemetry failure must not block an otherwise valid state transition.

Do not record secrets or auth files. Sanitize adapter logs, restrict runtime-file
permissions, and document local log retention. No automatic external upload.

## 11. Proposed layout and user entry points

```text
factory/
  bin/run                 # start/resume the controller
  bin/doctor              # configuration and adapter preflight
  bin/job                 # authoritative state transitions
  bin/gate
  bin/route
  core/                   # scheduler, contracts, artifacts, recovery
  adapters/               # kilo, codex, claude, deterministic test adapter
  roles/                  # eight shared role instructions
  schemas/                # configuration, invocation, result, event schemas
  config.json
  checks.sh
docs/
  harness-agnostic-factory-spec.md
```

Proposed commands are `factory/bin/doctor`, `factory/bin/run`, and
`factory/bin/run <issue-number>`. They do not exist at this baseline. Doctor reports
missing executables/auth, unsupported capabilities, unresolved profiles, and the
placeholder checks script without posting GitHub comments or launching paid work.
A separate explicit smoke-test command may perform a small model invocation.

Thin Claude, Codex, or Kilo skills delegate to these commands and expose status,
feedback, and cancellation. Document actual invocation syntax for each client.
Installations target an explicitly selected GitHub repository; controller startup
must refuse an accidental or mismatched checkout before creating worktrees or
mutating GitHub. Retain GPL licensing and source attribution.

## 12. Migration plan

1. Capture existing job transitions and artifact formats in deterministic tests.
   Add a fake harness and fake GitHub boundary; configure real checks for this
   repository rather than making the placeholder report success.
2. Extract portable roles and add versioned contracts, profile validation,
   capabilities, and invocation metadata. Remove cost parsing from `sync`'s
   critical path. Preserve the original entry point during migration.
3. Implement the Python controller and Kilo/OpenRouter adapter. Qualify one pinned
   Kilo version and explicit model, including permissions and artifact output.
4. Add Codex and prove mixed-harness execution. Add Claude through the same contract;
   only then retire duplicate orchestration in the original skill and its hook.
5. Update the dashboard, installation guide, examples, and per-harness wrappers.
   Publish tested version ranges and capability limitations.

Existing jobs require a dry-run migration report and a backup of runtime state.
Add schema versions with explicit conversions; never infer a new job from a partial
old one. Preserve branches, PR URLs, round history, waiting decisions, and old usage
as labeled legacy estimates. Roll back by restoring the state backup and old
controller only after stopping new workers. Avoid running two controller versions
against the same job directory.

## 13. Acceptance and qualification

Use deterministic tests first, then opt-in integration runs in a disposable local
repository and dedicated test GitHub repository. No real queued client work is
required to validate adapters.

The release is accepted when:

1. The fake adapter drives every transition, including NEEDS-INFO, rejection,
   blocked build, failed gate, CHANGES, round exhaustion, and human approval.
2. Kilo/OpenRouter and Codex each complete the same small fixture feature through
   draft PR creation. A mixed-harness run produces identical artifact contracts.
3. A failed gate launches no reviewers; all routed reviewers are required; stale
   or malformed output and zero-exit/missing-artifact runs cannot advance.
4. Killing the controller during build, review, and an uncertain GitHub mutation
   resumes without duplicate builders, PRs, comments, or merges.
5. Cancellation stops owned work and suppresses late results; two controllers
   respect leases and all worker limits.
6. Every evidence artifact records its commit. A changed head or base invalidates
   the appropriate gate/review/approval and prevents stale merging.
7. Missing auth, a nonexistent model, unsupported restrictions, rate limits,
   malformed event streams, timeout, and absent usage produce explicit outcomes.
   Missing usage displays as unknown and does not deadlock a job.
8. Code-write attempts by a restricted reviewer and shell attempts by intake are
   denied by the qualified permission implementation. If enforcement cannot be
   demonstrated, that profile is unsupported rather than advertised as isolated.
9. Jobs cannot write into another job's worktree or output, and process cleanup
   leaves unrelated dev servers and browsers running.
10. Fresh sessions reconstruct work from artifacts without native resume support.
    Native resume is tested separately where advertised. Changing harness does
    not require conversation conversion.
11. Visual fixture changes have desktop/mobile browser evidence when routed to UI
    review. Unavailable required tooling causes a block with a specific reason.
12. Claude compatibility passes the same contract suite. User docs distinguish
    verified adapters from planned ones and list the actual tested versions.

## 14. Decisions to resolve during implementation

- Which filesystem/shell enforcement mechanisms can each CLI actually provide
  while allowing one designated output directory? Prove this before qualification.
- What terminal-event and usage schemas do the pinned CLI versions emit? Capture
  redacted fixtures; help text alone does not establish these contracts.
- Which OpenRouter models are suitable for each role and available to the operator?
  Qualify explicit choices; do not promise equal behavior across models.
- Is a CLI adapter sufficient for Kilo's lifecycle requirements, or does its server
  interface materially improve cancellation and recovery? Start with CLI and
  change only on demonstrated limitations.
- Which inherited skills, hooks, MCP servers, and user settings are required or
  prohibited for reproducible workers? Record per-adapter behavior and validate
  against the target repository's instructions.

## References

- Local baseline: `DESIGN.md`, `INSTALL.md`, `.claude/skills/factory/SKILL.md`,
  `.claude/agents/`, `factory/bin/{job,gate,route}`, and `factory/config.json`.
- [Upstream origin](../UPSTREAM.md).
- [Kilo CLI reference](https://kilo.ai/docs/code-with-ai/platforms/cli-reference).
- [Kilo with OpenRouter](https://kilo.ai/docs/ai-providers/openrouter).
- [Codex noninteractive execution](https://learn.chatgpt.com/docs/non-interactive-mode).
- [Codex skills](https://learn.chatgpt.com/docs/build-skills).
- [OpenRouter tool calling](https://openrouter.ai/docs/guides/features/tool-calling).

External interfaces change. Source inspection and local help establish the baseline;
the acceptance suite establishes supported behavior for a particular release.
