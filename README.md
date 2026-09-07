# DeskPet — Agent 监听桌宠

一只常驻桌面的自定义桌宠，实时监听终端里正在运行的 AI 编码 Agent（**Claude Code / Codex / Kimi CLI / pi**，Windows 原生与 WSL 均可，可多选多监听），用头顶气泡汇报状态，支持**在气泡里一键批准/拒绝审批请求**、**自动批复**、**双击唤起 Agent 终端置顶**。

```
进程发现 ──► 会话文件定位 ──► 增量 tail 解析 ──► 本地摘要 ──► 动画 + 气泡
(Windows+WSL)  (~/.claude 等)   (只读，无 hooks)   (规则压缩)   (批复按钮)
```

## 功能

- **五状态动画**：`walk` 工作中 ｜ `attack` 下达指令/批复发出 ｜ `die` 等待批复 ｜ `special` 任务完成（×3）｜ `sleep` 无任务
- **状态气泡**：显示 Agent 类型、当前模式（如 Codex 的审批策略/沙箱、Claude 的权限模式、Kimi 的 Plan 模式与上下文占用）、任务标题与最新活动的**本地摘要**（规则压缩截断，不调用 LLM，不搬运原始输出）
- **一键批复**：气泡内 [批准]/[拒绝] 按钮 → 自动定位 Agent 终端窗口 → 置前发送批准键（字符经 UNICODE 注入，不受中文输入法影响）→ 可选自动还原焦点
- **自动批复**：设置开启后对所有等待批复的请求自动发送批准键（带冷却与焦点还原，可在菜单/仪表盘开关）
- **双击桌宠**：把当前 Agent 的终端窗口强制置顶唤回
- **多 Agent**：多实例同时监听，气泡轮播；仪表盘勾选绑定
- **系统集成**：托盘图标（左键显隐/右键菜单）、开机自启动、暂时隐藏桌宠
- **位置稳定**：窗口位置由"锚点"（桌宠底部中心）反推，气泡变化/换肤/缩放不漂移
- **低内存**：常驻约 **50MB**（tkinter 原生 GIF 解码、帧缓存 LRU、素材转换在独立子进程完成）

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

## 自定义桌宠（素材用户自备）

桌宠动画由**五个素材文件**驱动，按状态命名：

| 文件名 | 状态 | 播放方式 |
|---|---|---|
| `walk` | Agent 工作中 | 循环 |
| `attack` | 指令/批复发出 | 单次 |
| `die` | 等待批复/中断 | 循环 |
| `special` | 任务完成庆祝 | 自动 ×3 |
| `sleep` | 无任务 | 循环 |

**放置方式**：在 `assets/pets/<你的桌宠名>/` 下放入五个素材（`.webm` / `.mp4` / `.gif`），可选 `manifest.json`：

```json
{
  "name": "我的桌宠",
  "title": "显示名称",
  "animations": {
    "walk": {"loop": true}, "attack": {"loop": false}, "die": {"loop": false},
    "special": {"loop": false, "repeat": 3}, "sleep": {"loop": true}
  }
}
```

**自动链路**：选择皮肤后自动完成 背景抠除（纯色背景连通域抠像，不伤主体深色）→ 内容裁剪对齐 → 缩放（0.5x~2x 按需生成缓存）→ 生成带透明索引的 GIF → 热切换显示。也可通过 仪表盘 → 皮肤 → 导入 从任意文件夹导入。

> ⚠️ 素材版权自负：请使用你拥有权利或已获授权的素材。`.gitignore` 已排除 `assets/pets/*`（素材）、`assets/cache/`（转换缓存）、`assets/icon.ico` 与 `config.json`，均不会进入 git 仓库。

## 监听原理（安全、无 hooks）

不注入 Agent 进程、不使用 hooks，只用两类只读信息：

1. **进程列表**：psutil 扫 Windows 进程；`wsl.exe ps` 扫 WSL 发行版内进程
2. **会话文件 tail**：各 Agent 自己实时落盘的 JSONL（增量读、容忍残行）

| Agent | 会话文件 | 状态判定 | 审批复扑键（默认，可配） |
|---|---|---|---|
| Claude Code | `~/.claude/projects/<路径改写>/<uuid>.jsonl` | assistant/tool_use/turn_duration；permission-mode → 模式 | 批准 `Enter`（默认 Yes）｜拒绝 `Esc` |
| Codex | `~/.codex/sessions/年/月/日/rollout-*.jsonl` | task_started/complete、CommandExecution；turn_context → 审批策略/沙箱 | 批准 `y`｜拒绝 `Esc` |
| Kimi CLI | `~/.kimi/sessions/<md5(cwd)>/<uuid>/wire.jsonl` | TurnBegin/End、ToolCall；StatusUpdate → Plan/上下文 | 批准 `Enter`｜拒绝 `Esc` |
| pi | `~/.pi/agent/sessions/<编码cwd>/*.jsonl` | assistant/toolCall（设计上无审批） | — |

WSL 内 Agent 的会话文件经 `\\wsl.localhost\<发行版>\home\...` 读取；批复/唤起复用其宿主 Windows Terminal 窗口。

## 批复与自动批复

