# DeskPet 版本变更记录

DeskPet 使用 Semantic Versioning 2.0.0。版本与 Release 的关系见
[VERSIONING.md](VERSIONING.md)。本文件维护各个可识别版本新增了什么功能、
修复了什么问题；源码版本可以先于 Git tag / GitHub Release 前进。

## 4.0.1 — ZCode WSL Remote Development 监听修复（源码版本，未发布）

状态：已作为源码维护版本验收；**不创建 `v4.0.1` tag，不创建 GitHub
Release，也不生成新的 Portable/EXE 发布文件**。最新已发布二进制仍是
`v4.0.0`。

### 新增与修复

- 修复 Windows ZCode Desktop 连接 WSL Remote Development 后只读取 Windows
  `%USERPROFILE%\.zcode\cli\db\db.sqlite`、因此看不到远端任务的问题。
- 监听拓扑拆成两个事实面：Windows `ZCode.exe` 继续作为桌面宿主和激活目标；
  会话状态从实际运行 Agent 的 WSL distro + Linux uid/user/HOME 数据面读取。
- `WslProcessProbe` 只识别已经运行的 `~/.zcode/server/zcode-server.cjs` /
  `~/.zcode/server/agents/glm/zcode.cjs`，同一 uid 的 server + child 合并为一个
  data plane；这些进程不会被错误显示成 Terminal Agent。
- 远端 SQLite 由 WSL 内 ZCode 自带的 `~/.zcode/server/node` + `node:sqlite`
  以 `readOnly: true` 打开；不通过 `\\wsl.localhost` 打开 live WAL，不执行
  checkpoint、repair、写入或依赖安装。
- 远端读取只发生在既有 `ProcessProbeWorker`，且必须来自同一轮 fresh
  `wsl --list --running` / process census；Monitor/UI 线程不执行 `wsl.exe`。
- 远端 plane 上限为 4，ZCode Desktop 已接纳会话仍维持全局最多 8 个，未新增
  常驻线程或每会话 worker。
- 本地 Windows ZCode 与多个 WSL plane 可以同时存在；相同 session id 按
  plane 隔离，不交叉继承状态。远端审批请求仍只从明确的
  `tool_usage approval_status=requested + running` 投影为 WAITING。
- WSL 用户名改为 `getent passwd <uid>` 的明确字段，不再从 HOME basename
  猜用户名；自定义 Linux 用户与非 `/home/<name>` HOME 都按实际身份工作。
- UI 来源标签新增 `ZCode · Desktop · WSL <distro>`，宿主激活仍只恢复 Windows
  ZCode 应用，不注入输入、不调用私有远端控制面。
- `tools/desktop_source_probe.py` 增加脱敏的 WSL runtime 结构诊断；输出不包含
  prompt、transcript、tool arguments、token/secret。

### 安全/可靠性约束

- 不要求 Hook、插件、MCP 或 Agent 配置改动；不自动审批。
- WSL 枚举失败或远端 DB 暂时 busy/超时/schema 不兼容时使用
  non-authoritative / last-good 语义，不把“读不到”解释成“任务不存在”。
- stopped distro 的权威 tombstone 会移除 remote plane；不会使用过期的
  “Running” 缓存去授权 `wsl -d` 远端读取。
- 私有 ZCode 路径/schema 都按 capability 检测处理；上游变化时安全降级而不是
  猜测状态。

## 4.0.0 — Portable 正式发布基线

首个采用永久版本规则的 GitHub 二进制 Release。

- 提供 Windows x64 Portable 独立发行包和 `DeskPet.exe`；普通用户不需要
  Python、pip 或虚拟环境。
- 可变数据统一迁移到 `%LOCALAPPDATA%\DeskPet`，程序目录与配置/皮肤/cache
  分离，便于覆盖升级。
- 建立正式构建/验收/Release pipeline、SHA-256 manifest 和编译产物验收。
- 包含 v3 开发线完成的被动终端/桌面 Agent 观察、三种呈现模式、Dashboard、
  自定义皮肤和可靠性改进。
- 正式安全合同保持：不注入、不自动审批、不写 Agent 数据库。

## 历史主版本

### 3.0.0 — Passive-only observation

Commit: `da20a689654093e247abf1c996011b9351d36604`

移除旧控制/审批 plane，确立被动只读监听：不再进行输入注入和自动批准。

### 2.0.0 — Managed approval channel

Commit: `e032640fb78fa043214214b292e76b81405e277f`

审批从通用按键注入迁移到受管理的 Codex app-server JSON-RPC 路径；这是相对
v1 行为合同的 breaking change。该控制路径后来在 v3 被移除。

### 1.0.0 — Initial public behavior

Commit: `c0ca087b74c2545dbbccbff5a58a380638becfd1`

首个可用里程碑：五状态动画、状态气泡以及早期多 Agent/审批行为。

## 2026-09-12 一次性 tag 归一化

在首个正式 GitHub 二进制 Release 前，旧的开发阶段 tag 被清理，使今后的版本号
表达兼容性而不是内部实现阶段。Git commit 历史没有删除或改写。

| 已退役 tag | 历史 commit |
| --- | --- |
| `v3.1.0` | `dfc32b7169087318c88e7c8fdb9a538849d46ae4` |
| `v3.1.2` | `7840568e15f024f78b885a92d7cc839b59121556` |
| old `v4.0.0` | `2330c26d86d38d6e8dc82ba511ff73a63d6d2d99` |
| `v4.1.0` | `ef3eea404524e46dff6a76c84bdafe1ccf179eb1` |
| `v4.1.1` | `0572da27819698bb8f263d553044916e3db2db3b` |
| `v4.2.0` | `ace08379761d8c1e63cc025f1e1093624e535444` |
| `v4.2.1` | `939c98e3cc056ed73b036e274a478b5c12c09125` |
| `v4.2.2` | `3d0ab8e515cd79fa703ee62138789b00f3c8f474` |
| `v4.2.3` | `57eebece9d77ba58a55c3045e39789976ad2764b` |
| `v4.3.0` | `39941c2815581eadf948ab8e4e90d82eb1c52207` |
| `v4.3.1` | `9635e1bac225378fd120e32f0a6f89512da46069` |
| `4.4.0` | `1cdd18bdaf2d21c2e4011d7c012f02cf97844753` |
| `4.5.0` | `67a5d3320c413773ae373c76efe84b065dc4e6a4` |

这些 SHA 仍是有效历史引用；tag 名不会被重新解释或复用。新的永久发布线从
正式 `v4.0.0` 开始。
