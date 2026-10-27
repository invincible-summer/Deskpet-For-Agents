# DeskPet Repository Development Contract

## Project goal

DeskPet is a lightweight Windows desktop pet that passively observes supported AI
Agent sessions across Windows/WSL terminals and supported desktop clients. Keep the
application low-CPU, low-memory, deterministic and fail-closed. Monitoring must not
require hooks, input injection, automatic approval or writes to Agent-owned data.

## Architecture discipline

Keep boundaries explicit: discovery/session parsing/state reduction belong in
`agents/`; UI/presentation/config/skin runtime belong in `pet/`; public Win32 window
actions belong in `actions/`; probes/build utilities belong in `tools/`. Do not mix
packaging, persistence or UI concerns into Monitor/state reducers.

Prefer one clear implementation over parallel legacy paths. Remove dead code and old
architecture when a replacement is accepted. Do not add abstractions without a real
boundary or test seam. Preserve implementations that already satisfy their contract.

## Plans and implementation

Before substantial work, re-read the current repository and existing docs. Treat
documented semantics, interfaces and acceptance criteria as implementation
requirements, not suggestions.

During implementation, continuously compare the code against the documented
contract and avoid scope creep. Necessary decisions must be documented with the
chosen option and rationale.

## Agent upstream alignment

Monitoring code must track each Agent's latest upstream design. When updating an
Agent integration, research the current upstream repository/docs, align the
reader to it, and refresh the version table below in the same change. Version
annotations live only in this file, not in source headers.

| Agent | Aligned upstream | Evidence date |
| --- | --- | --- |
| ZCode | 0.17.0 (live `db.sqlite` `session.version`; migrations through 0005 / 0.7.0 schema era) | 2026-10-27 |
| Codex | 0.164.0-alpha.11.1 (live `state_5.sqlite` `cli_version`; rollout JSONL event schema) | 2026-10-27 |
| OpenCode | anomalyco/opencode dev @ 86976449c40d (`session_v2` / `session_message` / `session_inbox`; `store.ts` `time_suspended` execution claim) | 2026-10-27 |
| DSH | deepseek-ai/deepseek-harness master @ 22ddee379240 (session.vN.jsonl[.zstd] event stream) | 2026-10-23 |
| pi | badlogic/pi-mono main @ 177e2ad7e41e (session JSONL event stream) | 2026-10-27 |
| Kimi Code | live `wire.jsonl` verified on a real CLI install | 2026-09 |
| Claude Code | code.claude.com docs (transcript/projects layout, permissionMode) | 2026-09 |

All sources share one stable status contract: an open turn is durable evidence
and holds WORKING through quiet gaps (bounded by an explicit crash-orphan TTL);
terminal facts (turn end, cancel, `time_idle`, latest terminal turn) close it.
Usage/stats tables that upstream writes only after a turn must never be the sole
liveness signal — pair them with the streaming fact the agent writes live
(ZCode `part.time_updated`, OpenCode `session_message` / execution claim).

## Safety and privacy

Observation is passive and least-privilege. Do not add keyboard/clipboard injection,
automatic approval, Agent database writes, WAL checkpointing, hidden control-plane
connections, or durable terminal text. Runtime identities such as PID/HWND/RuntimeId
must not be persisted. Ambiguous attribution fails closed.

## Performance

Do not add high-frequency polling where an event/revision/deadline model exists. Keep
Tk free of blocking I/O. Bound queues, caches, workers, subprocesses and shutdown time.
Do not load heavy conversion dependencies into the resident GUI process. Optimization
changes require benchmark or profiling evidence.

## Tests and acceptance

Every changed contract needs regression coverage. Run the complete unit suite and the
relevant blocking benchmarks. Release work also requires compiled-artifact acceptance
on Windows. A task is not complete while required CI/acceptance is red. Review the
final diff for stale code, duplicated paths, documentation drift and privacy/resource
regressions before declaring completion.

## Versioning and releases

Follow `VERSIONING.md` and Semantic Versioning 2.0.0 for every release decision.
Version numbers describe changes to the documented public compatibility contract, not
plan phases, refactors, CI work or internal milestones. `pet/version.py` is the single
application-version source of truth.

A published release tag is immutable project history: never move it, delete and reuse
it, or replace its release assets. If a published release needs correction, increment
the version and publish a new release. Release work is complete only after the required
source tests, blocking benchmarks and compiled-artifact acceptance pass.

## Documentation

README describes current user behavior and maintained architecture; `CHANGELOG.md`
records release history; `VERSIONING.md` defines release/version policy; release notes
carry version-specific user changes. Keep commands, paths, versions and file names
synchronized with the actual repository.
