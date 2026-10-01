# AGENTS.md

个人缠论分析 Web 应用。安装、demo、测试命令见 README；配置、数据契约、周期与分析、运维、支持与限制见 `docs/`。

- **布局**：仓库根是运行目录，所有命令在仓库根执行。Python 包在 `chanapp/`：`api/`（FastAPI 路由）、`engine/`（门面 `data.py`、K 线模块 `kline/`、显示层 `display_feed.py` 与 `feeds/`、搜索 `search.py`、AI `llm.py`）、`web/`（前端）、`tests/`、`samples/demo/`（随包样本）、`scripts/`。在线来源适配在 `chanapp/engine/kline/providers/`，安装清单是其中的 `catalog.py`。
- **冻结契约**：`chanapp/engine/data.py:get_bars` 的签名与返回结构字段只增不改义；K 线换源或加源只改 `chanapp/engine/kline/`。契约与接入方式见 `docs/data-contract.md`。
- **测试**：改代码后在仓库根跑 `COLLECTOR_ENABLED=0 python -m unittest discover -s chanapp/tests`；改了 `chanapp/web/` 还要跑 `bash chanapp/scripts/test_js.sh`。provider 单测用 `chanapp/tests/fixtures/` 里的录制数据，不访问外网；真实源冒烟脚本不进 `chanapp/tests/`。
- **凭据**：只经环境变量传入，值不进代码、配置、日志、文档与提交。变量名见 `docs/configuration.md`。
- **提交**：提交信息用 Conventional Commits，由 `chanapp/scripts/git-hooks/commit-msg` 校验；安装了 gitleaks 时，提交前按仓库根 `.gitleaks.toml` 扫描暂存内容与提交信息里的凭据和本机路径；它不识别姓名、邮箱等一般个人信息，提交前自己检查。hooks 的启用方式见 `CONTRIBUTING.md`，不要用 `--no-verify` 绕过。
- **文档**：行为或配置变化时同步更新 `docs/` 对应页面。
