# chanapp

自用缠论分析 web app（公开版）。v1.6.3 内置固定版 chan.py 计算结构和原生形态买卖点，支持严格/宽松成笔标准，前端 lightweight-charts
（本地 vendor，无构建）。

功能全景：30分/60分/日线 × A股指数/个股；自选股 + 交易时段自动刷新 + 后台预热；
笔/段双级别信号、雏形虚线、通道线、原生多类型依据卡、MACD 副图、K 线悬停
OHLC 图例；AI 完全分类面板（贝叶斯推理结构：先验/证据/后验，无概率数字；
配置 `LLM_API_KEY` 后启用，无 key 时回落静态样例）；日/夜双主题、多周期信号摘要、
副图 MACD/KDJ/RSI/BOLL 切换、主图 MA 多选开关。

成笔标准（严格/宽松）和提示范围（标准/扩展）独立切换并记忆，默认严格+扩展。扩展仅放开笔级中枢数量门槛；原生力度比作信息展示，不作硬过滤。全部点保留类型、笔/段级别及确认状态，图表与 AI 按四组合规则身份隔离。通道投影不参与价格轴自动缩放。

规则开关位于顶栏；三周期摘要固定为一行，长内容悬停查看。底栏仅保留数据摘要，缓存状态收进名称旁标记。

AI 仅通过刷新按钮触发；自动刷新和切换不发送分析请求，手动请求仍可复用后端缓存。

## 数据说明

**本仓库不含实时数据接入层**（数据源适配为作者私有实现，未公开）。内置演示数据
（`chanapp/engine/demo_data/`，静态历史快照）开箱即可跑通页面与测试：

- 演示覆盖：`sh000001` 日线、`sh000688` 30分钟、`sz399006` 30分钟；其余代码/周期返回 502
- 未安装适配层时，`GET /api/quotes`、`/api/f10`、`/api/search` 返回 503，主图链路不受影响

接入自有数据源：替换 `chanapp/engine/data.py`，保持 `get_bars(code, freq)` 冻结契约
（返回 `{code, freq, bars, source, fqf, fetch_time, degraded, degraded_note,
from_cache, cache_ttl}`，bars 元素 `{dt,open,high,low,close,volume}`，日线 dt
`YYYY-MM-DD`、分钟 `YYYY-MM-DD HH:mm`），上层无需改动；`engine/display_feed.py`、
`engine/search.py` 两个可选适配层按同名将模块放入 `chanapp/engine/` 即自动启用。

## 启动

```bash
# 首次：建 venv 装依赖（python3.12）
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 起服务（仓库根目录即运行目录）
.venv/bin/python -m uvicorn chanapp.api.main:app --host 127.0.0.1 --port 8899

# 浏览器打开 http://127.0.0.1:8899/
```

## 测试

```bash
.venv/bin/python -m unittest discover -s chanapp/tests -v
```

## 口径声明

- MACD(12,26,9)，hist = 2×(DIF−DEA)（A股软件通行口径）。
- 形态规则：chan.py 固定提交 `429d6ed3043e27c93a003ba2b10e70a05575e1f5`；strict/relaxed 仅改变 `bi_strict`。标准提示其余采用上游默认，扩展提示将笔级 min_zs_cnt 设为 0；两者都不强制 MACD 力度比例过滤；形成中点可能移动或消失。图上端点时间不是首次可识别时间。
- 用户可见术语与日期：中枢上沿/下沿；时间轴刻度 MM/DD（分钟级日界 MM/DD、日内
  HH:MM，年界 YYYY/MM/DD），十字线标签 YYYY/MM/DD（分钟级带 HH:MM）。
- AI 面板：环境变量 `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_MODEL`（示例见 `.env.example`）。

## License

MIT（`LICENSE`）。前端图表库 lightweight-charts © TradingView，Apache-2.0
（见 `chanapp/web/vendor/` 文件头）。
