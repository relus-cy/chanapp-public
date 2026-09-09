# AGENTS.md

自用缠论分析 web app（公开版）。启动/测试命令以 README.md 为准
（仓库根含 `chanapp/` 包，运行与测试一律在仓库根目录执行）。

## 约定

- 改动后跑全量 unittest（命令见 README）；单测用录制 fixture，不打外网。
- 本仓库不含实时数据接入层：`chanapp/engine/data.py` 是演示数据门面，
  `get_bars(code, freq)` 的签名与返回结构是冻结契约；接入自有数据源时保持契约不动。
- `engine/display_feed.py`、`engine/search.py` 为可选适配层：api 层做可选导入，
  模块不存在时对应路由返回 503，不得改成硬依赖。
- `api/supply.py` 的供数管理适配层也是可选；公开演示返回受控 503，静态界面和演示行情仍可访问。
- 公开演示不含版本化缓存层（无 `engine/cache_store.py`）：不启动后台预热与保留清理。
- `.cache/` 是本地数据缓存（已 gitignore）。
