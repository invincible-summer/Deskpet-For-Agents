# DeskPet V4.0.1

DeskPet 是一个轻量的 Windows 桌宠，用来被动观察你已经启动的 AI 编码 Agent，并把当前任务、模式和工作状态显示成桌宠动画与气泡。

支持 Windows / WSL 终端中的 Codex CLI、Claude Code、Kimi CLI、pi，以及 Codex Desktop / ChatGPT Desktop Codex 模式和 ZCode Desktop；ZCode Desktop 通过 Remote Development 连接到 WSL 时，也会从对应 WSL 用户环境读取会话状态。DeskPet 不接管 Agent，不要求 Hook、插件或 MCP，也不会自动审批。

当前源码版本是 **4.0.1**。按本次维护要求没有创建 `v4.0.1` tag 或 GitHub Release，因此仓库的 latest Portable 下载仍然指向已经发布的 **v4.0.0**；这不影响从源码运行和验收 4.0.1。

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

## 三种显示模式

- **单 Agent / SINGLE**：一只桌宠对应当前 Agent。
- **单宠聚合 / AGGREGATE**：多个 Agent 的状态卡片集中在一只桌宠上，这是每次启动的默认模式。
- **多宠分离 / FLEET**：每个 Agent 使用独立桌宠；每只桌宠可以选择不同皮肤，同一套皮肤也可以重复使用。

双击对应 Agent 的气泡或桌宠可以请求唤起它所在的终端或桌面应用。桌面端目前只承诺恢复并前置宿主应用，不猜测或调用私有会话跳转接口。

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

## 更新 Portable 版

新版发布后：

1. 下载新的 `DeskPet-windows-x64-portable.zip`；
2. 退出正在运行的 DeskPet；
3. 解压新版程序；
4. 启动新的 `DeskPet.exe`。

配置和皮肤仍保存在 `%LOCALAPPDATA%\DeskPet`，不会因为更换程序目录而消失。

## 发布文件说明

正式 Release 主要提供：

```text
DeskPet-windows-x64-portable.zip   普通用户下载的完整程序
SHA256SUMS.txt                     ZIP 的 SHA-256 校验值
```

源码仓库中的 `.release/` 只是本地/CI 构建输出目录，不提交到 Git 历史。

## 项目资料

- [VERSIONING.md](VERSIONING.md)：Semantic Versioning、源码版本与正式 Release 规则。
- [CHANGELOG.md](CHANGELOG.md)：各版本新增功能、修复内容及历史版本映射。
- [SourceLink.md](SourceLink.md)：Agent 监听、Windows/WSL、桌面端数据源等调研与依据。
- [AGENTS.md](AGENTS.md)：仓库开发和验收约定。
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)：Portable 包中第三方组件说明。

源码测试与发布构建面向维护者，不影响普通用户通过 Portable ZIP 解压即用。
