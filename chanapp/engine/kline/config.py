"""取数与视图的参数，单一出处（执行计划 A 决定 9）。

三类：spec §15 待定、须所有者确认（计划 B Q3）；已有 spec 依据；可逆的实现参数（实测后可调）。
"""
from chanapp.engine.kline.providers.catalog import REGISTRY

# spec §15 待定项，计划 B Q3 于 2026-09-28 由所有者确认（FINALIZE_DEADLINE 暂定，切换后首个交易日复核；
# 2026-09-30 所有者把 A 股首个定稿时点改到 20:00，A 股截止随之后移，港股不变）
STALE_IN_SESSION_S = 180            # 盘中距最近一次成功提交超过即 stale
FINALIZE_DEADLINE = {"CN": "21:00", "HK": "18:30"}   # 按市场：交易日此刻后仍无当日 final 日线即 stale（首个定稿时点后 1–2 小时）

# 已有 spec 依据（§5.1、§1）
INTRADAY_INTERVAL_S = 60            # 盘中增量间隔（接近额度上限时自动加倍；目标 2026-09-29 第三阶段统一 60 秒）
INTRADAY_INDEX_INTERVAL_S = 60      # 指数盘中增量间隔（同样随额度加倍；所有者 2026-09-29 按首日用量定）
VIEWING_WINDOW_S = 150              # 页面在看：最后一次图表请求后这么久内仍盘中取数（页面每 60 秒刷新；只影响盘中，不给历史回填资格）
DAY_BACKFILL_YEARS = 10
MINUTE_BACKFILL_YEARS = 3
HK_DAY_BACKFILL_FROM = "2023-01-01"
DEFAULT_WINDOW = 520
WINDOW_MINUTE_FREQ = "m60"          # 分析窗口里最宽的分钟周期（共振与 AI 至多读 day/m60/m30）：搜索查看的分钟窗口按它折算

# 可逆的实现参数
DEFAULT_PER_MINUTE = 300
DEFAULT_PER_DAY = 1000
QUOTA_RESERVE = 0.10
QUOTA = {source: {"per_minute": DEFAULT_PER_MINUTE, "per_day": DEFAULT_PER_DAY,
                  "reserve": QUOTA_RESERVE} for source, spec in REGISTRY.items() if spec.budgeted}

QUOTA_SLOWDOWN_RATIO = 0.8          # 当日用量超过此比例时盘中间隔加倍
BREAKER_MAX_FAILURES = 3            # 单源连败冷却
BREAKER_COOLDOWN_S = 600
CODE_BACKOFF_AFTER = 2              # 单标的连败退避
CODE_BACKOFF_S = 300
FINALIZE_RETRY_S = 7200              # 当日定稿失败后隔这么久再试（按实际失败时刻；目标 2026-09-29 第三阶段）
FINALIZE_MAX_ATTEMPTS = 3           # 每个（代码，交易日）至多这么多次自动尝试；一个请求都没发出的暂缓与手动重拉不计
FINALIZE_DEFER_S = 60               # 定稿因退避、冷却或额度暂缓后，同一代码至少隔这么久再看
CATCHUP_WAIT_S = 15                 # 请求路径追赶的同步等待上限（前端图表超时 25 秒），超时由后台继续
REQUEST_LOCK_WAIT_S = 2              # 请求路径（首开补取、手动重拉）等同一代码单飞锁（历史规划、缓存刷新在途）的上限：首开超时读现有快照、没有就 503 由页面重试；重拉超时回 busy
CATCHUP_THROTTLE_S = 60             # 同一代码两次请求路径追赶的最小间隔
CATCHUP_MAX_REQUESTS = 4            # 一次追赶续传本代码历史缺口的请求上限（从新到旧）
CATCHUP_LOOKBACK_DAYS = 31          # 追赶只续传结束于近这么多天的缺口，更早的留给历史追赶线程
HISTORY_INTERVAL_S = 5              # 历史追赶线程的轮次间隔（计划 2026-09-29 D5）
HISTORY_CODE_REQUESTS = 2           # 历史追赶每轮为每个活跃代码续传缺口的请求上限（从新到旧；盘中也运行）
HISTORY_ROUND_REQUESTS = 8          # 历史追赶每轮活跃代码续传的全局请求上限（跨代码按缺口新旧排队；与盘中共享每分钟额度）
HISTORY_DRAIN_REQUESTS = 20         # 历史追赶每轮全库缺口续传的请求上限（每市场；只在没有市场开盘、到了回填时段时）
READ_RULES_VERSION = "views-1"      # 视图读取规则版本，进令牌
