可以。下面这份是我建议直接放进 **DeskPet V3 设计文档的“技术依据 / References”章节**的资料清单。我按截至 **2026-09-07** 的现行资料重新整理，并明确区分“可以作为实现合同的一手资料”和“只能证明当前行为/缺陷的 issue”。

## 一、Windows Terminal / UI Automation：V3 TerminalObserver 的核心依据

| 资料                                                              | 权威性                           | 能确认什么                                                                                                                       | 对 V3 的直接意义                                                                          |
| --------------------------------------------------------------- | ----------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| **Microsoft Learn — UI Automation Overview**                    | Microsoft 官方文档                | UIA client 可以读取 UI element、control pattern，并订阅 provider 的事件                                                                 | 证明 DeskPet 作为外部只读 UIA Client 是正规的 Windows API 用法 ([Microsoft Learn][1])             |
| **Microsoft Learn — UI Automation Events for Clients**          | Microsoft 官方文档                | UIA 支持事件订阅，目的之一就是避免不断轮询整棵 UI tree                                                                                           | 直接支持 V3 的“事件驱动而非 200ms 扫屏”设计 ([Microsoft Learn][2])                                 |
| **Microsoft Learn — Understanding Threading Issues**            | Microsoft 官方文档                | 与桌面 UI 交互的 UIA client 应在独立线程执行 UIA 调用，避免 UI thread 卡顿                                                                       | 支持 `TerminalUiaObserver` 独立 MTA thread，不放 Tk 主线程 ([Microsoft Learn][3])             |
| **IUIAutomation5::AddNotificationEventHandler**                 | Microsoft Win32 API           | 官方 Notification event handler API                                                                                           | V3 可以订阅 Terminal notification，而不是不停读取完整窗口 ([Microsoft Learn][4])                    |
| **IUIAutomationEventHandlerGroup::AddNotificationEventHandler** | Microsoft Win32 API           | Microsoft 当前建议 UIA client 优先使用 handler group 注册事件                                                                           | 实现 UIA backend 时优先采用较新的 handler-group 路线 ([Microsoft Learn][5])                     |
| **IUIAutomationTextPattern::GetVisibleRanges**                  | Microsoft Win32 API           | 可以取得文本控件当前“可见”的连续文本区域                                                                                                       | V3 approval recognizer 应检查 viewport，而不是整个历史 scrollback ([Microsoft Learn][6])       |
| **Windows Terminal Accessibility 2023**                         | Windows Terminal 官方源码仓库       | Windows Terminal 在 2022 年加入了携带“实际新输出文本”的 UIA notifications；其目的之一就是减少 screen reader 对整个 buffer 做 diff 的性能成本                  | 这是 V3 `Notification → delta text → 必要时 GetVisibleRanges()` 方案最直接的官方依据 ([GitHub][7]) |
| **Windows Terminal Discussion #19614**                          | 官方仓库 + Terminal maintainer 回复 | 外部进程无法方便地直接读取 Terminal console HWND；Windows Terminal maintainer Leonard Hecker 明确建议：创建 UIA client，通过 accessibility API 读取窗口 | 直接证明“已有 Windows Terminal 可以事后被 DeskPet 被动读取”，无需从 ConPTY 启动时接管 ([GitHub][8])         |
| **Windows Terminal UI Automation Scenario #4533**               | 官方仓库                          | Terminal v1.0 就专门建立了 UIA tree、TextRange、GetVisibleRanges 等 accessibility provider                                           | 证明 UIA 并不是偶然暴露出来的能力，而是 Terminal 正式 accessibility 架构的一部分 ([GitHub][9])               |

推荐直接阅读：

