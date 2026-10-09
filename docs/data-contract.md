# 数据契约

分析用的 K 线只有一种真相：事实库里的原始事实（不复权的日线与分钟线，日线带交易所参考前收和停牌标志）。前复权、30/60 分钟与周线、当日日线、视图令牌都在读取时计算。上层只经 `chanapp/engine/data.py` 门面读取；外部数据只能经两条入口写入：CSV 一次性导入和在线来源 adapter（raw provider），两者共用同一套准入，由采集器作为唯一写者提交。

本页依次说明：门面怎么读、返回什么；令牌、新鲜度与数据状态；HTTP 接口里与 K 线有关的部分；CSV 导入格式与例子；怎样在已有来源之外接入其他在线来源；怎样只读查询事实库。下文命令都在仓库根执行，Python 环境按 [README](../README.md#安装) 准备。

## 门面函数

`chanapp/engine/data.py` 的函数签名与返回结构是冻结契约：字段只增不改义。

| 函数 | 说明 |
| --- | --- |
| `get_bars(code, freq="day", *, adjust="qfq") -> dict` | 单周期首页（最近 520 根）。`freq` 取 `week`、`day`、`m60`、`m30`、`m15`、`m5`，`adjust` 取 `qfq`（前复权）或 `raw`（不复权），其他值抛 `ValueError`。指数恒按不复权返回。没有可服务数据时抛 `DataUnavailable` |
| `get_bars_bundle(code, freqs, *, adjust="qfq", primary=None, with_quote=False) -> {freq: dict 或 None}` | 多周期在同一次读事务里读取（图表、共振与 AI 共用）。标的没有任何事实时该周期为 `None`。`with_quote=True` 时另含 `"quote"` 键：同一次读事务的 `quote` 报价（无事实为 `None`），`/api/chart` 的 A 股应答用它 |
| `get_bars_history(code, freq, before, limit=520, *, adjust="qfq", token=None) -> dict 或 None` | 历史分页，取 `before` 之前（不含）至多 `limit` 根。先比令牌，不符或缺失抛 `TokenMismatch`（其 `.token` 为当前令牌）；令牌相符的空页表示历史到头 |
| `refetch_window(code, freq="day") -> dict` | 手动重拉，返回 `{"status": ...}`：`ok`、`partial`、`failed`、`busy`、`disabled`（demo 或采集器关闭） |
| `quote(code) -> dict 或 None` | 报价 `{price, price_time, price_label, pc, pct, limit_up, trade_date, stale}` |
| `status(codes) -> dict` | 运行状态，同 `/api/status` |
| `configure_instance()` | 上下文管理器：加载并校验实例配置、初始化实例目录。应用启动时调用；脚本直接使用门面时也要先进入它，才会按实例配置选择模式与数据路径 |

`m15`、`m5` 只在门面层保留；页面与 HTTP 接口只提供 30 分、60 分、日线、周线。所请求周期细于该市场的分钟事实粒度时（例如港股事实为 m30 时的 m15），返回空 `bars` 与 `unsupported` 提示，不从粗粒度伪造细周期。

### 返回字段

`get_bars`、`get_bars_bundle` 的每个周期、`get_bars_history` 返回同一结构：

| 字段 | 含义 |
| --- | --- |
| `code`、`freq` | 请求参数 |
| `adjust` | 实际生效的模式；指数恒为 `raw` |
| `bars` | `[{dt, open, high, low, close, volume}]`，按时间升序。日线、周线的 `dt` 为 `YYYY-MM-DD`，分钟为 `YYYY-MM-DD HH:MM`，一律是区间末端标签（A 股上午最后一根标 11:30）。`volume` 恒以股计 |
| `token` | 视图令牌，见下文 |
| `source` | 页内最新一根 bar 所属批次的来源名；CSV 导入的批次为 `import` |
| `degraded`、`degraded_note` | 页内有任一 bar 来自冷备来源时为 `true` 与 `冷备来源`，否则为 `false` 与空串 |
| `fqf` | 口径标签：`前复权`、`不复权`、`前复权（供应商口径）` |
| `fetch_time` | 判定新鲜度所用数据集最近一次成功提交的时刻（本地 ISO 时间带偏移）；不是行情时间。从未提交为 `null` |
| `from_cache` | 本次经同步首次取数为 `false`，否则为 `true` |
| `stale`、`stale_age_s` | 新鲜度，见下文。`stale_age_s` 只在 `stale=true` 且有过提交时为距最近提交的秒数，否则为 `null` |
| `data_version` | `bars`、令牌、`adjust`、`source` 的 SHA-256，计算缓存用它识别输入 |
| `incomplete_days` | 质量标记。分钟周期为 `[{date, kind, slots?}]`，`kind` 取 `forming`（过去交易日仍有未收盘 bar）、`incomplete`（槽位不足或整日无分钟）、`pending_review`（分钟与日线核对待核验）；周线为缺日或日历未知的周；日线恒为空 |
| `volume_unit` | 恒为 `share` |
| `coverage` | `{qfq_from, qfq_through, stop_reason, data_status}`。前三项只在 A 股个股前复权时有值：前复权可用区间，以及因子链停在哪里的原因（`unknown_calendar`、`gap`、`missing_pc`、`bad_factor`）；`data_status` 见下文 |
| `notices` | `[{code, text}]`，见下表 |
| `has_more`、`oldest_dt` | 是否还有更早的可服务 bar；本页最早的 `dt` |

`notices` 的取值：

| `code` | 默认文案 | 出现条件 |
| --- | --- | --- |
| `qfq_from` | 前复权自 X 起可用 | A 股个股前复权因子链在 X 之前断开；更早的历史只能在不复权模式查看 |
| `today_unconfirmed` | 今日除权信息待确认，前复权显示至 X | 交易日当天的参考前收还没确认，前复权只显示到上一个确认日 |
| `backfill_pending` | 更早分钟历史加载中 | 分钟周期，该标的还有未补完的历史缺口 |
| `structure_short` | 结构输入尚未补齐 | 首页不足 520 根且没有更早数据；demo 下文案改为「历史样本仅 N 根，少于 520 根分析窗口；demo 不补齐」 |
| `unsupported` | 该市场暂不提供 | 所请求周期细于该市场的分钟事实粒度 |

### 复权与周期口径

- **A 股个股前复权**：等比因子链，因子 = 除权日参考前收 ÷ 前一有效交易日收盘。缺参考前收、日历未知、缺日或因子非法时停链，更早的部分不以前复权提供（不以因子 1 冒充）。
- **指数**：恒为原始价。
- **港股前复权**：只能由港股在线来源提供的供应商前复权序列给出（`fqf` 为「前复权（供应商口径）」）。CSV 导入的港股数据只有不复权；请求前复权时图表接口返回 502。
- **周期合成**：30 分、60 分由分钟事实按交易所会话钟点分桶聚合（A 股 60 分四桶止于 10:30、11:30、14:00、15:00），不是相邻 N 根拼接；当日日线由当日分钟聚合，收盘定稿后以正式日线为准；周线按自然周聚合日线，标签为该周最后一个有 bar 的交易日，当前周为形成中。
- **桶内取值**：停牌（`trade_state=suspended`）的分钟不参与聚合；桶内有成交的行时，开高低收只取成交量大于 0 的行，成交量为全桶之和；整桶都是零成交时，开高低收取桶内最后一行的价格，成交量为 0。整周停牌不出周线 bar。

## 令牌、409 与形成中

**视图令牌**是以下内容的哈希：标的、周期、生效复权模式、读取规则版本、所依赖数据集的已收盘代次、来源绑定代次、判定日期、前复权锚点与当日确认状态、所读区段的交易日历摘要、事实库运行身份（港股前复权另含供应商缓存版本与状态）。

- 已收盘事实的新增或修订、当日参考前收确认、隔离、切换来源、日历变化、日期翻过、从备份恢复，都会换令牌。
- 盘中尚未收盘的分钟 bar（形成中，`forming`）正常更新不换令牌，所以向左翻页不会被盘中写入打断。
- 首页与分页必须使用同一令牌。令牌不符时，门面抛 `TokenMismatch`，HTTP 返回 **409**，客户端应整窗重载，不要把新旧数据拼在一起。

**形成中**有三处含义：分钟事实的 `state=forming` 是今天尚未收盘的 bar（只能由采集写入，CSV 导入一律记为已收盘）；周线的当前周；缠论结构里尚未确认的笔与点位（`provisional`），它们会随新 K 线移动或消失。

**degraded** 只表示数据来自冷备来源；**stale** 只表示新鲜度。取数失败时服务最后可信的数据，不会自动换用其他来源。

## 新鲜度与数据状态

真实模式下，`stale` 按「距该数据集最近一次成功提交」与交易时段判定：

| 时段 | 判定 |
| --- | --- |
| 交易日会话内 | 分钟数据集距最近提交超过 180 秒为 stale；日线数据集不判 |
| 交易日定稿截止（A 股 21:00、港股 18:30）之后 | 当日未定稿为 stale：没有可读的定稿日线，或分钟仍有形成中 |
| 交易日开盘后到定稿截止之间的会话外时段（午休、收盘后） | 不判，为 `false` |
| 非交易日与交易日开盘前 | 最近一个过去交易日未定稿为 stale |

阈值在 `chanapp/engine/kline/config.py`（`STALE_IN_SESSION_S`、`FINALIZE_DEADLINE`）。demo 模式恒为 `stale=false`。

`coverage.data_status = {phase, day, at}` 与 `bars` 出自同一次读取，页面状态栏据此显示：

| `phase` | 条件 | 页面文案 |
| --- | --- | --- |
| `live` | 交易日开盘到收盘（含午休） | 交易中 · 实时抓取 MM-DD HH:MM |
| `awaiting_final` | 其余时段，最近应完成交易日的数据还没定稿 | 已收盘 · 待定稿 MM-DD HH:MM |
| `final` | 同上，已定稿 | 已收盘 · 历史抓取 MM-DD HH:MM |
| `none` | 所依赖的数据集从未成功提交 | 交易中 / 已收盘 · 暂无数据 |
| `historical` | demo 模式 | 历史截止于 YYYY-MM-DD（`day` 为本图最后一根的日期，`at` 为 `null`） |

## HTTP 接口要点

完整字段以 `chanapp/api/main.py`、`chanapp/api/analysis.py` 为准，这里只列与数据契约相关的行为。

- `GET /api/chart?code=&freq=&adjust=`：`freq` 取 `week`、`day`、`m60`、`m30`；`m15`、`m5` 在读取前返回 400。未勾选或当前市场不能合成的周期返回 400。当前周期首次取数失败返回 502。响应为 `{kline, macd, structure, signals, evidence, channels, resonance, meta, ...}`，`kline` 元素为 `{time, open, high, low, close, volume}`；`meta` 是门面返回体去掉 `bars`，另加 `bars`（根数）、`first_dt`、`last_dt`、`analysis_freqs`、`analysis_tokens`、`analysis_calculation_id`。A 股（`sh`/`sz`）响应另含 `quote`：与 `kline` 同一次读取的门面 `quote` 报价（标的没有事实时为 `null`），页面的价格卡与自选当前行用它，保证与图中末根 bar 同快照；港股不含此键。带弱 ETag，`If-None-Match` 命中返回 304。带 `refetch=1` 时先手动重拉，结果放在响应头 `X-Refetch-Status`。
- `GET /api/chart?...&before=&limit=&token=`：历史分页，`limit` 为 1–2000（默认 520），`token` 取首页的 `meta.token`。令牌不符或缺失返回 **409 `{"detail": "数据已更新", "token": 当前令牌}`**；标的没有事实返回 503。
- `GET /api/analysis?code=&freq=&adjust=&rule_profile=&signal_scope=&tokens=`：`tokens` 是 URL 编码的 JSON 对象，取自图表的 `meta.analysis_tokens`；`freq` 不影响分析内容。按以下顺序判断，命中即返回：
  1. `freq` 为 `m15`、`m5` 返回 400；`week` 等不在 `day`、`m60`、`m30` 内的值由参数校验返回 422；
  2. demo 返回 200 `status: "disabled"`；
  3. 未配置模型（没有 `LLM_API_KEY`）返回 200 `status: "unconfigured"`，不取数；
  4. `tokens` 缺失、无法解析，或周期键集合与本次分析组合不同，返回 409 `{"detail": "数据已更新", "tokens": 当前令牌}`；
  5. 组合内某周期读不出数据集（读取失败或该标的没有事实）返回 502 `分析数据暂不可用`；
  6. 任一令牌值不符返回 409（同第 4 步）；
  7. 令牌相符但某周期没有 bar 返回 502；
  8. 之后才计算结构并调用模型；结构计算失败或模型报错返回 502，模型报错的结果缓存 600 秒。

  以上 409 都不调用模型。
- `GET /api/status`：`{mode, enabled, checked_at, datasets, probes, budget, calendar_export}`。`mode` 为 `demo` 或 `real`；`enabled` 为真表示采集器在运行；`datasets` 逐项给出 `last_commit_at`、`stale`、`stale_judged`（为 false 时表示当前时段采集器不判 stale，`stale` 恒为 false）、`open_gaps`、`known_gaps`（重试 5 次后不再自动补取的缺口）、`pending_review`。
- `GET/PUT /api/periods`：展示周期勾选，见 [展示周期与分析](periods-and-analysis.md)。

## CSV 导入

一次性导入把一份 CSV 写入当前实例的事实库，适合已有的历史数据。导入命令按当前实例配置运行（`CHANAPP_INSTANCE_CONFIG`，未设时按 demo 默认），本身不访问网络。

### 文件格式

UTF-8 CSV（允许 BOM），逗号分隔，首行表头恰好是下面 12 列（顺序不限，不多不少）。一行一根 bar；一个文件可以混合多个标的、日线与分钟线。

| 列 | 日线行 | 分钟行 |
| --- | --- | --- |
| `code` | A 股 `sh`/`sz` 加 6 位数字，港股 `hk` 加 5 位数字；`sh000*`、`sz399*` 为指数 | 同左 |
| `freq` | `day` | 必须等于实例的分钟事实粒度，如 `m15` |
| `dt` | `YYYY-MM-DD`（交易所当地日期） | `YYYY-MM-DD HH:MM`，区间末端标签，必须落在该市场该粒度的槽位上 |
| `open`、`high`、`low`、`close` | 原始价（不复权）；停牌日可留空 | 原始价 |
| `volume` | 成交量；停牌日可留空 | 成交量 |
| `volume_unit` | `lot`（手，100 股）或 `share`（股） | 同左 |
| `amount` | 成交额，可留空 | 同左 |
| `pc` | 交易所参考前收，可留空（A 股个股缺它前复权会停链） | 必须留空 |
| `suspended` | `0` 或 `1`，留空按 `0` | `1` 表示停牌 |

槽位：A 股会话 09:30–11:30、13:00–15:00，m15 槽为 09:45…11:30、13:15…15:00，m5 槽为 09:35…11:30、13:05…15:00；港股会话 09:30–12:00、13:00–16:00，m30 槽为 10:00…12:00、13:30…16:00，收市竞价并入 16:00。

### 处理规则

- 文件至多 64 MiB、50 万行，超出整份拒绝（`too_large`），更多历史分批导入。
- 先整份预检，任何一行有问题就整份拒绝，事实库不写任何行。格式问题的原因码：`bad_header`、`bad_columns`、`bad_code`、`bad_freq`、`not_number`、`bad_flag`、`pc_on_minute`；粒度问题：`freq_mismatch`、`no_minute_facts`（实例只提供日线）；指数问题：`index_hole`；准入问题：`duplicate_key`、`out_of_range`、`off_grid`、`bad_enum`、`non_finite`、`non_positive_price`、`negative_quantity`、`bad_ohlc`。
- 只收已收盘的历史，截止到昨天（按东八区日期）。日线一律记为定稿，分钟一律记为已收盘；今天的数据只能来自在线采集。
- 交易日历：文件里的上证指数 `sh000001` 日线视为首尾之间连续，中间缺的工作日推为休市。所以要导入 A 股，应同时导入覆盖同一区间的 `sh000001` 日线；相邻两日间空档超过 10 个工作日，或某只 A 股有日线而指数缺这一天，都按 `index_hole` 拒绝。没有指数日线时日历未知：周线标 `unknown_calendar`，前复权在未知日处停链。港股的日历只由日线确认开市，不推休市。
- 分钟周期必须等于实例的分钟事实粒度，导入时不聚合、不拆分。
- 重复导入同一文件不产生变化，令牌也不变；同一键已收盘的值不同时进入待核验（`pending_review`），不覆盖原值。
- 成交量单位按 `volume_unit` 原样保存，读取时换算成股。准入拦不住「合法但标错」的单位：把以股计的量标成 `lot` 会放大 100 倍，导入前请自行确认。
- 运行中的应用只在提交时短暂持有写者锁，导入可以与它同时进行；锁被占时报错退出，稍后重跑即可。中途失败时已提交的部分保留，重跑同一文件补齐。

输出：标准输出是一个 JSON 对象，日志在标准错误。成功时按标的、数据集报告 `inserted`（新键）、`revised`、`skipped`（同值未变）、`pending_review` 与 `rejected`。退出码 `0` 成功，`1` 被拒或出错，`2` 用法错误。被拒时最多列出前 100 条问题，`problem_count` 是总数。

### 例子：导入并查看

如果端口 8899 上仍有 demo 服务运行，先在它的终端按 `Ctrl+C` 停止服务。确认当前终端没有设置 `CHANAPP_CACHE_DIR`、`WATCHLIST_PATH`、`VIEW_LOG_PATH`、`ANALYSIS_CACHE_DIR` 路径覆盖，避免读写其他实例。路径规则见 [配置 · 实例目录与数据路径](configuration.md#实例目录与数据路径)。

下面的数据取自随仓库发布的 demo 样本。它包含中际旭创 `sz300308` 与上证指数 2026-09-14 至 09-18 的日线，以及中际旭创 09-18 的 16 根 15 分线。15 分线由样本的 5 分线聚合。

创建独立的 demo 实例目录：

```bash
mkdir -p .cache/csv
```

保存实例配置。A 股分钟事实粒度设为 `m15`：

```bash
cat > .cache/csv/instance.json <<'EOF'
{"mode": "demo", "markets": {"CN": {"minute_fact_freq": "m15"}}, "instance_dir": "."}
EOF
```

保存示例 CSV：

```bash
cat > .cache/csv/example.csv <<'EOF'
code,freq,dt,open,high,low,close,volume,volume_unit,amount,pc,suspended
sh000001,day,2026-09-14,3867.02,3895.51,3867.02,3885.33,458888916.0,lot,779281246497.0,3888.11,0
sh000001,day,2026-09-15,3879.73,3891.62,3858.15,3864.28,440907766.0,lot,763982900036.0,3885.33,0
sh000001,day,2026-09-16,3861.75,3894.66,3842.72,3891.6,459125108.0,lot,871141346852.0,3864.28,0
sh000001,day,2026-09-17,3877.0,3898.84,3866.89,3875.6,452886464.0,lot,868773066850.0,3891.6,0
sh000001,day,2026-09-18,3891.96,3919.67,3888.5,3911.87,485712507.0,lot,994169450166.0,3875.61,0
sz300308,day,2026-09-14,890.0,895.0,866.66,873.0,271830.0,lot,23936800301.0,926.0,0
sz300308,day,2026-09-15,873.38,883.73,858.0,864.01,179554.0,lot,15613591729.0,873.0,0
sz300308,day,2026-09-16,869.02,918.38,866.0,907.8,240218.0,lot,21471490117.0,864.01,0
sz300308,day,2026-09-17,900.3,927.83,892.51,896.0,227875.0,lot,20684420945.0,907.8,0
sz300308,day,2026-09-18,910.0,947.6,893.08,926.43,295055.0,lot,27079558744.0,896.0,0
sz300308,m15,2026-09-18 09:45,910.0,920.0,907.15,907.5,46468.0,lot,4244911432.0,,0
sz300308,m15,2026-09-18 10:00,906.63,912.22,898.88,903.02,24625.0,lot,2226752959.0,,0
sz300308,m15,2026-09-18 10:15,903.02,905.9,897.04,903.63,15501.0,lot,1397386983.0,,0
sz300308,m15,2026-09-18 10:30,903.63,904.9,895.01,896.0,11495.0,lot,1033863756.0,,0
sz300308,m15,2026-09-18 10:45,895.31,899.72,893.08,895.91,12781.0,lot,1145072225.0,,0
sz300308,m15,2026-09-18 11:00,895.55,898.5,893.2,898.39,8410.0,lot,753488427.0,,0
sz300308,m15,2026-09-18 11:15,898.33,909.4,898.33,909.0,15975.0,lot,1445422948.0,,0
sz300308,m15,2026-09-18 11:30,909.86,921.0,909.86,920.76,28820.0,lot,2639274874.0,,0
sz300308,m15,2026-09-18 13:15,922.03,947.6,922.03,943.1,53219.0,lot,4969981927.0,,0
sz300308,m15,2026-09-18 13:30,942.97,944.8,930.0,931.11,21120.0,lot,1977910812.0,,0
sz300308,m15,2026-09-18 13:45,931.41,932.35,928.0,928.14,8656.0,lot,805152768.0,,0
sz300308,m15,2026-09-18 14:00,928.14,929.8,920.8,923.66,11670.0,lot,1078569274.0,,0
sz300308,m15,2026-09-18 14:15,923.86,926.66,921.97,925.19,7009.0,lot,647994646.0,,0
sz300308,m15,2026-09-18 14:30,924.91,926.02,921.58,925.98,6106.0,lot,564148910.0,,0
sz300308,m15,2026-09-18 14:45,925.78,928.78,923.63,926.13,8375.0,lot,775953943.0,,0
sz300308,m15,2026-09-18 15:00,926.1,927.45,925.92,926.43,14825.0,lot,1373672842.0,,0
EOF
```

导入 CSV：

```bash
CHANAPP_INSTANCE_CONFIG=.cache/csv/instance.json .venv/bin/python -m chanapp.engine.kline.ingest .cache/csv/example.csv
```

输出（实际逐字段缩进，这里合并了行）：

```json
{
  "status": "ok",
  "rows": 26,
  "codes": {
    "sh000001": {"day": {"inserted": 5, "revised": 0, "skipped": 0, "pending_review": 0, "rejected": 0}},
    "sz300308": {
      "day": {"inserted": 5, "revised": 0, "skipped": 0, "pending_review": 0, "rejected": 0},
      "m15": {"inserted": 16, "revised": 0, "skipped": 0, "pending_review": 0, "rejected": 0}
    }
  }
}
```

再运行一次，各项变为 `inserted: 0` 与全部 `skipped`。事实库写在 `.cache/csv/data/facts.sqlite`。

启动这个实例的服务：

```bash
CHANAPP_INSTANCE_CONFIG=.cache/csv/instance.json .venv/bin/python -m uvicorn chanapp.api.main:app --host 127.0.0.1 --port 8899
```

在浏览器打开 <http://127.0.0.1:8899/>。

在页面中搜索 `sz300308`，打开中际旭创的图表。日线有 5 根，历史截止于 2026-09-18。30 分线有 8 根，60 分线有 4 根。

如需检查 HTTP 返回值，另开一个终端执行：

```bash
curl -s 'http://127.0.0.1:8899/api/chart?code=sz300308&freq=m60&adjust=raw'
```

#### Python 接口示例

完成上面的 CSV 导入后，可以另开一个终端运行下面的脚本。脚本直接调用门面时，先进入 `configure_instance()`：

```bash
CHANAPP_INSTANCE_CONFIG=.cache/csv/instance.json .venv/bin/python - <<'EOF'
from chanapp.engine import data

with data.configure_instance():
    day = data.get_bars("sz300308", "day")
    print(day["fqf"], day["source"], len(day["bars"]), day["bars"][-1])
    print(day["coverage"]["data_status"], [n["code"] for n in day["notices"]])
    m30 = data.get_bars("sz300308", "m30", adjust="raw")
    print(len(m30["bars"]), m30["bars"][0])
    page = data.get_bars_history("sz300308", "day", "2026-09-16", 2, token=day["token"])
    print([b["dt"] for b in page["bars"]], page["has_more"])
    try:
        data.get_bars_history("sz300308", "day", "2026-09-16", 2, token="old-token")
    except data.TokenMismatch as exc:
        print("TokenMismatch", exc.token == day["token"])
EOF
```

输出：

```text
前复权 import 5 {'dt': '2026-09-18', 'open': 910.0, 'high': 947.6, 'low': 893.08, 'close': 926.43, 'volume': 29505500.0}
{'phase': 'historical', 'day': '2026-09-18', 'at': None} ['structure_short']
8 {'dt': '2026-09-18 10:00', 'open': 910.0, 'high': 920.0, 'low': 898.88, 'close': 903.02, 'volume': 7109300.0}
['2026-09-14', '2026-09-15'] False
TokenMismatch True
```

成交量 295,055 手读出为 29,505,500 股。16 根 15 分线合成 8 根 30 分线，经 HTTP 读 60 分为 4 根。

### 例子：被拒绝的文件

```bash
cat > .cache/csv/bad.csv <<'EOF'
code,freq,dt,open,high,low,close,volume,volume_unit,amount,pc,suspended
sz300308,day,2026-09-14,890.0,895.0,866.66,873.0,271830.0,lot,,926.0,0
sz300308,day,2026-09-15,873.38,883.73,858.0,864.01,179554.0,hand,,873.0,0
sz300308,day,2026-09-16,869.02,900.0,866.0,907.8,240218.0,lot,,864.01,0
sz300308,m15,2026-09-18 09:50,910.0,920.0,907.15,907.5,46468.0,lot,,,0
EOF
CHANAPP_INSTANCE_CONFIG=.cache/csv/instance.json .venv/bin/python -m chanapp.engine.kline.ingest .cache/csv/bad.csv
```

```json
{
  "status": "rejected",
  "problem_count": 3,
  "problems": [
    {"line": 3, "reason": "bad_enum", "detail": "sz300308 day 2026-09-15"},
    {"line": 4, "reason": "bad_ohlc", "detail": "sz300308 day 2026-09-16"},
    {"line": 5, "reason": "off_grid", "detail": "sz300308 m15 2026-09-18 09:50"}
  ]
}
```

退出码为 1。第 3 行单位 `hand` 不合法，第 4 行最高价低于收盘价，第 5 行 09:50 不在 15 分钟槽位上；第 2 行本身合格，也随整份文件一起不写入。

### 随包 demo 的初始化

随包样本在 `chanapp/samples/demo/`。它包含中际旭创 `sz300308` 和上证指数 `sh000001`，覆盖 2025-09-24 至 2026-09-24。每个标的有 243 根日线和 11,664 根 5 分线。

[`manifest.json`](../chanapp/samples/demo/manifest.json) 记录输入语义、覆盖范围和各文件的 SHA-256。

`python -m chanapp.engine.kline.seed_demo DIR` 先按 `chanapp/samples/demo/manifest.json` 核对样本的字节数与 SHA-256，再经同一导入入口写入事实库，并在同一写者锁内写入交易日历与标的名称。它只接受空目录或已有同一份 demo 配置的目录，拒绝目录内的符号链接，要求先清除 `CHANAPP_INSTANCE_CONFIG`、`CHANAPP_CACHE_DIR`、`WATCHLIST_PATH`、`VIEW_LOG_PATH`、`ANALYSIS_CACHE_DIR`。重复执行不新增行情行、不改令牌、不覆盖个人自选与周期偏好。

## 接入其他在线来源

仓库已带 A 股与港股在线来源的适配，代码在 `chanapp/engine/kline/providers/`，安装清单是其中的 `catalog.py`。本节说明怎样在已有来源之外再接入别的来源。

adapter 只负责取数、结构检查、单位与标签归一，输出上文同样的原始行；调度、额度、退避、缺口、定稿和写库都由采集器负责。adapter 不复权，也不跨来源回落。

已有适配可以直接作参考：`chanapp/engine/kline/providers/` 下的 `mairui.py`、`longbridge.py`、`baostock_raw.py`、`pytdx_raw.py`、`yahoo_raw.py`，它们各自声明的能力见 `catalog.py`；对应的单测是 `chanapp/tests/test_kline_provider_*.py`，录制数据在 `chanapp/tests/fixtures/kline_raw/<来源名>/`。

再接入一个来源需要两处改动：

1. **provider 模块**（例如 `chanapp/engine/kline/providers/<名称>.py`）：模块级常量 `CONTRACT_VERSION`（字符串，改变取数口径时更新），以及一个无参构造的类。来源的验证结论按 `CONTRACT_VERSION` 与 `chanapp/tests/fixtures/kline_raw/<来源名>/` 下录制数据的哈希记录，两者任一变化，旧结论即失效。
2. **在安装清单 `catalog.py` 里注册**：在 `REGISTRY` 字典里加一项。能力完整的来源注册后就能在实例配置的 `markets.<市场>.source` 里选用，选中后该市场的全部取数项都以它为主来源，`BINDING_DEFAULTS` 里登记的冷备保持不变。只有想改变产品默认来源，或要把新来源登记为某个取数项的冷备时，才需要改 `BINDING_DEFAULTS`。

### provider 方法

必需方法（`chanapp/engine/kline/providers/raw.py` 的 `RawProvider` 协议）：

| 方法 | 返回 |
| --- | --- |
| `day_history(code, start, end)` | `list[RawDayRow]`，`start`/`end` 为 `YYYY-MM-DD` |
| `minute_history(code, fact_freq, start, end, *, now)` | `list[RawMinuteRow]`，已收盘分钟 |
| `minute_live(code, fact_freq, *, now)` | `list[RawMinuteRow]`，当日分钟，可含 `state="forming"` |

按能力调用的可选方法（不存在时采集器视为「不支持」，跳过且不计失败）：

| 方法 | 用途 | 对应注册能力 |
| --- | --- | --- |
| `calendar(year)` | `list[CalendarRow]`，交易年表 | `calendar=True` |
| `preopen_ref(code, trade_date)` | `RawDayRow` 或 `None`：当日参考前收，`provenance="preopen"` | `preopen=True`（A 股必需） |
| `instrument(code)` | `InstrumentRow`：名称、上市日、退市日、类型 | — |
| `qfq_series(code, freq, start, end)` | 供应商前复权序列，`freq` 为 `day` 或 `m30`，格式见下文 | `vendor_qfq=True`（港股必需） |

行类型在 `chanapp/engine/kline/rows.py`：日线 `RawDayRow(code, trade_date, open, high, low, close, volume, volume_unit, amount, currency, pc, sf, provenance, batch_id)`，分钟 `RawMinuteRow(code, trade_date, slot_end, open, high, low, close, volume, volume_unit, amount, state, trade_state, batch_id)`。`provenance` 取 `preopen`、`live`、`final`；`state` 取 `forming`、`closed`；`trade_state` 取 `traded`、`no_trade`、`suspended`；`batch_id` 用 `rows.new_batch_id()` 生成。`sf` 是停牌标志（`1` 停牌，此时日线价格与量可以为空，`0` 正常）；`currency` 是价格货币，A 股为 `CNY`、港股为 `HKD`。

**`qfq_series` 的格式**：这是港股供应商口径的前复权序列，只用于港股前复权视图。它不进入原始事实表，而是作为整段替换的缓存版本保存；门面 `bars` 在港股前复权时读的就是它（再加盘中的原始价尾巴），量换算成股后输出为 `{dt, open, high, low, close, volume}`。与原始行的区别：价格是供应商复权后的价格，不带 `pc`、`sf`、`provenance`、`state` 等字段，也不经准入，而是在发布前整段校验（重复标签、非有限值、非正价、OHLC 越界、负量），任何一根不合法则整段不发布。

- 参数：`freq="day"` 时 `start`、`end` 为日期 `YYYY-MM-DD`；`freq="m30"` 时为完整时间 `YYYY-MM-DD HH:MM`（采集器传入 `<起始日> 09:30` 与 `<截止日> 16:00`）。两端都包含。
- 返回：字典列表。日线每项必须有 `trade_date`；30 分每项必须有 `trade_date` 与 `slot_end`（区间末端标签，规则同分钟原始行）。两者都必须有 `open`、`high`、`low`、`close`、`volume`；`volume_unit` 可省略，缺省为 `share`。
- 例子：

```python
[
    {"trade_date": "2026-09-18", "slot_end": "2026-09-18 10:00", "open": 512.0, "high": 515.5,
     "low": 510.5, "close": 514.0, "volume": 1250000, "volume_unit": "share"},
    {"trade_date": "2026-09-18", "slot_end": "2026-09-18 10:30", "open": 514.0, "high": 516.0,
     "low": 513.0, "close": 515.0, "volume": 830000, "volume_unit": "share"},
]
```


失败用 `raw.py` 里的异常区分，采集器据此处理：`ProviderRangeError`（返回越出请求范围，整批作废并登记缺口）、`ProviderServerError`（上游 5xx，登记缺口）、`ProviderConnectionError`（连接类失败，计入该来源的冷却）、`ProviderUnsupported`（能力未实现或未验证，跳过）、其他 `ProviderError`（只对该标的退避）。异常消息不得包含凭据。

### 注册能力

`ProviderSpec(module, factory, market, day_kinds, minute_freqs, calendar=False, preopen=False, vendor_qfq=False, credentials=(), budgeted=False)`：

| 字段 | 含义 |
| --- | --- |
| `module`、`factory` | provider 模块路径与类名 |
| `market` | `CN` 或 `HK` |
| `day_kinds` | 日线覆盖的标的类型：`stock`、`index` |
| `minute_freqs` | 提供的分钟粒度，`()` 表示没有分钟数据 |
| `calendar`、`preopen`、`vendor_qfq` | 是否提供交易年表、当日参考前收、供应商前复权 |
| `credentials` | 需要的环境变量名；真实模式启动时逐个检查非空 |
| `budgeted` | 是否受实例 `quota` 约束 |

作为某市场的主来源（实例配置里的 `source`），能力必须完整：A 股要求 `day_kinds` 含 `stock` 与 `index`、`calendar=True`、`preopen=True`；港股要求 `day_kinds` 含 `stock`、`calendar=True`、`vendor_qfq=True`；所选分钟粒度必须在 `minute_freqs` 里。能力不全的来源只能在 `BINDING_DEFAULTS` 里作冷备。真实模式要求两个市场都满足这些条件。

`BINDING_DEFAULTS`（现有各行见 `catalog.py`）的每一行是 `(市场, 标的类型, 取数项, 主来源, 冷备或 None, 探针编号[, 分钟事实粒度])`。探针编号是你给这条绑定起的标识字符串：来源验证结论按它记录在事实库 `probe_runs`，冷备保活结论记为 `keepalive-<探针编号>`，`/api/status` 的 `probes` 也按绑定显示这些结论；它不影响取数。取数项为 `day_history`、`minute_history`、`minute_live`、`preopen_ref`（A 股个股）、`session_calendar`、`instrument_list`；后两项的标的类型写 `any`；分钟取数项带粒度。冷备只能手动切换（见 [运维 · 冷备切换](operations.md#冷备切换)），不会自动回落。

### 最小骨架

下面的骨架只演示接口形状：在已有来源之外注册一个 A 股来源和一个港股来源，所有取数都报「不支持」。它能让选用它们的真实模式通过启动校验，配合 CSV 导入使用；要持续更新数据，需要把方法换成真实取数。

建议先在一个独立的实例目录里试装（例如下文的 `.cache/real/`），不要直接用于已有实例。只在 `REGISTRY` 里加项、不改 `BINDING_DEFAULTS` 时，产品默认来源不变，已有的 demo 与真实模式实例不受影响。

在 `chanapp/engine/kline/providers/catalog.py` 的 `REGISTRY` 字典里加两项（已有各项保持不变）：

```python
REGISTRY = {
    # ……已有来源保持不变……
    "example_cn": ProviderSpec("chanapp.engine.kline.providers.example", "ExampleCnProvider", "CN",
                               ("stock", "index"), ("m15",), calendar=True, preopen=True,
                               credentials=("EXAMPLE_CN_TOKEN",), budgeted=True),
    "example_hk": ProviderSpec("chanapp.engine.kline.providers.example", "ExampleHkProvider", "HK",
                               ("stock",), ("m30",), calendar=True, vendor_qfq=True,
                               credentials=("EXAMPLE_HK_TOKEN",)),
}
```

新建 `chanapp/engine/kline/providers/example.py`：

```python
"""示例 raw provider 骨架：只演示接口形状，所有取数都报「不支持」。"""
import os

from chanapp.engine.kline.providers.raw import ProviderUnsupported

CONTRACT_VERSION = "example-raw-1"


class _Skeleton:
    name = ""
    token_env = ""

    def __init__(self):
        self._token = os.environ[self.token_env]      # 凭据只从环境变量读取

    def day_history(self, code, start, end):
        raise ProviderUnsupported(f"{self.name} 尚未实现日线")

    def minute_history(self, code, fact_freq, start, end, *, now):
        raise ProviderUnsupported(f"{self.name} 尚未实现分钟历史")

    def minute_live(self, code, fact_freq, *, now):
        raise ProviderUnsupported(f"{self.name} 尚未实现盘中分钟")


class ExampleCnProvider(_Skeleton):
    name, token_env = "example_cn", "EXAMPLE_CN_TOKEN"


class ExampleHkProvider(_Skeleton):
    name, token_env = "example_hk", "EXAMPLE_HK_TOKEN"
```

用 [配置](configuration.md#例子) 里选用 `example_cn`、`example_hk` 的真实模式实例配置（保存为 `.cache/real/instance.json`）验证：

```bash
CHANAPP_INSTANCE_CONFIG=.cache/real/instance.json .venv/bin/python -m uvicorn chanapp.api.main:app --port 8899
# 启动失败：ValueError: 真实模式缺少环境变量: EXAMPLE_CN_TOKEN, EXAMPLE_HK_TOKEN

export EXAMPLE_CN_TOKEN='<placeholder>' EXAMPLE_HK_TOKEN='<placeholder>'
CHANAPP_INSTANCE_CONFIG=.cache/real/instance.json .venv/bin/python -m chanapp.engine.kline.ingest .cache/csv/example.csv
CHANAPP_INSTANCE_CONFIG=.cache/real/instance.json .venv/bin/python -m uvicorn chanapp.api.main:app --port 8899
curl -s http://127.0.0.1:8899/api/status    # "mode":"real","enabled":true
```

两个市场都选了骨架来源，所以只检查它们声明的两个变量。因为骨架不取数，图表只显示导入的历史（`source` 为 `import`），手动重拉（`/api/chart?...&refetch=1`）的 `X-Refetch-Status` 为 `failed`。

## 只读查询事实库

事实库是缓存根下的 `facts.sqlite`（SQLite，WAL 模式，文件权限 0600）。外部读者不得写入；需要分析时读 [运维 · 备份](operations.md#备份) 生成的一致副本，在线库只允许短时只读查询：

```bash
sqlite3 -cmd 'PRAGMA query_only=ON' 'file:.cache/csv/data/facts.sqlite?mode=ro' \
  "SELECT code, MIN(trade_date), MAX(trade_date), COUNT(*) FROM current_day_bars WHERE provenance='final' GROUP BY code;"
```

WAL 库缺少 `-shm` 文件时（例如写入进程关闭后它被删掉），`mode=ro` 会报 `unable to open database file`；确认没有进程在写时改用 `?mode=ro&immutable=1`。

主要对象：

| 对象 | 语义 |
| --- | --- |
| `day_bars`、`minute_bars` | 原始事实，修订追加，主键含 `revision`；分钟按 `fact_freq` 分开存放 |
| `current_day_bars`、`current_minute_bars` | 每个键的最新修订。**不排除隔离的键**，读者须用 `quarantine` 自行排除 |
| `quarantine` | 已隔离的日期或槽位，`released_at IS NULL` 为生效中 |
| `pending_review` | 已收盘值冲突待核验，`verdict` 为空表示未裁决 |
| `coverage_gaps` | 覆盖缺口，`resolved_at IS NULL` 为未解决；`reason='known_gap'` 表示不再自动补取 |
| `calendar`、`instruments` | 交易日历、标的名称与上市日 |
| `series_state` | 每个数据集的修订代次、已收盘代次与最近提交时刻 |
| `batches` | 每个批次的来源、绑定代次、接受与拒绝数 |
| `calc_runs`、`calc_signals` | 计算结论审计（demo 写在缓存根下独立的 `demo-audit.sqlite`） |

`volume` 是来源原生单位，必须按 `volume_unit` 换算后才能相加。库里没有复权价（港股供应商前复权缓存除外）。`PRAGMA user_version` 是物理结构版本，版本不符时应用拒绝打开。
