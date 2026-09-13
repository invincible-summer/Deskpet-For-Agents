# DeskPet 4.0.1 — SourceLink

> Research snapshot: 2026-09-13  
> Acceptance implementation tree: `8fe7461d46e60c31ff2da65c3983c28aa2ea0898`  
> Purpose: record the external contracts and implementation evidence used by DeskPet's passive Windows/WSL/Desktop Agent monitoring.  
> Trust labels: **Official contract**, **Upstream implementation**, **Empirical issue**, **Third-party reverse evidence**.

This file records evidence, not promises made by third-party products. Private paths, process names and database schemas are implementation observations and must remain capability-detected and fail closed. DeskPet's own safety contract is defined by the repository code, README and AGENTS.md.

## DeskPet 4.0.1 implementation

The following links pin the accepted ZCode WSL implementation tree. The final `main` squash commit is tree-equivalent; this header is updated to the final `main` SHA after merge.

- [agents/models.py @ `8fe7461`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/8fe7461d46e60c31ff2da65c3983c28aa2ea0898/agents/models.py) — `RemoteRuntimeContext` and three-state `SourceProbeSnapshot.remote_runtimes`.
- [agents/discovery.py @ `8fe7461`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/8fe7461d46e60c31ff2da65c3983c28aa2ea0898/agents/discovery.py) — fresh WSL running-distro census, ZCode remote-runtime detection, uid/user/HOME metadata, authoritative/stale/tombstone semantics.
- [agents/paths.py @ `8fe7461`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/8fe7461d46e60c31ff2da65c3983c28aa2ea0898/agents/paths.py) — pure Linux-path helpers for `~/.zcode/cli` and `~/.zcode/server`; production remote DB access does not use WSL UNC.
- [agents/zcode_remote.py @ `8fe7461`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/8fe7461d46e60c31ff2da65c3983c28aa2ea0898/agents/zcode_remote.py) — bounded direct-argv WSL transport and read-only `node:sqlite` facts reader.
- [agents/zcode_desktop.py @ `8fe7461`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/8fe7461d46e60c31ff2da65c3983c28aa2ea0898/agents/zcode_desktop.py) — local Windows + remote WSL plane projection with one global admission cap.
- [agents/monitor.py @ `8fe7461`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/8fe7461d46e60c31ff2da65c3983c28aa2ea0898/agents/monitor.py) — remote reads are owned by the existing `ProcessProbeWorker`; Monitor/UI polling never enters WSL.
- [tests/test_zcode_wsl_remote.py @ `8fe7461`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/8fe7461d46e60c31ff2da65c3983c28aa2ea0898/tests/test_zcode_wsl_remote.py) — path, discovery, transport, last-good, projection, cap and worker-gating regression coverage.
- [tools/desktop_source_probe.py @ `8fe7461`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/8fe7461d46e60c31ff2da65c3983c28aa2ea0898/tools/desktop_source_probe.py) — sanitized real-machine structure probe; no prompt/transcript/tool-argument/token output.

### 4.0.1 architecture conclusions

- Windows `ZCode.exe` remains the Desktop host and activation target. WSL remote runtime is a separate observation/data plane and never becomes a Terminal Agent target.
- A remote data plane is authorized only by the current fresh WSL process census. Failed WSL enumeration retains last-good state as non-authoritative; an authoritative stopped-distro result removes the plane.
- The remote database engine runs inside the same WSL environment as the database file. Windows does not open a live WSL SQLite/WAL file through `\\wsl.localhost`.
- ZCode's private DB schema, runtime filenames and filesystem layout are feature-detected implementation evidence, not treated as stable public API.
- Approval state is projected only from explicit session-scoped DB facts (`approval_status=requested` on a running tool). DeskPet does not synthesize approval from silence and does not approve automatically.

## ZCode — product contracts