[Microsoft UI Automation Overview](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-uiautomationoverview?utm_source=chatgpt.com)
[UI Automation threading guidance](https://learn.microsoft.com/zh-cn/windows/win32/winauto/uiauto-threading?utm_source=chatgpt.com)
[GetVisibleRanges API](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationtextpattern-getvisibleranges?utm_source=chatgpt.com)
[Windows Terminal accessibility design document](https://github.com/microsoft/terminal/blob/main/doc/terminal-a11y-2023.md?utm_source=chatgpt.com)
[Windows Terminal external screen-buffer reading discussion](https://github.com/microsoft/terminal/discussions/19614?utm_source=chatgpt.com)

### 这里可以得出的确定结论

**UIA 路线可行。**

但是 Terminal maintainer 同时提醒：

> UIA 通常不是特别高性能。([GitHub][8])

因此 V3 里我提出的：

```text
Notification event
       ↓
短 delta
       ↓
pattern trigger
       ↓
必要时 GetVisibleRanges
```

明显优于：

```text
500 ms
↓
读取整个 UIA document
↓
diff 整个 terminal
```

---

# 二、Windows Terminal ↔ WSL identity：`WT_SESSION`

这个是 V3 解决：

> “WSL PID 到底属于哪个 Windows Terminal？”

的重要身份 hint。

### Windows Terminal Session Management spec

Windows Terminal 官方设计文档讨论了：

```text
WT_SESSION
```

作为 Terminal session GUID，用于识别命令来自哪个 Terminal session；文档甚至明确讨论通过这个 GUID 找到 hosting window/session。([GitHub][10])

[Windows Terminal Session Management specification](https://github.com/microsoft/terminal/blob/main/doc/specs/%235000%20-%20Process%20Model%202.0/%234472%20-%20Windows%20Terminal%20Session%20Management.md?utm_source=chatgpt.com)

### WT_SESSION 会传播进入 WSL

Windows Terminal 官方 issue #7130 直接记录了：

```text
WSLENV =
WT_SESSION:
WT_PROFILE_ID:
...
```

并引用了原本专门实现：

> Add WT_SESSION to WSLENV so it propagates into WSL

的 PR。([GitHub][11])

更早的 #3948 也明确说明，Terminal 团队修复过“WT_SESSION 没进入 WSL”的问题。([GitHub][12])

[WT_SESSION / WT_PROFILE_ID propagation into WSL](https://github.com/microsoft/terminal/issues/7130?utm_source=chatgpt.com)

因此这一条 V3 方案有依据：

```text
WSL Codex PID
     ↓
/proc/<pid>/environ
     ↓
只提取 WT_SESSION
     ↓
TerminalResolver hint
```

但 **不要把它当绝对真值**。

---

# 三、为什么仍不能根据 WT_SESSION 精确操作某个 Windows Terminal Tab

Windows Terminal 目前仍缺少完整的外部：

```text
WT_SESSION
→ pane/tab
```

查询/激活 API。

2026 年的 Windows Terminal issue #19783 就直接提出：

> external process 无法按照 WT_SESSION programmatically switch 到具体 tab。([GitHub][13])

该 issue 还明确描述：

```text
WT_SESSION
= per tab/pane unique GUID
```

以及当前 workaround 包括 UI Automation，但 tab title 匹配容易脆弱。([GitHub][13])

[Windows Terminal — Focus/Activate Tab by WT_SESSION request](https://github.com/microsoft/terminal/issues/19783?utm_source=chatgpt.com)

所以 V3 里的：

```text
CONFIRMED
HIGH
AMBIGUOUS
NONE
```

binding confidence 是必要的，而不是过度设计。

特别是：

```text
一个 Windows Terminal
    └── Tab
         ├── Pane A → Codex
         └── Pane B → Claude
```

不能假装现在有官方：

```text
WT_SESSION → UIAutomationElement
```

接口。

---

# 四、WSL 进程发现：Microsoft + Linux `/proc` 官方依据

### WSL distribution discovery

Microsoft 官方支持：

```powershell
wsl --list --verbose
wsl -l -v
wsl --list --running
```

用于获得 distro 和 Running/Stopped 状态。([Microsoft Learn][14])

[Microsoft — Basic commands for WSL](https://learn.microsoft.com/en-us/windows/wsl/basic-commands?utm_source=chatgpt.com)

因此 DeskPet：

```text
每 10~15 秒
wsl.exe --list --running
```

是合理的。

---

## `/proc/<pid>/cwd`

Linux man-pages 明确：

```text
/proc/<pid>/cwd
```

是指向该进程当前 working directory 的 symbolic link。([man7.org][15])

[Linux proc_pid_cwd(5)](https://man7.org/linux/man-pages/man5/proc_pid_cwd.5.html?utm_source=chatgpt.com)

所以 V3 不应该继续只靠 session JSONL 猜 project：

```text
PID
 ↓
/proc/PID/cwd
 ↓
/home/user/DeskPet
 ↓
project = DeskPet
```

---

## `/proc/<pid>/stat`

Linux 官方 man-pages 明确提供：

```text
pid
ppid
pgrp
session
tty_nr
tpgid
starttime
```

其中：

* field 4 = PPID
* field 5 = process group
* field 6 = session ID
* field 7 = controlling TTY
* field 8 = foreground process group
* field 22 = process start time since boot

([man7.org][16])

[Linux proc_pid_stat(5)](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html?utm_source=chatgpt.com)

这就是我建议：

```text
PID + starttime
```

作为 WSL process incarnation token，而不是只拿 PID 的依据。

例如：

```text
wsl:Ubuntu|codex|4812|36791842
```

这样 PID 4812 将来被另一个进程重用，也不会误继承旧绑定。

---

# 五、为什么 `/proc/PID/environ` 必须严格 allowlist

Linux man-pages 明确：

```text
/proc/<pid>/environ
```

暴露该进程 initial environment，项目之间用 `\0` 分隔。访问也受 ptrace read permissions 控制。([man7.org][17])

[Linux proc_pid_environ(5)](https://man7.org/linux/man-pages/man5/proc_pid_environ.5.html?utm_source=chatgpt.com)

这意味着 DeskPet 技术上能够看到：

```text
WT_SESSION
CODEX_HOME
CLAUDE_CONFIG_DIR
...
```

但也可能同时看到：

```text
OPENAI_API_KEY
ANTHROPIC_API_KEY
AWS_SECRET_ACCESS_KEY
...
```

所以 V3 必须采用：

```text
WSL 内部读取 environ
        ↓
立即只提取 allowlist
        ↓
Python 永远看不到其他变量
```

而不是：

```python
env = read_all_environ()
```

再在 Python 里过滤。

这是 privacy architecture 的重要区别。

---

# 六、Codex CLI：最重要的一手源码

Codex 是三者中最容易从官方源码精确验证的，因为 `openai/codex` 是开放源码仓库。

## 1. Codex rollout 的持久化策略

这是整个 V3 **“不能用 rollout 判断 Waiting Approval”** 最关键的资料。

Codex 官方源码：

```text
codex-rs/rollout/src/policy.rs
```

明确把这些事件标记为 transient、不持久化：

```text
TerminalInteraction
ExecCommandOutputDelta

ExecApprovalRequest
RequestPermissions
RequestUserInput
ElicitationRequest
ApplyPatchApprovalRequest

...
```

即：

```rust
=> false
```

([GitHub][18])

[OpenAI Codex — rollout persistence policy](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/policy.rs?utm_source=chatgpt.com)

这是 V3 最重要的一条事实依据。

因此：

```text
rollout silence
```

绝对不能推断：

```text
Waiting For Approve
```

---

## 2. UserMessage 会进入 rollout

同一个官方 persistence policy 说明：

```text
EventMsg::UserMessage
```

在 legacy history 中是持久化事件；新的 paginated history 则通过 canonical ResponseItem 表达 user turn。官方 recorder tests 也明确构造并写入 `UserMessageEvent.message`。([GitHub][18])

因此：

> **DeskPet 把“当前真实 user turn 的文本”解释成 Goal**

是有数据基础的。

但需要明确：

```text
Goal
```

是 **DeskPet semantic derived field**，

不是 Codex 官方保证存在一个：

```json
"goal": "..."
```

字段。

这是 V3 设计里应该写清楚的。

---

# 七、Codex Plan Mode

Codex 官方 source 中 `TurnStartedEvent` 有：

```text
collaboration_mode_kind
```

Codex 在创建 `TurnStartedEvent` 时直接：

```rust
collaboration_mode_kind: ctx.mode()
```

另外协议源码/讨论也可以看到：

```rust
pub struct TurnStartedEvent {
    ...
    pub collaboration_mode_kind: ModeKind,
}
```

([GitHub][19])

所以：

```text
Codex Plan
```

不应该通过终端 OCR/UIA 猜。

应该首先从：

```text
TurnStartedEvent
```

获得。

---

# 八、Codex Approval UI 确实存在于 TUI 层

Codex 官方：

```text
codex-rs/tui/src/bottom_pane/approval_overlay.rs
```

定义：

```rust
ApprovalOverlay
```

并处理：

```text
ApprovalRequest::Exec
ApprovalRequest::Permissions
ApprovalRequest::ApplyPatch
ApprovalRequest::McpElicitation
```

当前界面字符串包括：

> `Would you like to run the following command?`

以及 permissions approval UI。([GitHub][20])

[OpenAI Codex — approval_overlay.rs](https://github.com/openai/codex/blob/main/codex-rs/tui/src/bottom_pane/approval_overlay.rs?utm_source=chatgpt.com)

这直接支持：

```text
rollout
→ 不知道 approval

Terminal visible UI
→ 能看到 approval overlay
```

的双信号设计。

但是 V3 **不能只 hardcode 一句话**。

例如不要只写：

```python
if "Would you like to run" in text:
```

而应该：

```text
标题模式
+
选项结构
+
当前可见 viewport
+
短 TTL
```

因为 UI 文案未来可能变化。

---

# 九、Codex 自定义数据根：CODEX_HOME

Codex 官方配置代码明确使用：

```text
CODEX_HOME
```

作为用户配置和相关本地数据 root；配置 stack 中用户配置也是：

```text
${CODEX_HOME}/config.toml
```

([GitHub][21])

[OpenAI Codex config implementation](https://github.com/openai/codex/blob/main/codex-rs/core/src/config/mod.rs?utm_source=chatgpt.com)

因此 DeskPet 不能只写死：

```text
~/.codex
```

必须：

```text
CODEX_HOME
否则 ~/.codex
```

---

# 十、Claude Code：官方文档能确认的部分

Claude Code 不像 Codex 那样完整开源，因此要严格区分官方文档与 bug report。

## Claude session transcript 路径

Anthropic 官方 sessions 文档明确：

```text
~/.claude/projects/<project>/<session-id>.jsonl
```

每行是：

```text
message
tool use
metadata
```

并且 `CLAUDE_CONFIG_DIR` 可以改变这一根目录。([Claude][22])

[Claude Code — Manage sessions](https://code.claude.com/docs/en/sessions?utm_source=chatgpt.com)

---

# 十一、Claude CLAUDE_CONFIG_DIR

Anthropic 官方环境变量文档：

```text
CLAUDE_CONFIG_DIR
```

会覆盖默认：

```text
~/.claude
```

而且：

> settings、credentials、session history、plugins 都存放在这个目录下。([Claude][23])

[Claude Code environment variables](https://code.claude.com/docs/en/env-vars?utm_source=chatgpt.com)

因此 V3 `/proc/PID/environ` allowlist 应包含：

```text
CLAUDE_CONFIG_DIR
```

---

# 十二、Claude Plan / Permission Mode

Anthropic 官方 Permission Modes 文档当前明确列出：

```text
default
acceptEdits
plan
auto
dontAsk
bypassPermissions
```

并明确：

```text
plan
```

是正式 mode，不是简单 UI 状态。([Claude][24])

[Claude Code permission modes](https://code.claude.com/docs/en/permission-modes?utm_source=chatgpt.com)

所以 V3 模型：

```text
mode = PLAN
status = WORKING
phase = READING
```

是正确的，而不是：

```text
status = PLAN
```

---

# 十三、Claude PID → Session registry：有证据，但不是稳定官方 API

这一部分一定要降级为 **implementation hint**。

2026 年 Claude Code 上游 issue 提供了明确实例：

```text
~/.claude/sessions/57116.json

{
  "pid": 57116,
  "sessionId": "...",
  ...
}
```

([GitHub][25])

另一个上游 bug report 也确认 Claude Code 使用：

```text
~/.claude/sessions/<pid>.json
```

记录 running sessions。([GitHub][26])

所以可以用于：

```text
PID
 ↓
sessionId hint
```

但不能当唯一真值。

---

# 十四、为什么 Claude PID registry 不能当唯一真值

官方 issue #53037 / #56766 已经复现：

```text
/clear
```

后：

```text
新的 transcript sessionId
```

发生了变化，但：

```text
~/.claude/sessions/<PID>.json
```

里面的 `sessionId` 仍然是旧值，只是 `updatedAt` 刷新。([GitHub][27])

[Claude PID registry stale after /clear](https://github.com/anthropics/claude-code/issues/53037?utm_source=chatgpt.com)

因此 V3 应：

```text
PID registry = strong hint
+
active transcript mtime
+
cwd
+
new user events
```

进行交叉验证。

而不是：

```text
PID file says session A
→ 永远 session A
```

---

# 十五、为什么 Claude 还需要 Terminal UIA fallback

Claude Code 2026 年曾有 WSL/Linux regression：

```text
interactive session
```

运行期间：

```text
~/.claude/projects/...jsonl
```

只创建 stub 或不实时追加，直到 session 结束/修复后才正常。([GitHub][28])

这类资料属于：

> **现状/回归证据，不是稳定协议。**

但它非常重要，因为它证明：

```text
“只依赖 Claude JSONL”
```

并不足以成为长期可靠的实时 observer architecture。

另一个 Claude issue 也能看到 permission/question dialog 属于 TUI 独立交互层：dialog 未渲染时 CLI 会保持 `Waiting…`。([GitHub][29])

所以：

```text
Claude transcript
+
Terminal UIA
```

比：

```text
Claude transcript only
```

鲁棒得多。

---

# 十六、Kimi Code：官方数据位置

Kimi 是 V3 中资料最完整的一家。

Kimi Code 官方文档明确：

```text
默认：
~/.kimi-code/

可覆盖：
KIMI_CODE_HOME
```

([Kimi][30])

[Kimi Code — Data locations](https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/data-locations.html?utm_source=chatgpt.com)

所以当前 DeskPet V2 的：

```text
~/.kimi/sessions
```

需要升级。

---

# 十七、Kimi session 目录结构

Kimi 官方文档明确给出了：

```text
$KIMI_CODE_HOME/
├── session_index.jsonl
└── sessions/
    └── <workDirKey>/<sessionId>/
         ├── state.json
         └── agents/
              └── main/
                   └── wire.jsonl
```

其中：

### `session_index.jsonl`

包含：

```text
sessionId
sessionDir
workDir
```

### `state.json`

包含：

```text
title
lastPrompt
timestamps
```

### `agents/main/wire.jsonl`

是：

> main Agent 的完整 communication record，用于 session resume/replay。([Kimi][30])

因此 Kimi 不需要 DeskPet：

```text
递归扫描所有 JSONL
```

而应该：

```text
cwd
 ↓
session_index.jsonl
 ↓
sessionDir
 ↓
wire.jsonl
```

---

# 十八、Kimi Plan Mode

Kimi 当前 runtime source 的 `StatusUpdate` 明确包含：

```typescript
payload: {
    model: status.model,
    thinking_effort: status.thinkingEffort,
    plan_mode: status.planMode,
}
```

([GitHub][31])

[Kimi Code session runtime — StatusUpdate](https://github.com/MoonshotAI/kimi-code/blob/main/apps/vscode/src/runtime/session-runtime.ts?utm_source=chatgpt.com)

Kimi changelog 也明确记录：

* 加入 Plan mode
* `plan_mode` 保存到 SessionState
* resume 后恢复
* `EnterPlanMode` / `ExitPlanMode` 后重新发正确 StatusUpdate

([GitHub][32])

因此：

```text
Kimi mode = PLAN
```

属于非常高可信的结构化状态。

---

# 十九、Kimi ApprovalRequest

Kimi Agent SDK 官方仓库文档明确列出 wire 类型：

```text
TurnBegin
StepBegin
ThinkPart
ToolCall
ToolResult
StatusUpdate
ApprovalRequest
ApprovalRequestResolved
```

并明确：

> 未处理的 ApprovalRequest 会阻塞当前 turn。([GitHub][33])

[Kimi Agent SDK — session/wire messages](https://github.com/MoonshotAI/kimi-agent-sdk/blob/main/guides/python/session.md?utm_source=chatgpt.com)

所以对于 Kimi：

```text
wire ApprovalRequest
→ WAITING_APPROVAL
```

是 **EXACT**。

不需要 UIA 猜。

---

# 二十、Kimi Plan approval 本身也有官方说明

Kimi 官方 tools reference：

```text
EnterPlanMode
ExitPlanMode
```

并明确说明：

> `ExitPlanMode` 会把 plan 展示给用户审批，再退出 Plan mode。([GitHub][34])

[Kimi Code — Plan Mode tools reference](https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/reference/tools.md?utm_source=chatgpt.com)

这对 DeskPet 非常有价值：

```text
plan_mode=true
+
ApprovalRequest
```

就可以准确展示：

```text
Kimi · Plan · 等待计划确认
```

---

# 二十一、DeskPet 当前代码本身的审计依据

当前仓库基线仍是：

```text
main
4707d90b87a9b75a587ed5f9dc2a9c0eae599164
commit: v2
```

当前 V2 `agents/discovery.py` 的 WSL scanner 确实只执行：

```text
ps -eo pid=,etimes=,args=
```

最终只有：

```text
kind
pid
source
started_at
```

并没有：

```text
cwd
tty
uid
sid
WT_SESSION
```

当前 `Monitor` 仍包含 Managed/Readonly 双路聚合。

Dashboard 当前也仍然提供：

```text
新建 Codex 会话
绑定终端
绑定会话文件
自动批准
发送任务
```

等 V2 模型。

[DeskPet repository](https://github.com/invincible-summer/DeskPet?utm_source=chatgpt.com)

---

# 二十二、我建议实际开发时按这个“资料可信度等级”执行

### A — 可以当实现合同

这些可以直接指导代码：

**Microsoft：**

* UI Automation API
* UIA threading
* UIA event subscription
* GetVisibleRanges
* WSL CLI

**Linux：**

* `/proc/PID/stat`
* `/proc/PID/cwd`
* `/proc/PID/environ`

**OpenAI Codex：**

* `rollout/src/policy.rs`
* `protocol.rs`
* `approval_overlay.rs`
* config source

**Anthropic：**

* sessions docs
* permission modes
* env vars / `CLAUDE_CONFIG_DIR`

**Kimi：**

* Data Locations
* Sessions
* Wire/SDK types
* Plan tools
* StatusUpdate source

---

### B — 可以作为强实现 hint，但必须有 fallback

例如：

```text
Claude ~/.claude/sessions/<PID>.json
```

有非常明确的上游实例，但当前并不是一个公开“外部 tooling API”。

所以：

```text
使用 ✅
依赖它作为唯一真值 ❌
```

---

### C — Issue / bug report 只能说明边界

例如：

```text
Claude transcript 不实时刷新
Claude PID registry /clear 后 stale
Windows Terminal 没有 WT_SESSION → tab external API
```

这些用于回答：

> “为什么 V3 必须有 fallback / confidence？”

但不能在代码里假定：

> “这个 bug 永远存在。”

---

## 最值得你优先自己核对的 12 个链接

1. [Windows Terminal accessibility / UIA notifications](https://github.com/microsoft/terminal/blob/main/doc/terminal-a11y-2023.md?utm_source=chatgpt.com)
2. [Windows Terminal maintainer：外部程序用 UIA 读取 Terminal](https://github.com/microsoft/terminal/discussions/19614?utm_source=chatgpt.com)
3. [Microsoft UIA threading requirements](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading?utm_source=chatgpt.com)
4. [UIA GetVisibleRanges](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationtextpattern-getvisibleranges?utm_source=chatgpt.com)
5. [WT_SESSION → WSLENV evidence](https://github.com/microsoft/terminal/issues/7130?utm_source=chatgpt.com)
6. [Linux /proc PID stat specification](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html?utm_source=chatgpt.com)
7. [Codex rollout persistence policy](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/policy.rs?utm_source=chatgpt.com)
8. [Codex approval TUI implementation](https://github.com/openai/codex/blob/main/codex-rs/tui/src/bottom_pane/approval_overlay.rs?utm_source=chatgpt.com)
9. [Claude Code session storage documentation](https://code.claude.com/docs/en/sessions?utm_source=chatgpt.com)
10. [Claude Code permission modes](https://code.claude.com/docs/en/permission-modes?utm_source=chatgpt.com)
11. [Kimi Code data locations](https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/data-locations.html?utm_source=chatgpt.com)
12. [Kimi Wire ApprovalRequest documentation](https://github.com/MoonshotAI/kimi-agent-sdk/blob/main/guides/python/session.md?utm_source=chatgpt.com)

用这些资料交叉验证以后，V3 最核心的三路监听架构基本是站得住的：

```text
/proc / psutil
→ WHO / WHERE

Agent persistence
→ WHAT IT IS DOING

Windows Terminal UIA
→ WHAT THE TERMINAL IS CURRENTLY ASKING
```

其中最关键的事实也得到了相当直接的一手支持：**Codex approval 事件明确不持久化到 rollout，而 Windows Terminal 明确提供 UIA 文本/notification 能力；因此用 UIA 补 Codex/Claude 的终端交互态并不是 workaround 式猜测，而是当前“不改 Agent、不用 hooks、旁路监听已有 CLI”约束下最有依据的实现路线。** ([GitHub][18])

[1]: https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-uiautomationoverview?utm_source=chatgpt.com "UI Automation Overview - Win32 apps | Microsoft Learn"
[2]: https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/ui-automation-events-for-clients?utm_source=chatgpt.com "UI Automation Events for Clients - .NET Framework | Microsoft Learn"
[3]: https://learn.microsoft.com/zh-cn/windows/win32/winauto/uiauto-threading?utm_source=chatgpt.com "了解线程问题 - Win32 apps | Microsoft Learn"
[4]: https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomation5-addnotificationeventhandler?utm_source=chatgpt.com "IUIAutomation5::AddNotificationEventHandler (uiautomationclient.h) - Win32 apps | Microsoft Learn"
[5]: https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationeventhandlergroup-addnotificationeventhandler?utm_source=chatgpt.com "IUIAutomationEventHandlerGroup::AddNotificationEventHandler (uiautomationclient.h) - Win32 apps | Microsoft Learn"
[6]: https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationtextpattern-getvisibleranges?utm_source=chatgpt.com "IUIAutomationTextPattern::GetVisibleRanges (uiautomationclient.h) - Win32 apps | Microsoft Learn"
[7]: https://github.com/microsoft/terminal/blob/main/doc/terminal-a11y-2023.md?plain=1&utm_source=chatgpt.com "terminal/doc/terminal-a11y-2023.md at main · microsoft/terminal · GitHub"
[8]: https://github.com/microsoft/terminal/discussions/19614?utm_source=chatgpt.com "Read terminal screen buffer programmatically? · microsoft terminal · Discussion #19614 · GitHub"
[9]: https://github.com/microsoft/terminal/issues/4533?utm_source=chatgpt.com "Scenario: Add Support for UI Automation · Issue #4533 · microsoft/terminal · GitHub"
[10]: https://github.com/microsoft/terminal/blob/main/doc/specs/%235000%20-%20Process%20Model%202.0/%234472%20-%20Windows%20Terminal%20Session%20Management.md?utm_source=chatgpt.com "terminal/doc/specs/#5000 - Process Model 2.0/#4472 - Windows Terminal Session Management.md at main · microsoft/terminal · GitHub"
[11]: https://github.com/microsoft/terminal/issues/7130?utm_source=chatgpt.com "Duplicate WT_SESSION and WT_PROFILE_ID in WSLENV env var · Issue #7130 · microsoft/terminal · GitHub"
[12]: https://github.com/microsoft/terminal/issues/3948?utm_source=chatgpt.com "WT_SESSION doesn’t appear in WSL · Issue #3948 · microsoft/terminal · GitHub"
[13]: https://github.com/microsoft/terminal/issues/19783?utm_source=chatgpt.com "Feature Request: Focus/Activate Tab by WT_SESSION · Issue #19783 · microsoft/terminal · GitHub"
[14]: https://learn.microsoft.com/zh-cn/windows/wsl/basic-commands?utm_source=chatgpt.com "WSL 的基本命令 | Microsoft Learn"
[15]: https://www.man7.org/linux/man-pages/man5/proc_pid_cwd.5.html?utm_source=chatgpt.com "proc_pid_cwd(5) - Linux manual page"
[16]: https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html "proc_pid_stat(5) - Linux manual page"
[17]: https://man7.org/linux/man-pages/man5/proc_pid_environ.5.html "proc_pid_environ(5) - Linux manual page"
[18]: https://github.com/openai/codex/blob/main/codex-rs/rollout/src/policy.rs?utm_source=chatgpt.com "codex/codex-rs/rollout/src/policy.rs at main · openai/codex · GitHub"
[19]: https://github.com/openai/codex/discussions/11261?utm_source=chatgpt.com "[Question] Clarify TurnStartedEvent.model_context_window optionality · openai codex · Discussion #11261 · GitHub"
[20]: https://github.com/openai/codex/blob/main/codex-rs/tui/src/bottom_pane/approval_overlay.rs?utm_source=chatgpt.com "codex/codex-rs/tui/src/bottom_pane/approval_overlay.rs at main · openai/codex · GitHub"
[21]: https://github.com/openai/codex/blob/main/codex-rs/core/src/config/mod.rs?utm_source=chatgpt.com "codex/codex-rs/core/src/config/mod.rs at main · openai/codex · GitHub"
[22]: https://code.claude.com/docs/en/sessions?utm_source=chatgpt.com "Manage sessions - Claude Code Docs"
[23]: https://code.claude.com/docs/en/env-vars?utm_source=chatgpt.com "Environment variables - Claude Code Docs"
[24]: https://code.claude.com/docs/en/permission-modes?utm_source=chatgpt.com "Choose a permission mode - Claude Code Docs"
[25]: https://github.com/anthropics/claude-code/issues/47018?utm_source=chatgpt.com "Expose CLAUDE_SESSION_ID as environment variable in tool execution context · Issue #47018 · anthropics/claude-code · GitHub"
[26]: https://github.com/anthropics/claude-code/issues/74566?utm_source=chatgpt.com "[BUG] Starting claude inside a sandbox deletes the session records of every other running session · Issue #74566 · anthropics/claude-code · GitHub"
[27]: https://github.com/anthropics/claude-code/issues/53037?utm_source=chatgpt.com "~/.claude/sessions/{PID}.json sessionId field doesn't refresh on /clear — only updatedAt does · Issue #53037 · anthropics/claude-code · GitHub"
[28]: https://github.com/anthropics/claude-code/issues/66486?utm_source=chatgpt.com "[BUG] 2.1.169: interactive sessions write no JSONL transcript (only ai-title stub) — reproduced on WSL2 + macOS; breaks --resume/history · Issue #66486 · anthropics/claude-code · GitHub"
[29]: https://github.com/anthropics/claude-code/issues/64289?utm_source=chatgpt.com "Permission/question dialogs do not render in Ctrl+O transcript mode (UI hangs indefinitely) · Issue #64289 · anthropics/claude-code · GitHub"
[30]: https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/data-locations.html?utm_source=chatgpt.com "Data locations | Kimi Code Docs"
[31]: https://github.com/MoonshotAI/kimi-code/blob/main/apps/vscode/src/runtime/session-runtime.ts?utm_source=chatgpt.com "kimi-code/apps/vscode/src/runtime/session-runtime.ts at main · MoonshotAI/kimi-code · GitHub"
[32]: https://github.com/bon3less/MoonshotAI_kimi-cli/blob/main/CHANGELOG.md?utm_source=chatgpt.com "MoonshotAI_kimi-cli/CHANGELOG.md at main · bon3less/MoonshotAI_kimi-cli · GitHub"
[33]: https://github.com/MoonshotAI/kimi-agent-sdk/blob/main/guides/python/session.md?utm_source=chatgpt.com "kimi-agent-sdk/guides/python/session.md at main · MoonshotAI/kimi-agent-sdk · GitHub"
[34]: https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/reference/tools.md?utm_source=chatgpt.com "kimi-code/docs/en/reference/tools.md at main · MoonshotAI/kimi-code · GitHub"
