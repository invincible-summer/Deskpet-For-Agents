# DeskPet 4.4.0 验收记录

> 基线：`main@9635e1b`（4.3.1）→ 4.4.0
> 计划：plan1.md（watcher 生命周期收口）+ plan2.md（Codex/ZCode Desktop 被动监听）
> 日期：2026-09-12
> 本文区分三类证据：**[自动]** 单元/基准测试；**[实机]** 本机只读探针/真实数据验证；**[人工待验]** 需要真实桌面 GUI 场景、无法忠实自动化的项。

## 1. plan1（Phase 0，commit `dd48b3c`）

| 验收项 | 证据 | 状态 |
|---|---|---|
| AC-PI-01 error→IDLE 不反弹 | `test_error_ttl_expiry_falls_to_idle_never_working` | ✅ 自动 |
| AC-PI-02 error→新 user turn | `test_new_user_turn_invalidates_stale_error` | ✅ 自动 |
| AC-PI-03 done→新 user turn | `test_new_user_turn_invalidates_stale_done` | ✅ 自动 |
| AC-PI-04 image-only user 建立 turn | `test_image_only_user_message_starts_turn` | ✅ 自动 |
| AC-PI-05 top-level error 同语义 | `test_toplevel_error_same_lifecycle_as_assistant_error` | ✅ 自动 |
| AC-CLAUDE-01/02 + tool_result 不伪造 Goal | 3 个新 Claude 测试 | ✅ 自动 |
| AC-KIMI-01/02/03（approval 不误伤） | 3 个新 Kimi 测试 | ✅ 自动 |
| AC-CODEX-01 对照回归 | `test_error_and_abort_lifecycle`（Codex 生产代码未改） | ✅ 自动 |
| 资源/架构不变量 | focused diff 仅 3 watcher + 测试；三 benchmark 全绿 | ✅ 自动 |

## 2. plan2 自动化验收

| 验收项 | 证据 |
|---|---|
| 终端 key 与 4.3.1 完全一致 | `test_terminal_key_unchanged_from_v431` |
| Desktop runtime key 含宿主 incarnation | `test_desktop_instance_and_host_key_shape`、`test_host_restart_changes_desktop_runtime_key` 语义（同一 PID 换 token → 新 key） |
| Codex GUI/helper 不再冒充 CLI | `test_codex_gui_host_not_emitted_as_terminal_agent` 等 6 个 inventory 测试 |
| 只读 SQLite 合同（mode=ro/query_only/40ms busy/WAL/指纹/无写路径） | `ReadOnlySqliteTests` 7 项 |
| 1 host → N sessions / host 退出原子级联 / source 失败保留 last good / claim 去重 / DESKTOP 不进 terminal service | `MonitorDesktopLifecycleTests` 9 项 |
| Codex Desktop source（过滤/admission/lease/path containment/WAITING 不合成/locked/corrupt/指纹） | `test_codex_desktop` 21 项 |
| ZCode Desktop source（root catalog/subagent 归并/WAITING 行级映射/resolve 退场/DONE-ERROR 窗口/降级） | `test_zcode_desktop` 19 项 |
| Desktop 激活 fail-closed 链 / surface dispatch / 无注入调用面 | `test_desktop_activation` 14 项 |
| config zcode 归一化（CONFIG_VERSION 仍为 5） | `test_zcode_kind_normalized_for_old_config` |
| 隐私：desktop 轮询不触发持久化 | `test_desktop_observation_never_persists_runtime_identity`（AC-PRIVACY-01 合成面） |
| 资源预算（plan2 §12/§17） | `benchmark_desktop_sources`：5000 ticks、1+1 host × 8 sessions，refresh 396/350 ≤ 预算 453/403（与变更/安全刷新成正比，绝非 ticks×sessions）；无线程增长；句柄 171→175；busy 5 次全部保留 last good |
| 既有预算不放宽 | benchmark_monitor / presentation / ui_architecture 三套阈值未动，全绿 |

## 3. plan2 实机验收（本机，2026-09-12）

数据来源：`tools/desktop_source_probe.py`（只读、输出脱敏），快照存 `.research/probe-snapshot.json`（本地，不入库）。

| 场景 | 结论 |
|---|---|
| AC-CODEX-DESKTOP-04 进程分类 | ✅ 实测：`ChatGPT.exe`（WindowsApps `OpenAI.Codex_*` 包）+ 其 `codex.exe` runtime 子进程被折叠为一个 Codex DesktopHost（helper 收敛）；`ZCode.exe` 主进程 + 22 个 `--type=`/`zcode.cjs app-server`/`__zcode-plugin-host` helper 折叠为一个 ZCode DesktopHost；终端 ancestry 下的 CLI 仍正常成 target |
| Codex schema capability | ✅ 实测 `state_5.sqlite`：threads 表含全部 required/preferred 列的 capability 探测路径；本机 threads 表为空（无桌面 thread 使用记录），`originator` 列本版本不存在——capability 降级路径按设计工作 |
| ZCode schema capability | ✅ 实测 `db.sqlite`：`task_type` 枚举仅 `interactive`(root)/`subagent_child`；`tool_usage.approval_status='requested'+status='running'` 行存在（WAITING 精确映射的真实行形态）；时间戳为毫秒 epoch；`session_input` 无可靠 pending-input 状态 → 按计划不投影 INPUT |
| Side Conversation（AC-ZC-SIDE-01） | ✅ 结论：当前版本 DB/进程形态没有可被动归属的稳定 side-conversation 身份 → v4.4 不单独显示（AC-ZC-SIDE-02 条件项不满足，README 已如实说明），持久 task 支持不受影响 |
| AC-ZCODE-04 subagent | ✅ schema 证实 `subagent_child` + `parent_id` 可精确排除/归并（合成测试覆盖归并路径） |
| AC-PRIVACY-01 | ✅ 探针输出核查：thread/session id 哈希化、无 prompt/命令/路径参数值、App Server socket 只报存在性未连接 |

## 4. 需真实 GUI 人工验证的项（发布后跟踪，不影响代码合入）

以下场景需要真实桌面应用内运行多个会话，无法忠实自动化；自动化面已由合成 fixture 等价覆盖：

1. **AC-CODEX-DESKTOP-01/02/03**：同一 Codex Desktop 内 3 个并行 thread 的独立呈现、与 CLI 并存/去重接管、隐藏 thread 审批诚实降级（本机 threads 表为空，无真实桌面 thread 可观测）。
   步骤：Codex Desktop 内开 3 个 thread 同时工作 → 桌宠/仪表盘应出现 3 个独立目标；同时终端跑 `codex` CLI resume 其中一个 thread → 该 thread 只出现一次（terminal 表面）；CLI 退出 → 桌面重新接管。
2. **AC-ZCODE-01/02/03**：ZCode 内 3 个并行 task、B 进入 permission 确认后切走仍保持 WAITING、resolve 后离开；关闭窗口缩托盘后监控继续。
3. **AC-CODEX-DESKTOP-06 / AC-RESOURCE-01 的 30 分钟观察面**：运行 30 分钟确认无 app-server 连接、无 Agent 数据写入、空闲 SQL 接近 safety-refresh 次数（自动化基准已覆盖合成面）。
4. **Desktop 激活的 foreground policy 接受度**（与终端激活同类的本机 manual acceptance）。

## 5. CI

推送后 GitHub Actions（windows-latest + Python 3.12）必须绿：compileall + 全部单元测试 + 4 套 blocking benchmark。CI 红则不得宣称 4.4.0 完成（README 发布合同）。
