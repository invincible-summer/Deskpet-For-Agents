# DeskPet V3.1.1 Core Hardening Closure 实施计划

基线 `9c3053d`（v3 complete，CI 在 Monitor benchmark 步骤红）。目标：按 V3.1.1 计划书收口 4 个闭环问题 + 2 个一致性问题。**不扩展任何功能**：不重写 UIA backend、不改 parser health 语义、不动 `privacy.wsl_root_metadata_fallback=false` 默认、不加任何控制/hooks/注入/审批能力、不放宽任何阈值或上限。所有不确定状态继续诚实选 UNKNOWN/AMBIGUOUS/stale。

已核实全部问题属实：`discovery.py:501-508` 空 running 列表返回缓存（停止后 Agent 永久残留）、`state.py:134` mode 只随 winner 赋值（terminal 胜出丢 session mode）、`winkeys.py:63-74` 三处 fail-open、`matching.py:103` identity 比较、`canonicalize_agent_processes:83` launchers 跨 kind 泄漏、benchmark 无 --report、CI 无 artifact。

## Phase A — WSL Running/Stopped/Failed 三态分离（P0）

**agents/discovery.py**
1. 新增 frozen dataclass `DistroInventory(running: tuple[str,...], authoritative: bool, error: str = "")`。
2. `__init__` 新增 `_known_distros: set[str]`、`_inventory`、`_inventory_ts`（保留 `_distros/_distros_ts` 仅作双命令失败时的旧名单 fallback）。
3. `_list_running_distros()` 改返回 `DistroInventory`：
   - 15s 缓存命中且缓存为 authoritative → 原样返回（最多 15s 旧，仍权威）；失败结果不缓存，下轮即重试。
   - 主路径 `--list --running --quiet` rc==0：**即使解析为空也返回 `authoritative=True`**（成功空输出 = 已确认无 Running distro）；仅 rc!=0 或"输出非空但解析为空"（疑似表头）才转 fallback。
   - fallback `wsl -l -v`：失败抛异常、成功返回解析列表（可为空 = 全部 Stopped）→ `authoritative=True`。
   - 双失败 → `DistroInventory(tuple(self._distros), False, error)`，不更新 `_known_distros`。
4. `scan()` 重构：
   - inventory 非 authoritative → 不更新 `_known_distros`、不发 tombstone；`_cache` 各 distro 输出缓存实例 + `healthy=False`（无法读取 ≠ 已经不存在），直接返回；`last_ok=False`。
   - authoritative → `running=set(inv.running)`，`_known_distros.update(running)`；对 `stopped=_known_distros-running` **每轮持续**输出 `instances_by_source[f"wsl:{d}"]=[]` + `healthy=True`（tombstone 常驻，保证 gone timer 可靠推进），并清理 `_cache.pop(d)`、`_distro_ok.pop(d)`、`_fallback` 中该 distro 全部 key（重启后 Linux PID 从小整数重来也不继承旧代次 token）。
   - running 中的 distro 走现有 ps/metadata 路径不变（ps 失败 = healthy False + 缓存实例）。
   - `last_ok = authoritative and 无 ps 失败`；`_cache/_distro_ok` 写入统一持锁。
5. `canonicalize_agent_processes` launchers 收口：过滤从 `p in wrappers`（全局集合）改为 `p in wrappers and by_pid[p].kind == by_pid[pid].kind`，杜绝把 Claude 祖先显示成 Codex launcher。

**agents/monitor.py**
6. `ProcessProbeWorker._tick` 删除"缺 key 即 pop"（probe 现为所有 known distro 持续输出 source 键）；保留 `wsl_enabled=False` 全清与 catastrophic 异常 `_ok=False`（`_snapshot` 不动）。
7. `_merge_instances` 不改（已验证 tombstone 空+healthy=True → authoritative → gone_since → 15s grace 后删除）。

**tests/test_discovery.py**（现有 MultiDistroScan/RootFallback 的 patch 适配 DistroInventory 返回值）
新增：`test_successful_empty_running_list_is_authoritative`、`test_one_of_two_distros_stopped_emits_empty_tombstone`（含持续输出）、`test_inventory_failure_keeps_cache_unhealthy`、`test_stopped_distro_clears_fallback_identities`（停止→同 PID 重启→新 token→新 key）、`test_stopped_distro_clears_process_cache`、`test_launcher_pids_never_cross_agent_kind`（Claude 100→非匹配 200→Codex 300：canonical={100,300}，launchers.get(300)==()）。

**tests/test_monitoring.py**
新增：`test_authoritative_empty_source_starts_gone_grace`、`test_authoritative_empty_source_removes_after_grace`、`test_failed_source_never_advances_gone_grace`、`test_one_distro_stopped_does_not_affect_other`、`test_one_distro_failed_does_not_affect_other`。

## Phase B — Status/Phase/Mode 正交（P0/P1）

**agents/state.py**
8. session 复制段在 winner 判定前：`snap.mode = session.mode`（+ 保留 mode_raw 复制）。
9. `if winner.mode:` → `if winner.mode is not Mode.NONE:`（显式枚举比较），命中时同步覆盖 `snap.mode_raw`。优先级：Session structured Mode → winner explicit Mode → NONE。WAITING 不再擦 PLAN / UNKNOWN("delegate") / DEFAULT。证据优先级与 generic-activity 规则一概不动。

