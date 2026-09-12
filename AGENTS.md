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

Before substantial work, re-read the current repository and the active plan. Treat the
plan's semantics, interfaces and acceptance criteria as implementation requirements,
not suggestions. If repository reality conflicts with a plan, update the plan explicitly
before changing architecture.

During implementation, continuously compare the code against the plan and avoid scope
creep. Necessary decisions must be documented with the chosen option and rationale.

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

## Documentation

README describes current user behavior and maintained architecture; release notes carry
version history; SourceLink records external research evidence. Keep commands, paths,
versions and file names synchronized with the actual repository.