- **批复与自动批复**：点击气泡按钮；发送后默认把焦点还给原先窗口（`approve_restore_focus` 可关）
- **自动批复**：⚙设置 / 仪表盘勾选"自动批复所有请求"。对所有等待批复的 Agent 自动发送批准键，每实例 4s 冷却，避免窗口找不到时反复尝试
- **彻底免审批的官方途径**（对新建会话生效，与桌宠互补）：
  - Codex：`~/.codex/config.toml` 里 `approval_policy = "never"` + `sandbox_mode = "workspace-write"`（或 CLI `--full-auto`），参考 [Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)
  - Claude Code：`--permission-mode acceptEdits`（自动接受编辑）或 `--dangerously-skip-permissions`（完全跳过，有风险），参考 [权限模式文档](https://code.claude.com/docs/en/permission-modes)
  - Kimi CLI：`kimi --yolo`（自动批准全部工具调用）
  - pi：设计上无审批弹窗

## 稳定性设计

- **主绑定自动选择**：发现 Agent 时自动选一个作为主绑定（等待批复 > 工作中 > 最新启动），选中后保持粘性；多个 Agent 时可 右键 → 监听目标 或 仪表盘 双击行手动钉住
- **消失宽限期**（`monitor.gone_grace_sec`，默认 45s）：进程从扫描中短暂消失（扫描抖动/WSL 卡顿）不会立刻判定退出，状态不闪跳
- **工作保持**（`monitor.working_hold_sec`，默认 90s）：Agent 活动后即使会话文件暂时静默（长思考/长命令），仍保持"工作中"，直到确认回合结束才转睡眠
- **固定尺寸气泡**：固定宽×两行（`bubble.width` / `bubble.max_lines`），不随内容伸缩；显示当前任务/最新摘要，不显示 Agent 名称前缀、不轮播
- **锁定动画**：外观设置可固定展示 walk/attack/die/special/sleep 之一（如演示/截图用）
- **定时清理**：每 10 分钟自动清理内存日志环、转换临时目录、已删除皮肤的缓存与多余尺寸缓存

## 已知限制（如实说明）

- **Codex 的审批请求不写入 rollout 文件**（官方只走内存事件流，已在 0.147~0.153.4 实测验证），其"等待批复"为**推测**（task_started 后静默超时）；气泡与仪表盘都会标注"（推测）"
- **Claude Code 的权限暂停同样没有落盘记录**，采用"末尾 tool_use 未闭合 + 文件静默 >N 秒"启发式（`monitor.waiting_quiet_sec` 可配），长时间运行的命令可能被误报
- Codex **桌面版（App）不写 rollout jsonl**，只能检测到进程存活（状态"未知"）
- 批复/自动批复通过"窗口置前 + 发送按键"实现（Windows 无跨进程后台注入终端的安全通用接口），发送瞬间会短暂切换焦点；自动批复模式下会自动还原焦点
- **屏幕锁定时无法批复**：Windows 禁止锁屏状态下的前台切换与按键注入（系统安全机制），解锁后恢复正常
- 各 CLI 会话格式属内部格式，解析按 type 字段容错处理，异常行跳过

## 项目结构

```
main.py                 入口（DPI 感知、单实例互斥）
pet/                    UI：窗口/动画器/气泡/仪表盘/皮肤/托盘/自启/配置
agents/                 监听：发现/tailer/四个 watcher/本地摘要/监控线程
actions/                批复：winkeys（SendInput）+ approver
tools/convert.py        素材→透明GIF 管线（含 CLI，子进程调用）
assets/pets/<名字>/     素材（用户自备，不入库）
assets/cache/<名字>@<h> 转换缓存（不入库）
tests/                  回放/批复/托盘/回归测试
```

## 常用配置（config.json）

```jsonc
{
  "skin": "amiya",            // 当前皮肤（= assets/pets 下的文件夹名）
  "scale": 1.0,               // 整体缩放
  "speed": 1.0,               // 播放速度
  "animated": true,           // 动态/静态
  "tray_enabled": true,       // 托盘图标
  "pet_pos": [x, y],          // 锚点（桌宠底部中心）
  "auto_approve": { "enabled": false },  // 自动批复
  "approve_restore_focus": true,
  "bubble": { "font_family": "...", "font_size": 11, "max_width": 280 },
  "monitor": {
    "agents": { "claude": true, "codex": true, "kimi": true, "pi": true },
    "wsl_enabled": true,
    "waiting_quiet_sec": 15
  },
  "keys": { "codex": { "approve": "y", "deny": "Escape" } },
  "window_instances": {}      // 手动绑定的终端窗口标题（批复/唤起用）
}
```

## 测试

```bat
D:\miniconda3\envs\deskpet\python.exe -X utf8 tests\regression.py   # 位置/气泡/缩放/隐藏/托盘/自启
D:\miniconda3\envs\deskpet\python.exe -X utf8 tests\tray_test.py    # 托盘事件链路
D:\miniconda3\envs\deskpet\python.exe -X utf8 tests\approve_e2e.py  # 批复发送链路
D:\miniconda3\envs\deskpet\python.exe -X utf8 tests\replay_real.py  # 真实会话数据回放
```
