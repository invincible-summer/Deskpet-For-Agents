# DeskPet V4.2.0

DeskPet 是一个轻量的 Windows 桌宠，用来被动观察你已经启动的 AI 编码 Agent，并把当前任务、模式和工作状态显示成桌宠动画与气泡。

支持 Windows / WSL 终端中的 Codex CLI、Claude Code、Kimi CLI、pi、OpenCode CLI 与 DSH（DeepSeek Harness）CLI，以及 Codex Desktop / ChatGPT Desktop Codex 模式、ZCode Desktop、OpenCode Desktop 和 DeepSeek Harness Desktop；ZCode Desktop 通过 Remote Development 连接到 WSL 时，也会从对应 WSL 用户环境读取会话状态。DeskPet 不接管 Agent，不要求 Hook、插件或 MCP，也不会自动审批。

当前源码版本是 **4.2.0**。`v4.2.0` 是本轮监听与桌面唤起可靠性升级的源码版本；本版本**不创建 GitHub Release，也不生成新的 Portable/EXE 发布文件**，除非另行发布验收通过的二进制资产。
版本 tag 与二进制 Release 分开推进：tag 用于固定已经验收的源码里程碑，GitHub Release 只在确实值得重新分发 Portable 的版本上显式发布，不会再由 tag push 自动触发。历史 `v1.0.0`、`v2.0.0`、`v3.0.0` Release 用于按开发顺序补齐源码版本历史，不反向制造当时并不存在的 Portable 二进制。详细规则见 [VERSIONING.md](VERSIONING.md)。

## 下载与启动

DeskPet 提供两种使用方式。普通用户建议直接使用 Portable 版；需要改代码或调试时再使用 Python 源码方式。

### 方式 A：下载 Windows Portable（推荐）

下载最新版本：