- **Official contract** — [Remote Development](https://zcode.z.ai/en/docs/remote-development) — Remote Development supports WSL on the Windows desktop client. After connection, file reads, terminal commands, Git operations and ZCode Agent execution occur in the selected target environment, while the desktop client continues to provide account/model/task UI. The WSL flow can select a specific Linux user and prepares remote-side components on first connection.
- **Official contract** — [Safety Confirmation](https://zcode.z.ai/en/docs/safety-confirm) — permission requests pause the affected task and remain task-scoped; this supports DeskPet's requirement that WAITING attribution be session-specific rather than globally guessed.
- **Official contract** — [Hooks](https://zcode.z.ai/en/docs/hooks) — hooks are explicitly configured behavior and can participate in permissions. DeskPet therefore excludes hooks from the zero-configuration monitoring path.
- **Official contract** — [Subagents](https://zcode.z.ai/en/docs/subagents) — ZCode may run subagents as work inside a task. DeskPet folds known child-session activity into the root task rather than creating a separate pet for each subagent.
- **Official contract** — [Plugin](https://zcode.z.ai/en/docs/plugin) — plugins can bundle hooks/MCP/subagents. DeskPet monitoring does not require a plugin.
- **Official contract** — [Usage Stats](https://zcode.z.ai/en/docs/usage-stats) — ZCode exposes usage derived from local session records, supporting the general existence of a persistent local session data plane; it does not make the private schema a public API.

## ZCode — observed Remote Development implementation

These issue logs are empirical evidence of current runtime layout. They are deliberately matched narrowly and are covered by fail-closed tests because upstream may change them.

- **Empirical issue** — [zai-org/feedback #162](https://github.com/zai-org/feedback/issues/162) — ZCode 3.4 Windows/WSL connection logs show `server/zcode-server.cjs` uploaded to `~/.zcode/server/zcode-server.cjs` and the server installed in the WSL environment.
- **Empirical issue** — [zai-org/feedback #195](https://github.com/zai-org/feedback/issues/195) — remote-workspace failure logs reference the server path and an agent entry under `~/.zcode/server/agents/glm/zcode-agent`. DeskPet 4.0.1 accepts this exact current entry in addition to `zcode.cjs`.
- **Empirical issue** — [zai-org/feedback #302](https://github.com/zai-org/feedback/issues/302) — ZCode 3.7.7 remote runtime/log evidence references `zcode.cjs` under the remote `~/.zcode/server/agents/glm/` tree and remote `~/.zcode/cli/log` activity.
- **Empirical issue** — [zai-org/feedback #28](https://github.com/zai-org/feedback/issues/28) — Windows 11 + WSL2 logs independently show the remote ZCode CLI log root under `~/.zcode/cli/log/`.

## ZCode — private data-plane reverse evidence

These are not official compatibility contracts. They are retained because public ZCode documentation does not specify the internal DB schema or bundled runtime executable path.

- **Third-party reverse evidence** — [yiyanwannian/zcode-monitor @ `fe8857f`](https://github.com/yiyanwannian/zcode-monitor/tree/fe8857f23708e7b45b6bbb00a075021ddb799984) — independent read-only monitoring evidence for ZCode session/model/tool/turn records and approval-related fields.
- **Third-party reverse evidence** — [xhwxt/zcode-token-usage-statusbar @ `42b64bc`](https://github.com/xhwxt/zcode-token-usage-statusbar/tree/42b64bc25a0aceaa473e70b791d0c11d4f9e953b) — evidence that current ZCode session data is read from `~/.zcode/cli/db/db.sqlite`, including remote environments.
- **Third-party reverse evidence** — [windviki/zcode-webui @ `a9abc61`](https://github.com/windviki/zcode-webui/tree/a9abc6177e24bc94f97f4d6828f18e709d8b3d2a) — evidence for the bundled remote `~/.zcode/server/node` runtime and ZCode remote-agent layout.

DeskPet never treats these repositories as authority over ZCode. Their evidence is used only to define conservative capability probes and is corroborated with official/empirical runtime evidence where possible.

## Node.js / SQLite — remote read-only transport

- **Official contract** — [Node.js `node:sqlite`](https://nodejs.org/api/sqlite.html) — `DatabaseSync` was introduced in Node 22.5.0; it supports a read-only connection option, a bounded busy `timeout`, and extension loading disabled by default. DeskPet explicitly requests `readOnly: true`, `timeout: 40`, and `allowExtension: false` in ZCode's bundled WSL Node process.
- **Official contract** — [SQLite WAL](https://sqlite.org/wal.html) — WAL requires shared-memory coordination among processes using the database and is not designed for ordinary network-filesystem access between different hosts.
- **Official contract** — [SQLite Over a Network](https://sqlite.org/useovernet.html) — SQLite recommends keeping the database engine on the same machine as the database file when a network boundary exists. DeskPet follows this by executing `node:sqlite` inside WSL and sending only bounded JSON facts back to Windows.

The 4.0.1 remote reader issues only a fixed schema probe and fixed `SELECT`/`WITH` facts queries. It performs no `INSERT`, `UPDATE`, `DELETE`, DDL, WAL checkpoint, repair, extension loading, or database migration.

## Microsoft WSL — passive running-distro boundary

- **Official contract** — [WSL interop](https://learn.microsoft.com/en-us/windows/dev-environment/wsl-interop) — Windows can access a distro through `\\wsl$` / `\\wsl.localhost`; Microsoft documents that `\\wsl.localhost` can auto-start a distro on Windows 11. This is why DeskPet never uses a stale cached distro name or a UNC file probe as authorization to inspect a stopped distribution.
- **Official contract** — [WSL basic commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands) — `wsl --list --running` is the host-side inventory used as the fresh gate before any distro-specific `--exec` operation.

DeskPet's existing WSL discovery therefore keeps the three-state distinction: authoritative presence, authoritative absence, and non-authoritative read failure. “Could not read” is never converted into “the Agent disappeared.”

## Codex / Windows sources retained by the project

The ZCode 4.0.1 change does not alter the existing Codex Desktop/CLI safety boundary; these references remain part of the project evidence set.

- **Official contract** — [Introducing the Codex app](https://openai.com/index/introducing-the-codex-app/) — product-level multi-agent desktop behavior and Windows availability.
- **Official contract** — [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server) — thread/turn/item and approval-server-request concepts. DeskPet's current passive Desktop source does not attach to this live control plane.
- **Upstream implementation** — [openai/codex state migrations](https://github.com/openai/codex/tree/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/state/migrations) — pinned implementation evidence used for capability-detected Codex Desktop state DB reads.
- **Upstream implementation** — [Codex rollout persistence policy](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/rollout/src/policy.rs) — distinguishes durable rollout evidence from transient runtime events; DeskPet does not infer hidden approvals from rollout silence.
- **Empirical issue** — [Windows Terminal #19783](https://github.com/microsoft/terminal/issues/19783) — illustrates the lack of a stable external mapping from an arbitrary process/session to an exact already-open Windows Terminal tab; DeskPet keeps terminal activation conservative.

## Evidence interpretation and safety rules

1. **Official contract** may justify product/platform behavior, but it does not turn undocumented internal file layouts into APIs.
2. **Upstream implementation** is pinned to a commit where possible and must be capability-detected if consumed.
3. **Empirical issue** proves an observed runtime shape or failure mode, not a permanent guarantee.
4. **Third-party reverse evidence** is used only to corroborate private implementation details and never as sole authority for a destructive/action path.
5. Monitoring must remain passive: no hooks/plugin installation, no Agent config mutation, no input injection, no automatic approval, no private control-plane navigation, and no Agent-database writes.
6. Runtime identity is bounded and ephemeral. Distro/user/session/runtime facts are not persisted as long-lived user configuration.
7. A schema/runtime mismatch degrades to non-authoritative/UNKNOWN/last-good behavior instead of guessing.

## Acceptance boundary

The 4.0.1 automated acceptance covers byte-compile, the full Windows unit suite, fresh/stale/tombstone discovery semantics, direct-argv read-only remote transport, state isolation, global session/plane bounds, and the existing Monitor/Desktop-source/Presentation/UI architecture benchmarks. It does **not** claim that GitHub-hosted CI is a real user's ZCode + WSL Remote Development machine. `tools/desktop_source_probe.py` exists for the final environment-specific read-only smoke check without exposing prompts, transcripts, tool arguments, credentials, or other sensitive payloads.
