# chanapp

个人缠论分析 Web 应用。后端是 Python 3.12 / FastAPI，前端是原生 JavaScript 加随包的 lightweight-charts（不依赖外部 CDN）；缠论的笔、线段、中枢与买卖点由固定版本的 chan.py 计算。仓库附带一份可离线运行的真实样本；自己的历史数据可以用 CSV 导入；持续更新的数据由已适配的在线来源采集，需要使用者自行开通并提供凭据。

主要功能：

- 30 分、60 分、日线、周线图表，A 股个股可切换前复权与不复权；
- 笔、线段、中枢与买卖点，严格／宽松成笔与标准／扩展提示两组预设，依据卡与多周期共振摘要；
- MACD、KDJ、RSI、BOLL 副图，自选与标签，搜索查看记录，日夜主题；
- 展示周期在页面勾选并保存在服务端，当前数据合成不了的周期注明原因；
- 手动触发的 AI 联合分析（需要自备模型服务 key）。

## 安装

仓库根就是运行目录，Python 包在 `chanapp/` 子目录。需要 Python 3.12，支持 macOS 与 Linux（Windows 不支持）；跑前端测试另需 Node.js。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 离线 demo

随包样本在 `chanapp/samples/demo/`：中际旭创 `sz300308` 与上证指数 `sh000001`，2025-09-24 至 2026-09-24，各 243 根日线和 11,664 根 5 分线。`manifest.json` 记录输入语义、覆盖范围和各文件的 SHA-256。

```bash
# 先确认没有设置 CHANAPP_INSTANCE_CONFIG、CHANAPP_CACHE_DIR、WATCHLIST_PATH、VIEW_LOG_PATH、ANALYSIS_CACHE_DIR
.venv/bin/python -m chanapp.engine.kline.seed_demo .cache/demo
CHANAPP_INSTANCE_CONFIG=.cache/demo/instance.json .venv/bin/python -m uvicorn chanapp.api.main:app --host 127.0.0.1 --port 8899
```

浏览器打开 `http://127.0.0.1:8899/`。初始化先核对样本校验值，再经正式的导入入口写入实例目录 `.cache/demo/`；重复执行不增加行情行、不覆盖个人自选与周期偏好。

demo 模式不联网：不采集、不补历史、不调用 AI，状态栏显示「历史截止于某日」。默认自选只有中际旭创，可以在本地搜索上证指数后添加。样本只有 243 根日线，少于 520 根分析窗口，页面会明确提示且不补齐；没有港股、F10、资金流与 AI 样本。

## 导入自己的历史数据

已有的历史 K 线可以写成规定格式的 CSV，用命令导入当前实例的事实库，然后在 demo 模式下查看：

```bash
CHANAPP_INSTANCE_CONFIG=path/to/instance.json .venv/bin/python -m chanapp.engine.kline.ingest FILE.csv
```

格式、校验规则和一个可以直接运行的完整例子见 [数据契约 · CSV 导入](docs/data-contract.md#csv-导入)。

## 真实模式（在线来源）

真实模式会按交易时段采集自选股的盘前、盘中、定稿与历史数据，行情快照、F10 与搜索也改为在线取数。仓库已带在线来源的适配代码（`chanapp/engine/kline/providers/`，安装清单为其中的 `catalog.py`；显示层与搜索在 `chanapp/engine/display_feed.py`、`chanapp/engine/feeds/`、`chanapp/engine/search.py`）。

目前在用的数据源：A 股 麦蕊智数（mairui）、BaoStock、通达信（pytdx）、东方财富、腾讯；港股 长桥（Longbridge）、Yahoo Finance、东方财富、腾讯。

使用在线来源需要使用者自行开通，并在环境变量里提供凭据；项目不代供数据。开启真实模式需要两样东西：

1. **实例配置**：一份写明 `"mode": "real"` 的 JSON，由 `CHANAPP_INSTANCE_CONFIG` 指定；各市场的来源、分钟粒度、额度与实例目录可以省略而取默认。字段、合法值与拒绝规则见 [配置](docs/configuration.md)。
2. **凭据**：所选来源声明的环境变量，变量名见 [配置 · 来源凭据](docs/configuration.md#来源凭据)。启动时逐个检查，缺失时只报变量名并拒绝启动。凭据不要写进配置文件或仓库。

在已有来源之外再接入别的来源，见 [数据契约 · 接入其他在线来源](docs/data-contract.md#接入其他在线来源)。部署、备份恢复与状态检查见 [运维](docs/operations.md)。

## 测试

```bash
COLLECTOR_ENABLED=0 .venv/bin/python -m unittest discover -s chanapp/tests
bash chanapp/scripts/test_js.sh
```

第一条是全量 Python 测试，`COLLECTOR_ENABLED=0` 让测试不启动后台采集；第二条用 node 逐个运行前端逻辑测试。provider 测试使用 `chanapp/tests/fixtures/` 里的录制数据，测试不访问外网。

装有 playwright-cli 时，`bash chanapp/scripts/test_browser.sh` 另起一个离线 demo 服务，在真实浏览器里走一遍页面验收（不拦截业务接口），结束后自行清理。

## 文档

| 想了解 | 读这里 |
| --- | --- |
| 实例配置、环境变量、demo 与真实模式 | [配置](docs/configuration.md) |
| 门面与接口返回字段、令牌、新鲜度、CSV 导入、接入其他来源、事实库查询 | [数据契约](docs/data-contract.md) |
| 展示周期勾选、共振与 AI 的周期组合、缠论规则预设 | [展示周期与分析](docs/periods-and-analysis.md) |
| 启动、部署、备份恢复、状态检查、凭据更新、冷备切换 | [运维](docs/operations.md) |
| 支持清单、默认额度、在用数据源与来源能力要求、已知限制 | [支持清单与限制](docs/support-and-limits.md) |

## 计算与许可证

MACD 参数 (12, 26, 9)，`hist = 2 × (DIF − DEA)`。缠论计算固定使用 chan.py 提交 `429d6ed3043e27c93a003ba2b10e70a05575e1f5`；严格／宽松只改变成笔严格度，扩展提示只放开笔级买卖点的中枢数量门槛，MACD 力度只作展示。形成中的点位可能移动或消失，图上端点时间不是首次可识别的时间。详见 [展示周期与分析](docs/periods-and-analysis.md#缠论规则预设)。

本项目以 MIT 许可证发布（[`LICENSE`](LICENSE)）。chan.py 子集为 MIT 许可证，来源与逐文件校验见 `chanapp/engine/chanpy_vendor/`；lightweight-charts © TradingView，Apache-2.0，见 vendor 文件头。

## 参与贡献

参与开发前请先读 [CONTRIBUTING.md](CONTRIBUTING.md)；安全问题的报告方式见 [SECURITY.md](SECURITY.md)。
