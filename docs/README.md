# docs/

仓库的版本策略、发布政策与验收证据统一收在本目录；根目录只保留
README / CHANGELOG / AGENTS 三个生态惯例文档。

| 文件 / 目录 | 内容 |
| --- | --- |
| [VERSIONING.md](VERSIONING.md) | Semantic Versioning、源码 tag 与 GitHub Release 政策（版本号唯一源是 `pet/version.py::APP_VERSION`）。 |
| [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) | Portable 包内第三方组件的法律声明；`tools/build_release.py` 打包时从这里复制，缺失即构建失败。 |
| [acceptance/](acceptance/) | 各轮实机验收记录（监控、桌面激活、DSH 插件等），按主题命名 `ACCEPTANCE-*.md`。 |

日常开发文档（使用方式、架构边界、开发契约）分别见根目录
[README.md](../README.md) 与 [AGENTS.md](../AGENTS.md)；测试与 blocking
benchmark 在 [`tests/`](../tests/)。
