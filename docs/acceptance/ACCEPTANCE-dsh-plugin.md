# 2026-11-08 DSH 插件分发渠道实机验收

环境：Windows 10 (26200)，真实 DeepSeek Harness Desktop **0.2.1-rc.2**
（NSIS 安装于 `D:\dsh`，自带 CLI `resources/runtime/cli/bin/dsh.cmd`），
`$DSH_HOME` 为默认 `~/.dsh`，desktop profile 已初始化且装有多个第三方
插件（`dsh-claude-style`、`@linxin666/dsh-pet` 等，证明第三方插件生态
在该版本可用）。DeskPet 源码仓库 4.4.0（本轮变更），监控逻辑零改动。

上游对齐：deepseek-harness master @
`fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`（0.2.2-rc.1，2026-11-04）。

## 自动化

- `node --check dsh-plugin/index.js` 通过；`node -e import(...)` ESM
  加载冒烟通过（导出 `apply`/`name`）。
- 新增 `tests/test_dsh_plugin_layout.py` 18 项契约（清单/补丁/版本单源
  同步/卫生/文档同步/实机修复锁定）通过。
- 完整套件 `python -m unittest discover tests -p "test_*.py"`：
  **940 tests / 61.1s / OK**（原 923 + 新 17 + 实机修复断言 1）。
  `compileall agents pet actions tools` 通过。
- 四项 blocking benchmark 全部 PASS（Monitor 5000 ticks 11.2s；
  Desktop sources 13.9s；Presentation；UI architecture）。

## 实机端到端（全部在本机真实 DSH Desktop 上执行）

1. **安装**（Desktop 完全退出状态）：
   `dsh.cmd plugin --profile desktop add D:\...\dsh-plugin`
   → `+ dsh-deskpet 4.4.0`（pnpm 11.28.5，18.7s）。`engines.dsh
   ">=0.2.0 <0.3.0"` 在 0.2.1-rc.2 上通过兼容检查。安装后
   `dependencies` 与 `dsh.profile.bundles` 均自动登记（无需 profile
   patch 行，与上游"安装即启用"文档一致）。
2. **首启**：启动 Desktop → 宿主（`--expose-internals ...dsh-desktop-host`）
   插件图加载 → apply：从 GitHub Release latest 下载
   `DeskPet-windows-x64-portable.zip`（v4.0.0，108,470,930 字节，19.5s）→
   按 `SHA256SUMS.txt` 校验 SHA-256 通过 → bsdtar 解压 → 原子改名安装到
   `~/.dsh/deskpet/` → **detached 拉起 `DeskPet.exe`（pid 3296），桌宠
   正常出现**。全程 `$DSH_HOME/deskpet-installer.log` 留痕。
3. **幂等**：再次重启 DSH → 日志 `already installed`（不重复下载）→
   再次拉起被 DeskPet 自身单实例互斥锁去重，进程数恒为 1。
4. **移除**：退出 Desktop 后 `dsh.cmd plugin --profile desktop remove
   dsh-deskpet` → dependencies/bundles 双双移除；重启 DSH 后插件不再
   加载（日志无新行）、**DeskPet 不受影响继续存活**（契约：插件不杀
   桌宠）、宿主正常启动。
5. **现场恢复**：验收后关闭测试拉起的 DeskPet 与 DSH Desktop，删除
   `~/.dsh/deskpet`、`~/.dsh/deskpet-installer.log` 与下载样本；
   `%LOCALAPPDATA%\DeskPet` 用户数据全程未触碰。

## 实机发现并修复的两个 Windows 坑（已加契约断言锁定）

1. **bsdtar 盘符路径被解析成 rsh 远程主机**：`tar -xf C:\...` 报
   `Cannot connect to C: resolve failed`。修复：以 workDir 为 cwd、
   命令行只出现相对路径（归档 basename + 相对 `-C extracted`）。
2. **PATH 解析到 Git 的 MSYS GNU tar**：宿主环境 `Get-Command tar` →
   `<Git>\usr\bin\tar.exe`（GNU tar 读不了 ZIP，报
   `Skipping to next header`）。修复：按 `SystemRoot\System32\tar.exe`
   绝对路径调用（本机实测 bsdtar 3.8.9 对真实发布 ZIP 解压 exit=0）。

另：宿主 stderr 平时不可见（仅崩溃时留 64Ki 尾巴），插件因此增加
`$DSH_HOME/deskpet-installer.log` 有界日志（≤64KiB，仅自身命名空间），
本轮排障即依赖它完成。

## NOT_TESTED / 边界

- npm 发布渠道未实测（包结构 npm-ready，发布另行决定）；git 地址
  安装（`github:...#tag`，需 allowBuilds）未实测，文档已注明。
- Desktop Plugins 页面（GUI "Custom address" 安装路径）未实测；CLI
  `dsh plugin add` 与其共用 plugin-manager，行为应一致。
- 插件安装的是最新已发布 Portable（当前 v4.0.0）；4.4.0 源码版本的
  Portable 未随本变更发布（VERSIONING.md 规则）。
- WSL 侧 DSH 会话监控不受插件影响（监控代码零改动，由既有 940 项
  套件覆盖）。
