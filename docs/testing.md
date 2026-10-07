# 流程覆盖与验证边界

安装与运行命令见 [CONTRIBUTING](../CONTRIBUTING.md#用户流程验收)。测试定义是断言的权威来源；本页说明入口与证据边界，不代表任何一次运行已经通过。

## 验证方式

- **真实 demo**：每次运行创建独立临时实例并选择随机端口；浏览器操作真实页面，经真实 HTTP API 写入该实例，随包历史样本代替在线行情。用例使用新的 browser context，常规流程 fixture 重置周期和自选。recent views 与缓存在一次运行内共享，用例显式创建所需查看记录。自选、标签、周期偏好等副作用通过页面重载与 API 读取确认。
- **外部 fixture**：浏览器、应用 API、采集与持久化保持真实；在 provider 和 LLM 外部边界提供固定数据，验证在线模式契约。它证明应用行为，不证明真实供应商或模型服务当前可用。
- **前端 mock**：旧 UI 回归拦截业务 HTTP API，检查布局、键盘、故障恢复和陈旧响应处理。它是前端回归，不能证明端到端持久化。

## 用户入口覆盖

测试文件均在 `chanapp/tests/`；`e2e/existing-flows.e2e.ts` 将既有浏览器测试接入同一运行器。

| 用户入口与结果 | 测试位置 | 证据边界 |
| --- | --- | --- |
| 打开 demo、默认图表、历史截止、短样本与缺少资料说明 | `browser_demo_offline.js` | 真实 demo |
| 搜索空结果、键盘选择、取消、候选加入、页头加入、移除、最近查看 | `e2e/watchlist.e2e.ts` | 真实 demo |
| 自选星标排序与撤销、标签保存／取消／清空、重载持久化 | `e2e/watchlist.e2e.ts` | 真实 demo |
| 两支样本的四个周期、前复权／不复权、指数复权边界 | `browser_demo_offline.js`、`e2e/chart-controls.e2e.ts` | 真实 demo |
| 展示周期取消／保存／清空、冲突、旧分钟链接 | `e2e/periods.e2e.ts` | 真实 demo |
| 首次选择、来源能力变化、跨页面同步、采集与重启 | `browser_period_selection_e2e.js` | 外部 fixture |
| 成笔标准、提示范围、日夜主题与偏好持久化 | `e2e/chart-controls.e2e.ts` | 真实 demo |
| 六个均线按钮状态、四种副图指标及样本数值、周期切换 | `e2e/chart-controls.e2e.ts` | 真实 demo；数学边界另见 `test_indicators.js` |
| 信号依据卡、共振详情、返回焦点、周期变化关闭旧详情 | `e2e/chart-controls.e2e.ts` | 真实 demo |
| 拖动历史、十字线、主副图同步、侧栏与窄屏布局 | `browser_demo_offline.js` | 真实 demo；部分图表观测使用只读探针 |
| 重拉成功、demo AI 禁用、无后台轮询、无外连 | `browser_demo_offline.js`、`e2e/periods.e2e.ts` | 真实 demo；服务端同时记录外连尝试 |
| AI 周期组合、展示周期与分析周期的关系 | `browser_period_selection_e2e.js` | 外部 fixture |
| busy、失败重试、陈旧令牌、详情焦点、侧栏深滚动、减少动态效果 | `browser_ui_regression.js` | 前端 mock |
| 初始化 demo、CSV 导入、实例读取与自检命令 | `test_demo_sample.py`、`test_kline_ingest.py`、`test_instance_reading.py`、`test_selfcheck.py` | Python 测试，具体公共入口以各用例为准 |
| HTTP 数据契约、在线行情与港股、F10／资金流、LLM 解析与缓存 | Python API／provider 测试 | 录制数据或外部边界替身，非真实在线验收 |

demo 不提供港股、实时行情、F10、资金流与 AI 样本。部署、生产恢复、供应商连通性及实际模型质量需要独立任务与环境，不能从本套件通过推断。

## 证据与维护

默认结果为 `.e2e/report.json`，截图与 trace 由报告指向。既有流程的补充记录在 `.e2e/logs/`；离线画面在 `tmp/demo-offline-browser/demo.png`。定向运行可用 `--output .e2e/<name>` 分开保存报告；辅助日志也会写入该目录的 `logs/`。旧离线截图路径固定，比较多次运行时另存需要保留的截图。

失败时保留命令、退出码、失败断言、对应截图／trace。修复实际缺陷时保留修复前失败和修复后通过的证据。清理临时实例后保留这些产物。应用改动后按 [功能地图](../.agents/skills/verify-chanapp/features/README.md) 核对入口；可用 `/maintain-verification-skill` 维护地图。

GitHub Actions 的 browser job 执行完整流程，包含三组既有浏览器验收；单 worker、零重试。每个提交的 `browser-evidence-<SHA>` artifact 保留 14 天，包括报告、运行器截图／trace、补充日志和离线截图。上传步骤在测试失败后仍执行，并显式包含 `.e2e` 隐藏目录中的指定产物；临时实例状态不在上传范围。长期追溯保留提交 SHA 和 run 链接；artifact 过期后须按需要另存证据。

副图读数共用浮点尾差显示处理，覆盖 MACD、KDJ、RSI 和 BOLL；它只格式化显示文本，不修改指标数组。这不是通用十进制半入算法。固定样本的四组完整读数由 `e2e/chart-controls.e2e.ts` 验证，其中 BOLL 中轨 `884.955` 显示为 `884.96`。

本套件采用主流 Playwright 浏览器驱动与独立实例隔离，按 [Playwright 最佳实践](https://playwright.dev/docs/best-practices) 从用户行为断言并等待可观察状态。新采用的 [tester-army/e2e](https://github.com/tester-army/e2e) 负责确定性测试组织与证据记录；当前用例通过精确操作执行，无模型步骤。
