# dsh-deskpet

DeskPet 桌宠的 DeepSeek Harness（DSH）插件。插件本身只是一个**安装与启动载体**：激活时确保 DeskPet Windows 便携版存在（缺失时从官方 GitHub Release 下载并做 SHA-256 校验），然后启动它。DeskPet 对 DSH 的监控方式与手动安装时完全一致——被动只读 `$DSH_HOME/sessions` 下的会话日志，本插件不读取、不写入任何会话数据，也不触碰 `$DSH_HOME/.credentials.yaml` 等 DSH 私有状态。

## 安装

```sh
# 本地路径（开发/验收）
dsh plugin --profile desktop add ./dsh-plugin

# GitHub（建议固定 tag；git 安装会执行 prepare，需在 profile 的
# pnpm-workspace.yaml 中 allowBuilds: dsh-deskpet: true）
dsh plugin --profile desktop add github:invincible-summer/Deskpet-For-Agents#v4.4.0
```

安装 Desktop 插件的标准流程：先启动一次 DSH Desktop 完成初始化，**完全退出**应用后执行上面的命令，再重新打开 Desktop。

本包同时满足 npm 发布要求；发布后可直接 `dsh plugin add dsh-deskpet`。

## 行为

- DeskPet 安装位置：`$DSH_HOME/deskpet/`（默认 `~/.dsh/deskpet`）。
- 下载源：`invincible-summer/Deskpet-For-Agents` Releases 的
  `DeskPet-windows-x64-portable.zip`，按同目录 `SHA256SUMS.txt` 校验后
  解压。校验或解压失败则放弃启动（fail-closed），下次 DSH 启动时重试。
- DSH 每次启动时，若 DeskPet 未在运行则拉起；DeskPet 自带单实例互斥锁，
  重复启动会被它自己安全去重。
- DSH 退出或本插件被禁用/卸载时**不会**结束 DeskPet：它是常驻桌宠，
  同时监控所有受支持的 Agent，不只 DSH。
- 非 Windows 宿主上是记日志的 no-op；任何失败都只记日志，绝不影响
  DSH 宿主进程。
- 安装/启动过程在 `$DSH_HOME/deskpet-installer.log` 留有有界日志
  （≤64 KiB，仅插件自身命名空间），排障时优先查看。
- 解压使用 Windows 自带 bsdtar（按 `System32` 绝对路径调用，避免 PATH
  解析到不支持 ZIP 的 GNU tar；命令行只用相对路径，规避盘符冒号被
  解析成远程主机）。

## 配置（可选）

在 profile 的 `cordis.patch.yml` 中按 `id: deskpet` 覆盖整行 `config`
（上游语义：整体替换、不做深合并，省略的键回落到下方默认值）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `autoStart` | `true` | DSH 启动时自动拉起 DeskPet；`false` 时插件不做任何事 |
| `exePath` | `''` | 指向已安装的 `DeskPet.exe` 绝对路径；设置后跳过托管下载 |
| `releaseTag` | `'latest'` | 从哪个 GitHub Release 安装，如 `v4.0.0` |

## 卸载

`dsh plugin --profile desktop remove dsh-deskpet` 只移除插件本身，
保留 `$DSH_HOME/deskpet`（DeskPet 程序）与 `%LOCALAPPDATA%\DeskPet`
（DeskPet 用户数据）。不再需要时手动删除这两个目录即可。

## 兼容性

`engines.dsh: >=0.2.0 <0.3.0`（上游以 `engines.dsh` 做安装期兼容检查）。
插件为纯 JS（上游不编译 TypeScript 插件）、零运行时依赖，仅使用 Node
内置模块与 Windows 自带的 `tar.exe`（Windows 10 1803+）。
