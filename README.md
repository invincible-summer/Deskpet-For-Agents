# DeskPet V4.6.0

轻量、被动、零 Hook 的 Windows / WSL AI Agent 桌宠观察器。

DeskPet 常驻 Windows 桌面，被动观察已经由用户启动的 AI 编码 Agent，
把 Goal、Mode、Thinking / Reading / Coding / Testing / Waiting Approval 等
状态映射为桌宠动画和状态气泡。支持 Codex CLI、Claude Code、Kimi CLI、
pi，以及 Codex Desktop / ChatGPT Desktop Codex 模式和 ZCode Desktop。

DeskPet 不创建、不托管、不控制 Agent：不要求 hooks、MCP、插件或代理
脚本，不注入 Agent 进程，不自动审批，不向 Agent 数据库写数据。

## 下载与启动

DeskPet 同时保留两种运行方式。

### 方式 A：Windows x64 Portable（普通用户推荐）

正式 Release 提供：

**[下载最新 DeskPet Windows x64 Portable](https://github.com/invincible-summer/Deskpet-For-Agents/releases/latest/download/DeskPet-windows-x64-portable.zip)**

使用：

1. 下载 `DeskPet-windows-x64-portable.zip`；
2. 解压到任意普通用户可执行目录；
3. 双击 `DeskPet.exe`。

无需安装 Python、pip、虚拟环境或安装器，也不要求管理员权限。普通用户
请下载 Release asset，不要把 GitHub 自动生成的 `Source code (zip)` 当作
可运行程序。

默认发布采用 Nuitka **standalone** 而不是 onefile：仍然免安装，但避免
常驻小工具每次启动都做 onefile 临时解包和额外磁盘 I/O。

### 方式 B：使用现有 Python 3.12 环境

如果机器已经有 Python 3.12，可以直接使用现有环境；不强制创建 DeskPet
专用 `.venv`：

```bat
python -m pip install -r requirements.txt -c constraints.txt
python main.py
```

希望无控制台窗口时可使用：

```bat
pythonw main.py
```

如果更希望依赖隔离，仓库仍保留可选的 repo-local `.venv` 流程：

```bat
Setup-Desktop.bat
Start-Desktop.bat
```

`Setup-Desktop.bat` 只寻找已有 Python 3.12 并创建 `.venv`；
`Start-Desktop.bat` 只启动，不执行 pip 安装或联网修复。

## 第一次使用

首次启动即使没有用户皮肤，也会显示程序化生成的 `builtin-cat`。并行监听
默认开启，每次启动固定进入单宠聚合模式；没有任何 Agent 时仍保留
`pet-1 idle fallback`，除非用户本次运行中显式隐藏全部桌宠。

```text
启动 DeskPet
  ↓
自己打开 Windows Terminal / WSL / Desktop Agent
  ↓
DeskPet 自动发现并被动观察
  ↓
桌宠动画 + Agent 状态气泡
```

用户可在本次运行内切换 SINGLE / AGGREGATE / FLEET；重启后再次回到
并行监听默认开启 + 单宠聚合。

## 皮肤导入

Dashboard → 外观 → `导入皮肤…`，选择包含五个状态素材的文件夹：

```text
MyPet/
├─ walk.webm      工作中
├─ attack.webm    下达指令
├─ die.webm       等待审批
├─ special.webm   完成
└─ sleep.webm     空闲
```

也支持转换器允许的 mp4/mkv/mov/avi/gif。外部目录只是 import source：
DeskPet 会先校验，再复制到自己的数据目录、生成 manifest、再次校验并原子
发布。因此移动/删除原 Downloads/Desktop 素材目录不会破坏已导入皮肤。

FLEET 模式下每只桌宠可独立选择皮肤；同一套皮肤可以重复使用。

## 本地数据目录

所有用户可变数据统一位于：

```text
%LOCALAPPDATA%\DeskPet\
├─ config.json
├─ config.json.bak
├─ icon.ico
└─ assets\
   ├─ pets\
   │  └─ MyPet\
   │     ├─ walk.webm
   │     ├─ attack.webm
   │     ├─ die.webm
   │     ├─ special.webm
   │     ├─ sleep.webm
   │     └─ manifest.json
   └─ cache\
      ├─ MyPet@240\
      └─ builtin-cat@240\
```

Dashboard → 设置 → 本地数据会显示实际路径，并提供 **打开数据目录**。
程序目录与用户数据彻底分离，因此替换 portable 程序目录不会删除配置、
皮肤或缓存。

普通配置全部通过 Dashboard 修改：内存立即生效，经 650ms debounce 后由
单一后台 writer 原子保存到 `config.json`。`config.json` 是内部持久化格式，
普通用户无需手工编辑。

## 状态与交互

状态优先级：

```text
ERROR > WAITING > INPUT > WORKING > DONE > IDLE > UNKNOWN
```

Mode 是独立维度，因此 `WAITING + APPROVAL + PLAN` 是合法组合。

- SINGLE：双击桌宠/气泡，唤起对应 Terminal。
- AGGREGATE：多张 Agent 卡片叠在同一只桌宠上；双击卡片只唤起该 Agent，
  双击桌宠 body 只互动。
- FLEET：每个目标有自己的桌宠/气泡并可使用不同皮肤。
- Codex/ZCode Desktop 只承诺恢复并前置宿主应用，不猜测私有会话导航。

## 被动观察、安全与隐私

```text
Process tells us WHO.
Session data tells us WHAT.
Terminal UIA tells us WHAT THE USER IS ASKED.
StateReducer combines evidence.
DeskPet only observes.
```

DeskPet 不发送键盘输入、不自动审批、不写 Agent 数据库、不 checkpoint WAL，
不因为静默而推断 WAITING。PID/HWND/RuntimeId/WT_SESSION/exact key 等运行期
identity 不持久化。终端可见文本只在内存参与状态/归属判断，默认不落盘；
归属证据不足时宁可缺失证据，也不把审批错归给其他 Agent。

Windows Terminal 唤起使用公共 Win32 API，并在操作前校验 HWND + PID +
进程创建时间 + 窗口类；系统拒绝抢前台时只闪烁提醒，不绕过 foreground
policy。WSL 只探测本轮确认正在运行的 distro，不为了监听而启动已停止 WSL。

# 架构与接口合同

以下边界是维护时必须保持的长期合同。发行逻辑不得扩散进 Monitor、状态融合
或 Presentation。

## `main.py` — 进程入口

启动顺序：

```text
early argv dispatch
→ Windows guard
→ DPI awareness
→ single-instance mutex
→ Config
→ PetApp
```

维护接口：

```text
DeskPet.exe --version
```

内部 worker 接口：

```text
DeskPet.exe --deskpet-internal-converter --gated ...
```

internal converter 必须在 mutex、Tk、Monitor 之前 dispatch，否则转换子进程
会被 GUI 单实例保护拦截或加载不必要的常驻组件。

## `pet/runtime_paths.py` — 唯一路径真值

`RuntimePaths` 提供：

```text
program_root
 data_root
 config_file / config_backup
 assets_dir
 pets_dir / cache_dir
 runtime_icon
```

Windows data root 通过 `SHGetKnownFolderPath(FOLDERID_LocalAppData)` 获取，
`LOCALAPPDATA` 仅为 API 失败 fallback；永不回退到程序目录。

`open_data_root()` 负责 lazy mkdir + `ShellExecuteW("open")`，返回结构化结果，
不会把 Win32 异常抛进 Tk event loop。

## `pet/config.py` / `pet/config_save.py`

默认持久化：

```text
%LOCALAPPDATA%\DeskPet\config.json
%LOCALAPPDATA%\DeskPet\config.json.bak
```

Config 继续负责 schema、migrate、normalize/clamp、revision、dirty 和原子写盘。
测试仍可显式传 `Config(path=temp_file)`，其 backup 跟随 custom path。

运行时保存协议：

```text
Config.set
→ revision++ / dirty
→ ConfigSaveCoordinator 650ms debounce
→ short snapshot
→ <=1 transient writer
→ temp + flush + optional fsync + backup + replace
→ acknowledge revision
```

磁盘写入期间新的 revision 不能被旧 snapshot 错误标记为 clean；同一失败
revision 不无限自动重试。

## `pet/skins.py` / `tools/convert.py`

`SkinBuildManager` 是唯一 skin mutation lane，串行 bootstrap/build/rebuild/
import/maintenance。import 与 cache build 都先写同 filesystem staging，再完整
校验并 atomic directory swap；失败保持旧 live 目录。

source 模式 converter：

```text
python.exe -m tools.convert --gated ...
```

compiled 模式：

```text
DeskPet.exe --deskpet-internal-converter --gated ...
```

二者复用同一个 `tools.convert.main(argv)`。converter 保持独立子进程；
numpy/scipy 在函数内惰性 import，转换结束后峰值内存随子进程退出释放。
Windows 使用 KILL_ON_JOB_CLOSE Job Object + one-byte gate，确保加入 job 之前
不会 spawn ffmpeg，取消/退出可终止整棵 converter→ffmpeg 树。

## `pet/petview.py`

一个 Tk interpreter、N 个 Toplevel PetView。所有 Pet 共享：

- `SharedAnimationCache`
- `AnimationScheduler`
- `SkinBuildManager`
- Monitor / Presentation

增加桌宠数量不得复制这些全局对象；这是 FLEET 仍保持低 CPU/内存的核心。

## `agents/monitor.py`

Monitor 只负责 Agent data plane 编排，输出 revision 驱动的 `AgentTarget`。
它不负责 LocalAppData、Nuitka、GitHub Release、皮肤目录或配置保存。本次 portable
发行不改变 Codex/Claude/Kimi/pi/Codex Desktop/ZCode Desktop 的监听协议。

## `pet/presentation.py`

Presentation 只负责 SINGLE / AGGREGATE / FLEET 和 target→slot/view 映射，
不负责 discovery 或持久化。每次启动固定初始化并行监听 + AGGREGATE；运行时
切换不改变下一次启动默认值。

## `pet/dashboard.py`

Dashboard 是普通用户控制面：设置写入走 Config/AppearanceController +
ConfigSaveCoordinator；皮肤选择只产生 import request，真实复制/校验/发布由
SkinBuildManager 完成；本地数据目录通过 RuntimePaths 显示/打开。

## `pet/autostart.py`

source 模式注册 `pythonw.exe main.py`；compiled 模式注册当前 `DeskPet.exe`。
portable 目录被移动后旧注册项视为 stale，由现有 repair 操作重新登记。不引入
Windows service、Task Scheduler 或安装器专属状态。

# 项目结构

```text
main.py                    GUI / --version / internal converter 入口
pet/runtime_paths.py       LocalAppData 与 program root 唯一边界
pet/config.py              schema / normalize / persistence
pet/config_save.py         async single-writer save coordinator
pet/dashboard.py           用户控制面
pet/skins.py               skin catalog/import/build/cache transaction
pet/petview.py             N PetView + shared cache/scheduler/build
pet/presentation.py        single/aggregate/fleet
agents/                    passive discovery/session/state/terminal/desktop sources
actions/winkeys.py         fail-closed Win32 activation
tools/convert.py           conversion child process
tools/build_release.py     standalone build 唯一入口
tools/release_acceptance.py compiled artifact acceptance
tests/                     unit / benchmark / acceptance contracts
.github/workflows/test.yml source CI
.github/workflows/release.yml tag -> standalone -> GitHub Release
```

# 测试

现有 Python 3.12 环境中：

```bat
python -m unittest discover tests -p "test_*.py"
python tests\benchmark_monitor.py --ticks 5000 --report benchmark-report.json
python tests\benchmark_desktop_sources.py --ticks 5000 --report desktop-source-benchmark.json
python tests\benchmark_presentation.py --report presentation-benchmark.json
python tests\benchmark_ui_architecture.py --report ui-architecture-benchmark.json
python tools\ttfv_probe.py
python tools\real_machine_acceptance.py
```

真实 Windows Terminal 前台策略/UIA 事件仍属于实机 acceptance；CI 只验证纯逻辑、
Win32 调用合同 mock、资源预算和 UI dataflow。

# Portable 构建与 GitHub Release

构建依赖与 runtime 依赖分离：

```text
requirements.txt          runtime/source dependencies
constraints.txt           verified runtime pins
requirements-build.txt    build-only Nuitka pin
```

维护者构建：

```bat
python -m pip install -r requirements.txt -c constraints.txt
python -m pip install -r requirements-build.txt
python tools\build_release.py
```

输出：

```text
.release/
├─ DeskPet.dist/
├─ DeskPet-windows-x64-portable.zip
├─ SHA256SUMS.txt
└─ nuitka-report.xml
```

二进制和 ZIP 不提交 Git history。

正式版本使用 `vX.Y.Z` tag，并强制：

```text
tag == v{pet.version.APP_VERSION}
```

`release.yml` 在 Windows 2022 + Python 3.12 上重新执行单测和 blocking benchmarks，
然后构建 Nuitka standalone、运行 compiled acceptance、生成 ZIP/SHA256，并把：

```text
DeskPet-windows-x64-portable.zip
SHA256SUMS.txt
```

发布为 GitHub Release assets。Actions artifacts 只保存 build report/benchmark 等
诊断数据，不作为长期用户下载入口。

Release 前还必须核验最终发行物中的 FFmpeg license/build configuration，禁止
发布 `--enable-nonfree` 构建；实际依赖族记录于 `THIRD_PARTY_NOTICES.md`。

## 研究资料

Agent 上游合同、实现快照与实证来源见 [SourceLink.md](./SourceLink.md)。
