# Source attribution

This project starts from ZazenCodes' Software Factory for Claude Code v2:
https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code-v2

Imported from commit `94f7ae7b9dced7ffa87fb4a58312c89522ee120c` on 2026-11-13.
Only that directory was imported. The original license and attribution are preserved.

This is an independently maintained repository intended to evolve into a model
and harness agnostic software factory. The initial import retains the original
Claude Code implementation.

The harness-agnostic foundation now exists alongside it: portable role
instructions in `factory/roles/`, versioned invocation contracts, schema-versioned
execution profiles, capability and permission validation, a deterministic fake
adapter, a GitHub boundary, a controller skeleton and a test suite. The Claude
skill and agents are still the working entry point; process-backed Kilo, Codex
and Claude adapters are the next migration step. See
[`docs/harness-agnostic-factory-spec.md`](docs/harness-agnostic-factory-spec.md).