**tests/test_state.py** 新增：`test_terminal_waiting_preserves_session_plan_mode`（WORKING/PLAN + terminal WAITING → WAITING/APPROVAL/PLAN）、`test_terminal_waiting_preserves_unknown_mode_raw`、`test_terminal_waiting_preserves_default_mode`、`test_explicit_winner_mode_can_override_none`。

## Phase C — HWND 完整 fail-closed（P1）

**actions/winkeys.py**
10. 补 restype：`IsWindow→wt.BOOL`、`GetWindowThreadProcessId→wt.DWORD`、`GetClassNameW→ctypes.c_int`。
11. 重写 `validate_terminal_window()`：无 user32/hwnd=0/IsWindow 假 → False；expected `window_pid<=0` → False；`GetWindowThreadProcessId` 返回 0（API 失败）→ False；`actual_pid<=0` → False（PID=0 是未完成验证，不是匹配）；不等 → False；expected `window_class` 空 → False；`GetClassNameW` 返回 <=0 → False；类名不等 → False。不加 ancestor/child 推测（不一致 → reject → rediscover）。

**tests/test_winkeys.py**：`_FakeUser32` 扩展失败注入；新增 `test_get_window_thread_process_id_failure_rejected`（返回 0 且 out 变量残留旧期望值仍 False）、`test_zero_owner_pid_rejected`、`test_get_class_name_failure_rejected`、`test_missing_expected_window_pid_rejected`、`test_missing_expected_window_class_rejected`。

## Phase D — matching 值语义（P2）

**agents/matching.py**
12. `if r_top is not l or not r_unique:` → `if r_top != l or not r_unique:`。
13. `MatchDecision.left/right` 类型改 `Hashable`/`Hashable|None`（接口契约 = hashable key 相等语义）。

**tests/test_matching.py** 新增 `test_mutual_match_uses_equality_not_identity`（自定义 Key 类 \_\_eq\_\_/\_\_hash\_\_，equal-but-not-identical 双向匹配）。

## Phase E — benchmark/CI 闭环

**tests/benchmark_monitor.py**
14. 新增 `--report PATH`：写 JSON（ticks/elapsed/ticks_per_sec/queue_high/budget_high/dirty_high/panes/visible_reads/fallback_reads/fallback_rate/dropped/events/checks{name:bool}），**失败也写出再 return 1**；不传时行为不变；argv 解析通用化。
15. 不放宽阈值、不删检查、不加 continue-on-error。（本地断言全部走注入模拟时钟、确定性成立；CI 红的真实原因待 Actions 日志/report 定位后修真实问题。）

**.github/workflows/test.yml**
16. benchmark 步骤加 `--report benchmark-report.json`；新增 `if: always()` 的 `actions/upload-artifact@v4`（name: monitor-benchmark）。保持 windows-latest + py3.12 + blocking。

## 文档
17. README.md：来源隔离改为三态语义（healthy+instances / healthy+empty=权威确认无 Agent 或已停止，经 gone_grace 清除 / unhealthy=枚举或 ps 失败，保留缓存显示"状态可能延迟"不误判退出）；明确成功空输出 = authoritative empty；停止检测最坏 ≈15s inventory 缓存 + 3s 调度 + 15s grace ≈33s；CI 措辞改为 "Release acceptance requires GitHub Actions green"。
18. SourceLink.md：补 WSL basic commands（--running 只列运行中、--quiet 只列名字）、GetWindowThreadProcessId（失败/无效 HWND 返回 0）、GetClassNameW（失败返回 0）、UIA Threading（MTA + 同线程 Add/Remove）、GitHub workflow run logs 五条官方依据。

## 验证与提交
19. 本地全量：`compileall agents pet actions tools` → `unittest discover`（150→约 170）→ `benchmark --ticks 5000`（含 --report 冒烟）→ `tray_test.py`/`regression.py`/`replay_real.py` → `uia_probe.py`（默认不打印原文）→ invariant grep（无可执行输入注入路径）。
20. 实机验收（你已 `wsl --shutdown`）：**Case 3 真机验证**——新代码下探测确认 running 列表为空 → authoritative empty、无幽灵 Agent、无崩溃；再 `wsl -d Ubuntu` 拉起单个发行版验证 inventory 正常路径（不启动任何 Agent）；UNC 会话读取视恢复情况跑 replay_real。HWND 实机（正常 raise / 关闭后拒绝）以单测 + uia_probe 冒烟覆盖。
21. 提交按计划书 §51 拆三个：`fix: make WSL stopped sources authoritative` → `fix: preserve mode and fail closed on terminal identity` → `test: close v3.1 core hardening regressions and CI`（body 注明 V3.1.1 Core Hardening closure）；不改写 9c3053d；CI 红期间不标 "v3 complete"。
22. **push 到 origin/main** 触发 Actions；若 benchmark 仍红，读取上传的 benchmark-report.json（checks 字段）定位具体不变量，修真实问题后追加 commit 再 push，直到 workflow conclusion=success。