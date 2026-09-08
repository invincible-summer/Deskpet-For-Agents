# DeskPet V3 — 被动 Agent 观察桌宠

一只常驻桌面的自定义桌宠，**被动观察**你已经在 Windows / WSL 终端里启动的 AI 编码 Agent（**Codex / Claude Code / Kimi / pi**），自动识别 Agent、项目、WSL 发行版、会话与终端，实时展示 Goal、Mode（Plan/Default…）、Thinking / Reading / Coding / Testing / Waiting Approval 等状态，并映射到桌宠动画和气泡。

V3 不创建、不托管、不控制任何 Agent：不配置 hooks、不注入进程、不发送键盘事件、不自动审批。

```
Process tells us WHO.          /proc + psutil（含 cwd/tty/uid/启动 token）
Session data tells us WHAT.    各 Agent 自己落盘的 JSONL（增量只读 tail）
Terminal UIA tells us          Windows Terminal 官方 UI Automation 接口
  WHAT THE USER IS ASKED.      （Notification 事件 + 当前可见区域）
StateReducer combines them.    ERROR > WAITING > INPUT > WORKING > DONE > IDLE > UNKNOWN
DeskPet only observes.         桌宠动画 + 气泡 + 仪表盘
```

## 正常使用流程

```
打开 Windows Terminal → 进入 Windows/WSL → 自己执行 codex / claude / kimi
        ↓ DeskPet 自动发现、自动绑定会话、自动关联终端
桌宠动画 + 气泡（Codex · Plan · 编码中 / 目标 / 当前活动）
```

等待审批时气泡提示"请在终端处理"；双击桌宠唤起该 Agent 所在终端。

## 功能

- **五状态动画**：`walk` 工作中 ｜ `attack` 下达指令 ｜ `die` 等待审批 ｜ `special` 任务完成（×3）｜ `sleep` 空闲
- **语义化状态气泡**：`Agent · Mode · Phase` + Goal（≤120 字）+ 当前活动摘要（≤160 字，本地规则压缩，不调用 LLM）
- **等待审批检测**：Kimi 来自 wire `ApprovalRequest`（精确）；Codex/Claude 来自 Windows Terminal UIA 当前可见审批 UI（高置信 + 1.5s TTL 复检）——**静默永远不被推断为等待审批**
- **多 Agent**：自动跟随（WAITING > INPUT > ERROR > WORKING …，工作中粘性），或手动钉住直到该 Agent 退出
- **仪表盘 V3**：`当前 ★ | Agent | 项目 | 环境/终端 | Mode | 状态 | 活动`；PID 等技术细节在"详情"高级诊断
- **双击桌宠**：唤起当前 Agent 的终端（公共 Win32 API 置顶）
- **系统集成**：托盘图标、开机自启、隐藏、换肤、缩放、锁定动画
- **隐私**：`/proc/<pid>/environ` 只在 WSL 内部按 allowlist（`WT_SESSION`/`CODEX_HOME` 等 9 项）过滤后才进入 Python；终端文本只在内存、绝不落盘

## 快速开始

```bat
:: 1) 创建环境（Miniconda）
D:\miniconda3\Scripts\conda.exe create -n deskpet python=3.12 -y
D:\miniconda3\envs\deskpet\python.exe -m pip install -r requirements.txt

:: 2) 启动
启动桌宠.bat        （pythonw 隐藏控制台）
:: 或
D:\miniconda3\envs\deskpet\python.exe main.py
```

旧 V2 配置自动迁移到 `config_version: 3`（删除连接方式/受控会话/按键/自动审批配置，清空旧绑定）。

依赖已拆分：`requirements-core.txt`（psutil/Pillow/comtypes，常驻监控路径）与 `requirements-convert.txt`（imageio-ffmpeg/numpy/scipy，仅皮肤转换期使用，转换在独立子进程完成）；完整安装仍是 `pip install -r requirements.txt`。

## 三路观察（安全、无 hooks）

1. **进程探测**（`agents/discovery.py`）：psutil 扫 Windows；WSL 每发行版每周期 1×`ps` + 1×匹配 PID 批量 metadata（`/proc/<pid>/cwd`、`stat` 启动 ticks=进程 token、allowlisted environ、`getent passwd` 解析 HOME，不再枚举 `/home/*`）
2. **会话文件 tail**（`agents/*.py`）：增量只读，容忍残行/轮转/超长行

| Agent | 数据根（env 覆盖） | 结构化状态 |
|---|---|---|
| Codex | `$CODEX_HOME`（默认 `~/.codex`） | `task_started.collaboration_mode_kind` → Plan/Default（EXACT）；user_message → Goal |
| Claude Code | `$CLAUDE_CONFIG_DIR`（默认 `~/.claude`） | `permission-mode` → 六种模式；`sessions/<pid>.json` 为强 hint（/clear 后自动切换新 transcript） |
| Kimi | `$KIMI_CODE_HOME`（默认 `~/.kimi-code`，legacy `~/.kimi` 兜底） | `session_index.jsonl` 按 cwd 精确定位；`state.json.lastPrompt` + `prompt.accepted` → Goal；`plan_mode.enter/exit`（EXACT）；wire `ApprovalRequest`（EXACT，兜底 SDK 命名） |
| pi | `~/.pi` | assistant/toolCall 生命周期 |