**[DeskPet-windows-x64-portable.zip](https://github.com/invincible-summer/Deskpet-For-Agents/releases/latest/download/DeskPet-windows-x64-portable.zip)**

使用方法：

1. 下载 `DeskPet-windows-x64-portable.zip`。
2. 解压整个 `DeskPet` 文件夹到任意普通目录。
3. 双击 `DeskPet.exe`。

Portable 版已经带齐运行所需组件，**不需要安装 Python、pip、虚拟环境，也不需要管理员权限**。

请保留解压后的整个 `DeskPet` 文件夹，不要只单独复制 `DeskPet.exe`。GitHub 页面自动生成的 `Source code (zip)` 是源码，不是可直接运行的 Portable 程序。

### 方式 B：使用现有 Python 3.12 环境

如果你已经有现有 Python 3.12，可以直接在源码目录运行：

```bat
python -m pip install -r requirements.txt -c constraints.txt
python main.py
```

希望后台启动、不显示控制台窗口时：

```bat
pythonw main.py
```

如果希望依赖与其他项目隔离，也可以使用仓库提供的可选 `.venv` 流程：

```bat
Setup-Desktop.bat
Start-Desktop.bat
```

`Setup-Desktop.bat` 用于创建并准备 `.venv`；`Start-Desktop.bat` 只负责启动，不会每次重新安装依赖。

## 开始使用

启动 DeskPet 后，照常在 Windows Terminal、WSL 或受支持的桌面 Agent 中工作即可，不需要改变原来的 Agent 启动方式。

```text
启动 DeskPet
    ↓
打开 Windows Terminal / WSL / Desktop Agent
    ↓
正常运行 Codex / Claude / Kimi / pi 等 Agent
    ↓
DeskPet 自动发现并显示状态
```

并行监听默认开启，每次启动默认进入**单宠聚合模式**。即使当前没有任何 Agent，也会保留至少一个桌宠显示在桌面上（`pet-1 idle fallback`），除非你在本次运行中主动隐藏桌宠。

DeskPet 会根据可用证据显示 Thinking、Reading、Coding、Testing、Waiting Approval、Done、Idle 等状态；Mode（例如 Plan）会独立显示。

气泡首行显示当前工作区名称（来自会话的工作目录），下面依次显示 Agent/状态、
当前动作和任务目标。活动摘要保留命令、文件路径或搜索词等必要对象；结构化输入
只提取任务主干，不直接展示 JSON 字段。读取不到动作细节时沿用明确状态和任务
上下文，不猜测 Agent 正在执行的内容；气泡不显示思考强度。

Dashboard 默认打开 **Agent 工作台**，在一个页面查看状态统计、筛选会话并展开
所选会话详情；桌宠、外观、监听与隐私、诊断、设置分别管理各自功能。
监听开关关闭或进程确认退出后，旧发现缓存不会重新生成同一进程的桌宠。
在「监听与隐私」页可以为每个 Agent 自定义**监听目标路径（数据根）**：留空
使用默认（环境变量 → 内置路径），也可以直接输入或浏览选择自己的目录，
保存后立即重新扫描。自定义路径只影响 Windows 监听（WSL 始终跟随各发行版
内进程的真实 HOME），并只保存在本机用户配置（`%LOCALAPPDATA%\DeskPet`），
不会写入仓库或其他任何位置。

## 三种显示模式

- **单 Agent / SINGLE**：一只桌宠对应当前 Agent。
- **单宠聚合 / AGGREGATE**：多个 Agent 的状态卡片集中在一只桌宠上，这是每次启动的默认模式。
- **多宠分离 / FLEET**：每个 Agent 使用独立桌宠；每只桌宠可以选择不同皮肤，同一套皮肤也可以重复使用。

双击对应 Agent 的气泡或桌宠可以请求唤起它所在的终端或桌面应用。桌面端目前只承诺恢复并前置宿主应用，不猜测或调用私有会话跳转接口。

Codex、DSH、ZCode 最小化到托盘时，DeskPet 使用已核验的应用单实例启动入口，请应用自身恢复窗口内容；任务栏最小化使用 Windows 恢复。重复唤起请求会合并，Dashboard 保持当前显隐状态。无法核验宿主或启动方式时显示失败，不猜测启动另一个实例。真实托盘交互验收进度见 [验收记录](tests/ACCEPTANCE-desktop-activation.md)。

## 自定义皮肤

打开 Dashboard → 外观 → `导入皮肤…`，选择包含五个状态素材的文件夹：

```text
MyPet/
├─ walk.webm      工作中
├─ attack.webm    下达指令
├─ die.webm       等待审批
├─ special.webm   完成
└─ sleep.webm     空闲
```

也支持转换器允许的 mp4、mkv、mov、avi、gif。导入成功后，DeskPet 会把素材复制到自己的数据目录，因此原素材目录之后移动或删除不会影响已经导入的皮肤。

没有自定义皮肤时会使用内置 `builtin-cat`，无需额外下载素材。

## 配置和用户数据放在哪里

所有可变用户数据统一保存在：

```text
%LOCALAPPDATA%\DeskPet\
├─ config.json
├─ config.json.bak
├─ icon.ico
└─ assets\
   ├─ pets\
   └─ cache\
```

Dashboard 的设置页可以直接查看并打开这个目录。

程序目录和用户数据是分开的，所以以后更新 Portable 版时，可以替换程序文件而不覆盖已有配置和已导入皮肤。

## DeskPet 会不会修改 Agent

不会。DeskPet 的正常监听路径保持被动、只读：

- 不向 Agent 进程注入代码；
- 不要求配置 Hook、插件或代理脚本；
- 不向 Agent 数据库写数据；
- 不自动发送键盘输入；
- 不自动批准 Approve 请求；
- 证据不足时不会把一个 Agent 的审批状态猜给另一个 Agent。

等待审批时，DeskPet 负责提示和帮助你快速回到对应窗口，最终操作仍由用户在 Agent 自己的界面中完成。

## OpenCode 与 DSH 的监听边界

OpenCode 与 DSH（DeepSeek Harness）同样以无插件、只读方式接入：

- **OpenCode** 读取本机（或对应 WSL 用户）`~/.local/share/opencode` 下的
  `opencode.db` 会话索引，并在能力探测通过时读取只读的 active/execution
  facts；只提供标题、工作目录等有界元数据。归档、删除和长期无活动的
  会话不会持续展示。OpenCode 的未决
  审批保存在其运行进程内存中；只有当本机 OpenCode 托管服务的注册文件
  （`~/.local/state/opencode/service.json`）指向的端口与一个存活的
  OpenCode 进程吻合、且该服务的只读 `GET /api/permission/request` 可匿名
  访问时，才显示"等待审批"。DeskPet 绝不读取该注册文件中的服务密码，
  因此在 OpenCode 默认启用随机服务密码的当前版本里，待审批状态通常
  显示为"状态暂不可读"（DEGRADED），绝不伪造。
- **DSH** 直接读取 `$DSH_HOME/sessions`（默认 `~/.dsh/sessions`）下的
  会话日志：明文 `session[.vN].jsonl` 直接解析；默认压缩格式
  `session.vN.jsonl.zstd` 按上游逐帧追加的结构解码（支持当前 generation，
  只消费完整帧，
  半帧等待写完整；每帧校验 checksum 并限制解压大小）。大日志从帧边界
  起步，header 由有界引导读取恢复。`turn/start`、
  `turn/end`、`step/*`、`tool/*` 事件给出 Working/Done/Error 状态，
  `approval/asked` 与 `approval/decided` 按请求 id 配对给出真实的
  等待审批与解除。DeskPet 不读取 `$DSH_HOME/.credentials.yaml`，不参与
  DSH Host 的任何私有 IPC。
- **审批可见性**：Codex 的 rollout 把审批请求列为 transient 不落盘，
  因此 rollout 数据面无法判断当前是否有未决审批。这类会话工作时会明确
  标注"审批状态不可见"，而不是伪造等待审批；DSH 的
  `approval/asked` 与 `approval/decided` 是落盘事件，可以给出真实的
  等待审批与解除。
- 两个 Agent 的 CLI 进程（Windows 与 WSL）沿用与其他 CLI 相同的终端
  唤醒；官方桌面宿主（OpenCode.exe / DeepSeek Harness.exe）提供应用级
  恢复前置。同一工作目录下同时运行多个同类 CLI 时无法从外部区分会话
  归属，此时诚实显示"未解析"而不是猜测绑定。

以上两个 Agent 的数据面解析以合成 fixture 通过自动化回归（schema 能力
探测、Zstandard 帧/校验/解压上限、审批配对、路径遍历防护、同 cwd 多实例
不绑定、旧配置迁移等）。Codex 的 WSL `app-server` 常驻形态识别、
DSH 压缩日志帧对齐起步与 header 引导已在本机实机复现取证并修复；
针对真实安装的 OpenCode / DSH（Windows、WSL 与官方桌面端）的逐表面
实机验收状态以 `tools/desktop_source_probe.py` 的脱敏探测结果为准。

## 更新 Portable 版

新版发布后：

1. 下载新的 `DeskPet-windows-x64-portable.zip`；
2. 退出正在运行的 DeskPet；
3. 解压新版程序；
4. 启动新的 `DeskPet.exe`。

配置和皮肤仍保存在 `%LOCALAPPDATA%\DeskPet`，不会因为更换程序目录而消失。

## 发布文件说明

从 v4.0.0 开始，正式二进制 Release 主要提供：

```text
DeskPet-windows-x64-portable.zip   普通用户下载的完整程序
SHA256SUMS.txt                     ZIP 的 SHA-256 校验值
```

`v1.0.0`～`v3.0.0` 是补齐项目开发顺序的历史源码 Release，只指向当时的真实 tag/commit，不补造后来才建立的 Portable 资产。源码仓库中的 `.release/` 只是本地/CI 构建输出目录，不提交到 Git 历史。

## 项目资料

- [VERSIONING.md](VERSIONING.md)：Semantic Versioning、源码 tag 与正式 Release 规则。
- [CHANGELOG.md](CHANGELOG.md)：各版本新增功能、修复内容及历史版本映射。
- [AGENTS.md](AGENTS.md)：仓库开发和验收约定（含各 Agent 上游对齐版本表）。
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)：Portable 包中第三方组件说明。

源码测试与发布构建面向维护者，不影响普通用户通过 Portable ZIP 解压即用。
