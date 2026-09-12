# DeskPet v4.4.0 — SourceLink

> Research snapshot: 2026-09-12  
> DeskPet baseline: `main@9635e1bac225378fd120e32f0a6f89512da46069` (`4.3.1`)  
> Purpose: sources used to design passive, zero-hook Codex Desktop / ZCode Desktop monitoring.  
> Trust labels used below: **Official contract**, **Upstream implementation**, **Empirical issue**, **Third-party reverse evidence**.

## DeskPet current implementation baseline

- [DeskPet current audited commit `9635e1b`](https://github.com/invincible-summer/Deskpet-For-Agents/commit/9635e1bac225378fd120e32f0a6f89512da46069) — v4.4.0 plan baseline.
- [AGENTS.md @ `9635e1b`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/AGENTS.md) — project-wide passive-monitoring, safety/privacy, plan and acceptance contract.
- [README.md @ `9635e1b`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/README.md) — current supported Agents/product behavior.
- [agents/models.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/agents/models.py) — current process-centric AgentInstance/Observation/AgentTarget model; v4.4 adds Desktop logical-session identity here.
- [agents/discovery.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/agents/discovery.py) — current one-pass Windows/WSL process discovery; current broad `codex*.exe` classification is the desktop/CLI ambiguity v4.4 must remove.
- [agents/base.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/agents/base.py) — current bounded file watcher, mutual-unique process/session binding and late-start fallback. Remains CLI-specific in v4.4.
- [agents/codex.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/agents/codex.py) — existing Codex rollout state parser reused by Codex Desktop exact rollout paths.
- [agents/monitor.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/agents/monitor.py) — current process/session/terminal evidence orchestration; v4.4 inserts DesktopSessionSource before StateReducer.
- [agents/state.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/agents/state.py) — state precedence/TTL; retained unchanged.
- [agents/paths.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/agents/paths.py) — canonical data roots / containment rules.
- [agents/tailer.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/agents/tailer.py) — bounded incremental file tailer.
- [agents/process_watch.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/agents/process_watch.py) — single blocking Windows process-exit watcher reused for DesktopHost leases.
- [agents/terminal_uia.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/agents/terminal_uia.py) — current single-MTA, event-driven UIA observer and bounded visible-text reads.
- [agents/terminal_resolver.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/agents/terminal_resolver.py) — terminal-only binding boundary retained by v4.4.
- [agents/terminal_service.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/agents/terminal_service.py) — terminal observation/activation service; Desktop surface must not be routed here.
- [actions/winkeys.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/actions/winkeys.py) — fail-closed HWND/PID/create-time validation and no input injection.
- [pet/app.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/pet/app.py) — startup/shutdown and activation dispatch integration point.
- [pet/config.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/pet/config.py) — config v5, runtime identity non-persistence, monitor cadence clamps, eligible kinds.
- [pet/presentation.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/pet/presentation.py) — aggregate/fleet consumes AgentTarget and should remain surface-agnostic.
- [pet/dashboard.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/pet/dashboard.py) — ZCode eligible-kind UI integration.
- [.github/workflows/test.yml](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/.github/workflows/test.yml) — current Windows/Python 3.12 CI and blocking benchmarks.
- [tests/benchmark_monitor.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/tests/benchmark_monitor.py) — existing monitor resource budget.
- [tests/benchmark_presentation.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/tests/benchmark_presentation.py) — existing concurrent presentation budget.
- [tests/benchmark_ui_architecture.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9635e1bac225378fd120e32f0a6f89512da46069/tests/benchmark_ui_architecture.py) — current UI scheduling/shutdown budget.

## Codex Desktop — official product contracts

- **Official contract** — [Introducing the Codex app](https://openai.com/index/introducing-the-codex-app/) — Codex app is designed to manage multiple agents in parallel; page includes the March 4, 2026 Windows availability update.
- **Official contract** — [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540) — current supported Codex clients include ChatGPT desktop app (Codex mode), CLI, IDE and web; Windows local Codex behavior is part of the same product family.
- **Official contract** — [ChatGPT Work and Codex](https://help.openai.com/en/articles/20001275/) — current ChatGPT desktop app exposes Codex as a separate local desktop view; history is separate from ordinary ChatGPT history.
- **Official contract** — [Codex App Server](https://learn.chatgpt.com/docs/app-server) — thread/turn/item model, initialize handshake, `thread/read`, `thread/list`, `thread/loaded/list`, runtime thread status, `thread/status/changed`, approvals/server requests and transport behavior. Important research source, but not used as a v4.4 production monitoring control plane because initialization is not side-effect-free in the current upstream implementation.

## Codex — audited upstream implementation snapshot

The following links pin the upstream code audited for this plan to `openai/codex@654b0a77d0d2f81aa21f61caf7af4be88fe550bb`. These are implementation evidence, not immutable public API contracts.

- **Upstream implementation** — [state migration 0001: threads table](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/state/migrations/0001_threads.sql) — `id`, `rollout_path`, `updated_at`, `source`, `cwd`, `title`, `approval_mode`, `has_user_event`, `archived`, etc.
- **Upstream implementation** — [state migration 0030: `thread_source`](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/state/migrations/0030_threads_thread_source.sql) — root/user vs generated thread filtering evidence.
- **Upstream implementation** — [state migration 0053: `originator`](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/state/migrations/0053_threads_originator.sql) — client-origin metadata used as positive Desktop provenance when present.
- **Upstream implementation** — [originator tags](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/otel/src/metrics/tags.rs) — current known originators include `codex_desktop`, CLI/TUI/VSCode families.
- **Upstream implementation** — [default client/originator logic](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/login/src/auth/default_client.rs) — current first-party/originator behavior and desktop-related values.
- **Upstream implementation** — [rollout persistence policy](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/rollout/src/policy.rs) — durable turn markers vs transient non-persisted approval/input events. This is the key reason v4.4 must not infer hidden approval from rollout silence.
- **Upstream implementation** — [App Server thread data](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs) — thread object/status/source shapes.
- **Upstream implementation** — [ThreadWatchManager](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/app-server/src/thread_status.rs) — runtime `waitingOnApproval` / `waitingOnUserInput` facts and `thread/status/changed`.
- **Upstream implementation** — [App Server transport routing](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/app-server/src/transport.rs) — notifications broadcast to initialized connections unless opted out.
- **Upstream implementation** — [App Server initialize processor](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/app-server/src/request_processors/initialize_processor.rs) — critical safety finding: ordinary client initialize can mutate process-global client metadata / `USER_AGENT_SUFFIX`; only internal non-originating client names are exempt. Therefore DeskPet v4.4 does not attach as a live observer.
- **Upstream implementation** — [control socket transport](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/app-server-transport/src/transport/unix_socket.rs) — App Server control socket uses WebSocket over UDS.
- **Upstream implementation** — [cross-platform UDS](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/uds/src/lib.rs) — Windows protected socket-directory DACL and peer-security implementation.
- **Upstream implementation** — [App Server daemon/control socket](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/app-server-daemon/src/lib.rs) — control socket/daemon lifecycle evidence.

## Codex — empirical multi-client / desktop issues

Issues are useful evidence of real behavior and capability gaps; they are not stable contracts.

- **Empirical issue** — [openai/codex#40134 — Allow Codex Desktop to connect to an externally managed App Server](https://github.com/openai/codex/issues/40134) — illustrates that transport multi-connection and full Desktop multi-client ownership are not the same guarantee; approval/active-writer ownership needs explicit semantics.
- **Empirical issue** — [openai/codex#37967 — Remote Control cannot attach reliably to an already-live CLI session](https://github.com/openai/codex/issues/37967) — further evidence that secondary-client semantics must not be assumed.
- **Empirical issue** — [openai/codex#20864](https://github.com/openai/codex/issues/20864) — evidence around shared local session history / scaling cost when broad-scanning rollouts.
- **Empirical issue** — [Windows Terminal #19783](https://github.com/microsoft/terminal/issues/19783) — still relevant to CLI activation: no reliable external `WT_SESSION -> exact existing tab` activation contract.

## ZCode — official product contracts

- **Official contract** — [Install](https://zcode.z.ai/en/docs/install) — current ZCode desktop platform support including Windows.
- **Official contract** — [ZCode Agent / Side Conversation](https://zcode.z.ai/en/docs/agents) — Side Conversation is desktop-only, fully capable, permission-aware, per-window, temporary and not part of normal task history.
- **Official contract** — [Safety Confirmation](https://zcode.z.ai/en/docs/safety-confirm) — permission requests pause the task, are task-scoped, remain pending when navigating away, and can appear as waiting confirmation in the sidebar.
- **Official contract** — [Usage Stats](https://zcode.z.ai/en/docs/usage-stats) — App Usage reads local ZCode session records on the current device; confirms the existence/usefulness of a local persistent session data plane.
- **Official contract** — [Hooks](https://zcode.z.ai/en/docs/hooks) — Hooks are a configurable local subprocess protocol and require configuration such as `~/.zcode/cli/config.json` / `hooks.enabled`; permission hooks can affect behavior. Explicitly excluded from DeskPet's zero-configuration observer architecture.
- **Official contract** — [Subagents](https://zcode.z.ai/en/docs/subagents) — ZCode can run foreground/background subagents in parallel; DeskPet treats these as internal work of the parent task, not user conversations/pets.
- **Official contract** — [Plugin](https://zcode.z.ai/en/docs/plugin) — plugins can bundle hooks/MCP/subagents. DeskPet does not require a plugin for monitoring.

## ZCode — third-party reverse evidence

These sources demonstrate current implementation details but are not official ZCode API contracts. Production code must feature-detect and fail closed when the schema changes.

- **Third-party reverse evidence** — [yiyanwannian/zcode-monitor @ `fe8857f`](https://github.com/yiyanwannian/zcode-monitor/tree/fe8857f23708e7b45b6bbb00a075021ddb799984) — independent read-only ZCode local monitoring implementation.
- **Third-party reverse evidence** — [zcode-monitor `server/db.js`](https://github.com/yiyanwannian/zcode-monitor/blob/fe8857f23708e7b45b6bbb00a075021ddb799984/server/db.js) — current `~/.zcode/cli/db/db.sqlite` location and observed session/model/tool/turn database usage.
- **Third-party reverse evidence / negative design reference** — [zcode-monitor `server/zcode-runtime.js`](https://github.com/yiyanwannian/zcode-monitor/blob/fe8857f23708e7b45b6bbb00a075021ddb799984/server/zcode-runtime.js) — documents current WAL behavior but opens a writable connection after ZCode exit to checkpoint. DeskPet deliberately does **not** adopt that behavior.
- **Third-party reverse evidence** — [zcode-acp](https://github.com/coder/zcode-acp) — evidence that private ZCode agent/app-server permission flows are bidirectional control requests. Useful for understanding why DeskPet must not attach to the private control plane as a silent observer.

## SQLite read-only concurrency references

- **Official contract** — [SQLite URI filenames](https://www.sqlite.org/uri.html) — `mode=ro`; also explains `nolock` risks. DeskPet uses read-only URI mode and does not use `nolock`.
- **Official contract** — [SQLite `PRAGMA query_only`](https://www.sqlite.org/pragma.html#pragma_query_only) — defense-in-depth read-only connection behavior.
- **Official contract** — [SQLite Write-Ahead Logging](https://www.sqlite.org/wal.html) — WAL readers/writers, checkpoint behavior and constraints. DeskPet reads a live WAL DB but never checkpoints or repairs it.
- **Python contract** — [Python 3.12 `sqlite3`](https://docs.python.org/3.12/library/sqlite3.html) — stdlib database client used to avoid a new runtime dependency.

## Windows UI Automation / activation / process-liveness references

- **Official contract** — [UI Automation threading](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading) — UIA clients should use a dedicated non-UI MTA thread and manage event handlers there.
- **Official contract** — [UI Automation Event IDs](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-event-ids) — event-driven observer reference.
- **Official contract** — [IUIAutomationElement::GetRuntimeId](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-getruntimeid) — runtime-only opaque identity; not persisted.
- **Official contract** — [SetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow) — foreground restrictions.
- **Official contract** — [FlashWindowEx](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-flashwindowex) — non-invasive fallback when foreground activation is denied.
- **Official contract** — [GetWindowThreadProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowthreadprocessid) — HWND owner verification.
- **Official contract** — [GetClassNameW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getclassnamew) — window-class identity evidence.
- **Official contract** — [OpenProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocess) — SYNCHRONIZE process-handle acquisition.
- **Official contract** — [WaitForMultipleObjects](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitformultipleobjects) — shared blocking process-exit watcher.
- **Official contract** — [Terminating a Process](https://learn.microsoft.com/en-us/windows/win32/procthread/terminating-a-process) — terminated process object becomes signaled.

## Windows Terminal / WSL references retained for mixed CLI + Desktop behavior

- [Windows Terminal command-line arguments](https://learn.microsoft.com/en-us/windows/terminal/command-line-arguments)
- [microsoft/terminal#19783](https://github.com/microsoft/terminal/issues/19783)
- [microsoft/terminal#19818](https://github.com/microsoft/terminal/issues/19818)
- [microsoft/terminal#18692](https://github.com/microsoft/terminal/issues/18692)
- [Default Terminal spec #492](https://github.com/microsoft/terminal/blob/main/doc/specs/%23492%20-%20Default%20Terminal/spec.md)
- [`/proc/PID/stat`](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html)
- [`/proc/PID/cwd`](https://man7.org/linux/man-pages/man5/proc_pid_cwd.5.html)
- [`ps(1)`](https://man7.org/linux/man-pages/man1/ps.1.html)
- [WSL basic commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

## Existing Agent upstream references retained

- [Codex protocol.rs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs)
- [anthropics/claude-code#53037](https://github.com/anthropics/claude-code/issues/53037)
- [Kimi data locations](https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/data-locations.md)
- [Kimi interactionOps.ts](https://github.com/MoonshotAI/kimi-code/blob/main/packages/agent-core-v2/src/agent/interaction/interactionOps.ts)
- [Kimi promptOps.ts](https://github.com/MoonshotAI/kimi-code/blob/main/packages/agent-core-v2/src/agent/prompt/promptOps.ts)
- [pi session format](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/session.md)
- [pi AI types](https://github.com/badlogic/pi-mono/blob/main/packages/ai/src/types.ts)

## Source interpretation rules for 4.4.0

1. Official product/API documentation defines intended behavior.
2. Upstream source at a pinned commit can justify an implementation strategy, but its private details must be capability-detected.
3. GitHub issues provide empirical failure/compatibility evidence, not API guarantees.
4. Third-party reverse-engineered ZCode schema is a probe target, not a compile-time contract.
5. If an implementation detail conflicts with DeskPet's passive/no-side-effect contract, DeskPet must decline that path even if it exposes richer state.
6. Ambiguous identity/status must degrade to UNKNOWN/WORKING rather than be guessed.
7. No source justifies hooks, Agent configuration changes, input injection, DB writes, WAL checkpointing, memory injection, or automatic approval.