3. **终端 UIA**（`agents/terminal_uia.py`）：独立 MTA 线程（comtypes `CUIAutomation8`/`IUIAutomation5`），订阅 TermControl 的 Notification（2022 起携带新增文本）+ TextChanged（0.15s debounce 的有界审批 fallback）+ 窗口级 StructureChanged（pane 开合立即重发现，20s 周期仅为兜底）；弱触发词命中才读 `GetVisibleRanges()` 当前可见区域；审批识别要求**标题模式 + 选项结构同时出现**且识别器种类与绑定 Agent 一致；内存边界：delta≤2048 / ring≤8192 / pane≤16 / 事件队列≤256 / UIA 命令队列≤32 / 可见读取全局≤6/s（单 pane≥0.5s 间隔）。

## 终端关联的置信度（诚实原则）

Windows Terminal 没有 `WT_SESSION → pane` 公开接口：

- **Windows 原生 Agent**：PID 祖先链 → 唯一窗口 → `CONFIRMED`
- **WSL Agent**：标题/cwd/distro 评分（kind+3 / cwd+2 / user@+1 / distro+1），**互相唯一匹配**（Agent 对 pane、pane 对 Agent 双向唯一 top-1 且分差足够）→ `HIGH`；"只有一个 pane + 弱提示"不再自动 HIGH；否则 `AMBIGUOUS` / `NONE`
- 只有 `CONFIRMED/HIGH` 才把终端审批观察归属到该 Agent；`AMBIGUOUS` 时仪表盘显示 ⚠ 并提供"高级：关联当前 Pane"修复入口（仅运行期有效）；详情页展示绑定依据与 `score / 次佳` 分数
- UIA 不可用时正常降级：Goal/Mode/Phase 来自会话文件，仅 Codex/Claude 的"等待审批"无法补足（仪表盘提示"终端交互状态不可读"）

## 稳定性设计

- **线程架构**：Tk UI ｜ Monitor Core（0.5s）｜ ProcessProbe worker（3s，single-slot）｜ UIA MTA —— WSL 卡顿不卡气泡
- **进程身份**：key 含启动 token（`wsl:Ubuntu|codex|4812|<ticks>`），PID 复用不继承旧绑定；wrapper/runtime 折叠（npm shim → node 只保留最深 runtime，不跨 kind 折叠）；`/proc` ticks 缺失时用稳定 fallback 代次 token，绝不退化成裸 PID；消失宽限 15s
- **来源隔离与三态生命周期**：探测健康按真实 source（`windows` / `wsl:Ubuntu` / `wsl:Debian`…）判定，一个 distro 扫描失败不污染其他来源的实例与"状态可能延迟"标记。WSL source 有三种内部语义（V3.1.1）：
  1. **healthy + instances** —— 发行版运行且 Agent 被发现；
  2. **healthy + empty** —— 已权威确认当前发行版没有 Agent，或发行版已停止（`wsl --list --running --quiet` 成功且输出为空即是权威空结果）；旧实例经 `gone_grace_sec`（默认 15s）后清除，同时清掉该 distro 的进程缓存与 fallback 代次 token——重启后 Linux PID 从小整数再来也不会继承旧绑定；
  3. **unhealthy** —— WSL 枚举/ps 读取失败：DeskPet 保留上一轮缓存并显示"状态可能延迟"，绝不误判退出（无法读取 ≠ 已经不存在）。
  停止检测的最坏延迟约为 15s 发行版清单缓存 + 3s 调度 + 15s 消失宽限 ≈ 33s，这是当前轻量设计的既定取舍。
- **状态语义**：已知 active turn → 无限保持 WORKING；仅活动证据 → 10s 宽限后回 UNKNOWN（不伪造）；DONE 展示 8s；IDLE 只在明确见过 turn 结束后出现；**泛化终端活动（pane 有文本变化）永远不能推翻结构化 Session 的 DONE/IDLE/ERROR/INPUT**
- **Status/Phase/Mode 正交**：Mode 是独立维度（Plan/Default/UNKNOWN+原始值），终端 WAITING 成为状态胜者时无权擦除 Session 已解析的 Mode——`WAITING + APPROVAL + PLAN` 是合法且必要的最终状态；优先级为 Session 结构化 Mode → 胜者明确携带的 Mode → NONE
- **会话解析**：绑定用互相唯一匹配（source/session_id/cwd/started_at 评分，结果与实例遍历顺序无关），同分竞争保持未绑定；late-start 每 15s 无窗 fallback（最近 12 候选）；目录重扫有绑定时降为 15s
- **兼容性诊断**：会话解析器按已知记录类型集合判定 `OK / PARTIAL / UNKNOWN`，上游格式变化会在仪表盘显示"未知记录"而不是静默失败；Mode 出现未知原始值时显示 `Unknown（原始值：…）`

