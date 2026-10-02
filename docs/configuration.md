# 配置

chanapp 的部署配置只有两处：一份实例配置 JSON（运行模式、各市场来源与分钟粒度、额度、实例目录），和若干环境变量（文件路径、采集开关、凭据、AI 模型）。没有指定实例配置时进入离线 demo；配置有任何错误，应用拒绝启动并说明原因；改动后重启生效。

展示哪些周期不属于部署配置，而是在页面上勾选的界面偏好，见 [展示周期与分析](periods-and-analysis.md)。

本页以 `chanapp/engine/kline/instance.py`（实例配置）与 `chanapp/engine/instance_paths.py`（路径）为准。下文命令都在仓库根执行。

## 实例配置

环境变量 `CHANAPP_INSTANCE_CONFIG` 指向一份 UTF-8 JSON 文件。没有设置时等于空对象 `{}`，也就是 demo 模式、全部取默认值。下例除 `instance_dir`（默认不设）外，写出的都是默认值：

```json
{
  "mode": "demo",
  "markets": {
    "CN": {"source": "mairui", "minute_fact_freq": "m15"},
    "HK": {"source": "longbridge", "minute_fact_freq": "m30"}
  },
  "quota": {"per_minute": 300, "per_day": 1000},
  "instance_dir": "."
}
```

### 字段与合法值