## 项目结构

```
main.py                 入口（DPI 感知、单实例互斥）
pet/                    UI：app/dashboard/bubble/labels/petwindow/animator/skins/tray/config
agents/
  models.py             Status/Phase/Mode/Observation/AgentInstance/TerminalBinding/AgentTarget
  state.py              StateReducer（状态融合；语义证据 > 泛化终端活动）
  matching.py           互相唯一匹配（session/pane 绑定共用，顺序无关）
  discovery.py          Windows + WSL ProcessProbe（三层探测、canonicalization、env allowlist）
  paths.py              数据根/wsl_unc 安全转换/Kimi 索引/Claude PID registry
  base.py               watcher 基座（互相唯一绑定、late-start fallback、parser 诊断）
  codex.py claude.py kimi.py pi.py
  terminal_uia.py       UIA 观察器 + 审批识别器 + TerminalResolver（订阅生命周期有界）
  monitor.py            ProcessProbeWorker + Monitor Core + AgentTarget API
  tailer.py summarize.py
actions/winkeys.py      仅终端唤起（公共 Win32 + HWND 属主 PID/窗口类一致性验证，fail-closed：任何一步无法证明身份即拒绝并触发重识别；无任何键盘注入）
tools/convert.py        素材→透明GIF 管线
tests/                  单元/隐私/UIA/匹配/基准/实机探针/回归
.github/workflows/      CI（windows-latest：compileall + unittest + benchmark）
```

## 常用配置（config.json，v3）

```jsonc
{
  "monitor": {
    "agents": { "claude": true, "codex": true, "kimi": true, "pi": true },
    "windows_enabled": true, "wsl_enabled": true,
    "windows_scan_sec": 3.0, "wsl_scan_sec": 3.0,
    "file_poll_sec": 0.5, "session_scan_sec": 3.0,
    "gone_grace_sec": 15.0, "activity_grace_sec": 10.0,
    "terminal_observer": true, "pinned": ""
  },
  "privacy": {
    "terminal_text_to_disk": false, "session_text_to_disk": false,
    "wsl_root_metadata_fallback": false,
    "goal_max_chars": 120, "summary_max_chars": 160
  }
}
```

`privacy.wsl_root_metadata_fallback` 默认关闭：默认绝不使用 WSL root 读取进程 metadata（Agent 仍会被发现，会话可能显示未解析）；仅在仪表盘显式开启后允许一次 root 补读（只读 cwd/启动 token/uid/HOME/allowlist env）。

## 测试

```bat
D:\miniconda3\envs\deskpet\python.exe -m unittest discover tests -p "test_*.py"  # 全部单元测试（170+）
D:\miniconda3\envs\deskpet\python.exe tests\benchmark_monitor.py --ticks 5000 --report benchmark-report.json    # 合成基准（队列/预算/churn 上限）
D:\miniconda3\envs\deskpet\python.exe tests\uia_probe.py                         # UIA 实机冒烟（--verbose-text 才打印原文）
D:\miniconda3\envs\deskpet\python.exe -X utf8 tests\regression.py                # 位置/气泡/缩放/托盘/自启
D:\miniconda3\envs\deskpet\python.exe -X utf8 tests\replay_real.py               # 真实会话数据回放
```

CI（`.github/workflows/test.yml`）：windows-latest + Python 3.12，运行 compileall + 全部单元测试 + benchmark 5000 ticks（`PYTHONUTF8=1`，benchmark 报告以 artifact 上传）；真实 UIA 验收属于本机 manual acceptance。**Release acceptance requires GitHub Actions green**：workflow conclusion=success 是发布验收的必要条件，CI 红期间不标记版本完成。

## 已知边界（如实说明）

- Codex 的审批事件明确不持久化到 rollout（官方 transient 策略），因此 Codex/Claude 的"等待审批"只能来自终端 UIA 可见区域；若审批 pane 无法唯一关联到 Agent（多 pane/后台 tab），在"不 hooks、不控制 Agent"的约束下没有第三条可靠信息源——此时显示 UNKNOWN/工作中而不是猜（plan §55 物理边界）
- Claude Code 上游存在"活跃 session transcript 不实时写出"的回归 → 终端活动观察可补充 WORKING 证据，但不伪造具体 Phase
- Codex 桌面版不写 rollout → 只能检测进程存活（UNKNOWN）
- 自定义桌宠素材版权自负；`assets/pets/*`、`assets/cache/`、`config.json` 不入 git

## V3 不变量（任何实现不得违反）

```
1. 不启动 Agent        7. 不把静默解释为审批     13. 不确定 Agent↔pane 时不乱绑定
2. 不修改 Agent        8. 不扫描用户整个 HOME    14. UI 只暴露 AgentTarget
3. 不配置 hooks        9. 不持久化终端原文       （PID/JSONL/HWND 只在高级诊断）
4. 不使用 SendInput   10. 不持久化完整 environ
5. 不向终端写输入      11. 终端文本只做匹配归类
6. 不自动审批          12. （见上）
```