| 字段 | 默认值 | 合法值与含义 |
| --- | --- | --- |
| `mode` | `"demo"` | `"demo"` 或 `"real"`。见下文「demo 与真实模式」 |
| `markets` | 两个市场都取默认 | 只允许键 `CN`（A 股）与 `HK`（港股）；每项只允许 `source` 与 `minute_fact_freq`，省略的字段继承默认 |
| `markets.<市场>.source` | A 股 `"mairui"`、港股 `"longbridge"`（取自安装清单 `chanapp/engine/kline/providers/catalog.py` 的 `BINDING_DEFAULTS`） | 安装清单里注册、且具备该市场必需能力的来源名称，能力要求见 [数据契约 · 注册能力](data-contract.md#注册能力)。CSV 导入的批次在数据里记为 `import`，但 `import` 不是可选的来源 |
| `markets.<市场>.minute_fact_freq` | A 股 `"m15"`、港股 `"m30"`（同样取自 `BINDING_DEFAULTS`） | 分钟事实粒度，即事实库保存的分钟 K 线周期。JSON `null` 表示该市场只提供日线。合法取值见下表 |
| `quota.per_minute`、`quota.per_day` | `300`、`1000` | 正整数（JSON 整数，不能是字符串或小数）。只对声明了「计额度」的来源生效，见下文「额度」 |
| `instance_dir` | 不设 | 非空路径字符串；相对路径以配置文件所在目录为基准，`~` 会展开。见下文「实例目录与数据路径」 |

`minute_fact_freq` 的合法取值取决于运行模式：

| 模式 | A 股 | 港股 |
| --- | --- | --- |
| demo | `null`、`"m5"`、`"m15"` | `null`、`"m30"` |
| 真实模式 | `null`、`"m5"`、`"m15"`、`"m60"`，且必须在所选来源声明的分钟粒度之内 | `null`、`"m30"`、`"m60"`，同样受来源能力限制 |

demo 不调用在线来源，分钟粒度只按导入可用的粒度校验，不要求所选来源具备，所以随包 demo 的 A 股 `m5` 实例可以照常启动。来源本身在 demo 下也要校验：`source` 仍须是已注册且能力完整的来源（省略时取默认），只是不会被调用。

页面只展示 30 分、60 分、日线、周线；分钟周期由事实粒度在读取时合成，规则见 [展示周期与分析](periods-and-analysis.md#哪些周期可以合成)。`m60` 作为事实粒度是合法规则，但安装清单里没有来源声明这一粒度，所以真实模式下选它会被拒绝。

### 校验与拒绝

启动时（以及每次运行 CSV 导入命令时）统一校验，任一条不满足都拒绝，错误信息如下（`<...>` 为实际值）：

| 情况 | 错误信息 |
| --- | --- |
| 文件不存在、不可读或不是合法 JSON | `实例配置文件不可读或不是合法 JSON` |
| 同一对象里出现重复字段 | `实例配置含重复字段` |
| 顶层不是对象 | `instance 必须为 object` |
| 未知字段 | `instance 未知字段: <字段>`、`markets 未知字段: <市场>`、`markets.CN 未知字段: <字段>` |
| `mode` 不合法 | `mode 必须为 demo 或 real` |
| 来源未注册 | `CN.source 必须为已注册来源` |
| 来源缺少该市场必需的能力 | `CN.source 缺少完整市场的日线、日历或复权辅助能力` |
| 分钟粒度不合法 | demo 为 `CN.minute_fact_freq 不支持 'm60'`；真实模式为 `CN.minute_fact_freq 不支持 'm60'：须为该市场允许且所选来源具备的粒度` |
| 额度不是正整数 | `quota.per_day 必须为正整数` |
| `instance_dir` 为空或不是字符串 | `instance_dir 必须为非空路径` |
| 实例目录建不出来 | `instance_dir 无法初始化：<系统错误>` |
| 真实模式缺凭据 | `真实模式缺少环境变量: <变量名>, ...`（只报变量名，不打印值） |

应用由 uvicorn 启动时，这些错误表现为 `Application startup failed`，进程退出。

## demo 与真实模式

模式只由实例配置的 `mode` 决定。环境里有没有凭据、`COLLECTOR_ENABLED` 取什么值，都不会改变模式。

| | demo（默认） | 真实模式（`"mode": "real"`） |
| --- | --- | --- |
| 前提 | 无 | 两个市场所选来源（默认 `mairui` 与 `longbridge`）声明的凭据环境变量都非空，见下文「环境变量」 |
| 联网 | 不联网：不采集、不补历史、不追赶、不手动重拉，AI 不调用 | 采集器随应用启动（`COLLECTOR_ENABLED=0` 时不启动） |
| 事实库 | 只读打开，不存在时用内存空库，不在磁盘新建 | 读写，采集器是唯一写者 |
| 新鲜度 | 不判 stale，状态栏写「历史截止于某日」 | 按会话与定稿时点判定，见 [数据契约 · 新鲜度](data-contract.md#新鲜度与数据状态) |
| 搜索、报价、F10 | 搜索只查本地事实；报价取本地最新已收盘值并标「历史」；F10 与资金流为空 | 远程搜索 `/api/search`、自选批量快照 `/api/quotes`、F10 `/api/f10` 与港股单代码报价经显示层在线取数（`chanapp/engine/display_feed.py`、`chanapp/engine/feeds/`、`chanapp/engine/search.py`），不需要凭据；A 股的报价价格由 K 线事实派生 |
| AI 分析 | 返回 `disabled`，即使配置了模型 key | 配置了 `LLM_API_KEY` 时可手动触发 |

只写 `{"mode": "real"}` 就会选用两个市场的默认来源；没有提供凭据时启动失败，报 `真实模式缺少环境变量: LONGBRIDGE_ACCESS_TOKEN, LONGBRIDGE_APP_KEY, LONGBRIDGE_APP_SECRET, MAIRUI_LICENCE`。即使只关心 A 股，港股也必须选一个能力完整的已注册来源并提供它的凭据。不开真实模式时，自己的历史数据可以经 CSV 导入后在 demo 模式下查看，见 [数据契约 · CSV 导入](data-contract.md#csv-导入)。

## 实例目录与数据路径

`instance_dir` 让一个目录承载一个实例的全部运行数据。配置了实例目录时，启动会创建所需目录（新建的实例根目录权限为 0700），并把仓库自带的 `chanapp/watchlist.json` 复制成个人自选；已有文件一律不覆盖。

每条路径按下表从左到右取第一个有值的来源（环境变量为空字符串视为未设）：

| 数据 | 显式环境变量 | 随 `WATCHLIST_PATH` | 配置了实例目录 | 两者都没有 |
| --- | --- | --- | --- | --- |
| 缓存根（事实库 `facts.sqlite`、demo 审计库、日历导出） | `CHANAPP_CACHE_DIR` | — | `<实例>/data` | `chanapp/.cache` |
| AI 分析缓存 | `ANALYSIS_CACHE_DIR` | — | `<实例>/data/analysis` | `chanapp/.cache/analysis`（不跟随 `CHANAPP_CACHE_DIR`） |
| 个人自选 | `WATCHLIST_PATH` | — | `<实例>/watchlist.json` | `<缓存根>/watchlist.json` |
| 搜索查看记录 | `VIEW_LOG_PATH` | 同目录 `<自选文件名>.views.sqlite` | `<实例>/views.sqlite` | `<缓存根>/views.sqlite` |
| 展示周期偏好 | — | 同目录 `<自选文件名>.periods.json` | `<实例>/periods.json` | `<缓存根>/periods.json` |

- 仓库里的 `chanapp/watchlist.json` 只是种子：个人自选文件不存在时从它读取，写入一律落在个人路径。
- 一个进程只服务一个实例。改用实例目录不会自动搬迁旧路径下的状态；需要沿用旧数据时，先停服务再手动移动文件。
- 配置文件、实例目录和个人状态应放在应用代码目录之外或被版本控制忽略的位置；仓库的 `.gitignore` 已忽略 `.cache/`。

## 额度

`quota` 是采集器给自己设的请求预算，用来防止失控，不代表任何来源的真实限额。它只对实例所选、且注册时声明 `budgeted=True` 的来源生效；当前安装清单里只有 `mairui` 声明了 `budgeted=True`，其余来源不受它约束。

- 每分钟上限按最近 60 秒计数；每日上限按主机本地日期计数，并持久在事实库里，重启不清零。
- 每日上限的 10% 固定留给恢复类请求，普通请求最多用到 90%。
- 当日用量超过上限的 80% 后，盘中增量间隔自动加倍。

默认额度大致能支撑多少只自选，见 [支持清单与限制 · 默认额度能支撑多少自选](support-and-limits.md#默认额度能支撑多少自选)。

## 环境变量

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `CHANAPP_INSTANCE_CONFIG` | 不设（demo） | 实例配置文件路径 |
| `CHANAPP_CACHE_DIR`、`WATCHLIST_PATH`、`VIEW_LOG_PATH`、`ANALYSIS_CACHE_DIR` | 不设 | 逐项覆盖数据路径，见上节 |
| `COLLECTOR_ENABLED` | `1` | 只在真实模式下有意义。`0` 时不启动采集器，首次打开、补窗口和落后追赶也不访问上游；已入库数据照常服务，没有事实的代码返回 502。不能跳过凭据校验。测试时设为 `0` |
| `LLM_PROVIDER` | `deepseek` | AI 分析的模型服务，目前只支持这一个值（OpenAI 兼容的 chat completions 接口） |
| `LLM_API_KEY` | 不设 | 模型服务的 key；不设时 AI 分析返回 `unconfigured`，页面显示静态样例 |
| `LLM_MODEL` | `deepseek-flash` | 模型名 |
| `LLM_TIMEOUT_SECONDS` | `30` | 单次请求超时秒数，超出 1–120 的值会被截到边界，非整数时 AI 请求报错 |
| `TZ` | 系统时区 | 建议设为 `Asia/Shanghai`，原因见下 |
| 来源凭据 | 不设 | 见下文「来源凭据」 |

**应用不会自己读取 `.env` 文件。** 变量要由启动它的 shell 或服务管理器注入。仓库根的 [`.env.example`](../.env.example) 是模板，复制后填写，文件本身不要提交。

**时区**：交易日判断、会话时段和 CSV 导入的截止日按东八区计算，不依赖主机时区；但事实库里的提交时刻（`fetch_time` 等）、每日额度的日期和采集调度按进程本地时间计。主机不在东八区的运行方式未实测，建议给进程设 `TZ=Asia/Shanghai`。

### 来源凭据

使用在线来源需要使用者自行开通，并在环境变量里提供凭据；项目不代供数据。各来源声明的凭据变量名以安装清单 `catalog.py` 的 `credentials` 为准：

| 来源（`source` 名称） | 凭据环境变量 |
| --- | --- |
| `mairui` | `MAIRUI_LICENCE` |
| `longbridge` | `LONGBRIDGE_APP_KEY`、`LONGBRIDGE_APP_SECRET`、`LONGBRIDGE_ACCESS_TOKEN` |
| `baostock`、`pytdx`、`yahoo` | 无 |

真实模式启动时只检查两个市场所选来源（`markets.<市场>.source`）的凭据，缺失或为空时只报变量名并拒绝启动，`COLLECTOR_ENABLED=0` 时也检查。行情快照、F10 与远程搜索所用的显示层接口不需要凭据。AI 分析的 key 是上表之外的 `LLM_API_KEY`。

凭据只经环境变量传入：不要写进实例配置、代码、日志或文档。示例（占位符）：

```bash
export MAIRUI_LICENCE='<your-licence>'
export LONGBRIDGE_APP_KEY='<your-app-key>' LONGBRIDGE_APP_SECRET='<your-app-secret>' LONGBRIDGE_ACCESS_TOKEN='<your-access-token>'
```

AI 分析是可选功能。需要时另设 `LLM_API_KEY`；启动真实模式不需要这个变量。

## 例子

只日线的 demo 实例（A 股不保存分钟，港股取默认）：

```json
{"mode": "demo", "markets": {"CN": {"minute_fact_freq": null}}, "instance_dir": "."}
```

### 真实模式实例

这个实例把 A 股分钟事实设为 5 分，其余取默认值。A 股使用 `mairui`，港股使用 `longbridge`。

创建实例目录：

```bash
mkdir -p .cache/real
```

保存实例配置：

```bash
cat > .cache/real/instance.json <<'EOF'
{"mode": "real", "markets": {"CN": {"minute_fact_freq": "m5"}}, "instance_dir": "."}
EOF
```

启动前，按 [来源凭据](#来源凭据) 设置两个市场所需的全部凭据。无需设置 `LLM_API_KEY`，除非要使用 AI 分析。

凭据设置完成后，按 [运维 · 启动](operations.md#启动) 启动这个实例。

### 自定义来源实例

接入自己的来源后，在 `source` 里写它注册的名称即可，见 [数据契约 · 接入其他在线来源](data-contract.md#接入其他在线来源)：

```json
{
  "mode": "real",
  "markets": {
    "CN": {"source": "example_cn", "minute_fact_freq": "m15"},
    "HK": {"source": "example_hk", "minute_fact_freq": "m30"}
  },
  "quota": {"per_minute": 300, "per_day": 1000},
  "instance_dir": "."
}
```
