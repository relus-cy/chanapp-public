"""Yahoo raw 冷备：港股日线（1d）与 m30（30m），不复权。spec §5.1 F1/F2、§7、§8。

- 请求只带 interval、period1/period2、includePrePost=false；不带 events、includeAdjustedClose（不复权、不取事件）。
- 时间戳按 Asia/Hong_Kong 解释。日线取当日日期；30m 时间戳是区间起点，加 30 分钟得末端标签，
  起点等于会话终点的收市竞价 bar 并入该终点槽（与长桥同一规则，spec §5.2）；其余不在
  sessions.slots("HK", "m30") 网格上的行丢弃（如形成中的非整点尾巴 13:41）。
- OHLC 任一为 None 的行（午休占位等）丢弃。日线 pc = 上一行收盘（港股前收即上一日收盘），首行用回看窗口补。
- 会话、代码映射、粒度校验与重试由旧实现迁入（计划 A 决定 11），本模块不依赖旧实现：
  - 符号必须 4 位零填充（hk00700 → 0700.HK；5 位会被当成另一交易所的基金，返回空 timestamp）；
  - 裸 requests 会 429，必须 curl_cffi impersonate chrome；偶发超时或 429 时重建 session 换边缘节点重试；
  - 边缘节点偶发返回错档缓存，meta.dataGranularity 与请求不符即判失败。
- 失败归类：超时、断连与 429 限流抛 ProviderConnectionError（采集器计入单源冷却、不消耗缺口重试次数），
  5xx 抛 ProviderServerError，其余 4xx 与数据形态错误抛 ProviderError。
- curl_cffi lazy import；单测注入 fetch，不联网。本 provider 不实现 minute_live（港股冷备盘中另行评估）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from chanapp.engine.kline import sessions
from chanapp.engine.kline.providers.raw import (ProviderConnectionError, ProviderError, ProviderRangeError,
                                                ProviderServerError, ProviderUnsupported)
from chanapp.engine.kline.rows import RawDayRow, RawMinuteRow, new_batch_id

log = logging.getLogger(__name__)

CONTRACT_VERSION = "yahoo-raw-1"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
HKT = ZoneInfo("Asia/Hong_Kong")
INTERVAL = {"day": "1d", "m30": "30m"}
GRANULARITY = {"1d": "1d", "30m": "30m"}
DAY_LOOKBACK_DAYS = 14     # 为首行 pc 回看的自然日（覆盖港股长假）
FETCH_ATTEMPTS = 6

_session = None


def _yahoo_sym(code: str) -> str:
    """hk00700 → "0700.HK"（4 位零填充）。"""
    return f"{str(int(code[2:])).zfill(4)}.HK"


def _get_session(refresh: bool = False):
    """进程级 curl_cffi Session（lazy import）；refresh=True 丢弃旧连接重建（换边缘节点）。"""
    global _session
    if _session is None or refresh:
        from curl_cffi import requests as creq
        old, _session = _session, creq.Session(impersonate="chrome")
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
    return _session


def default_fetch(sym: str, params: dict) -> dict:
    """带重试的 chart 抓取（偶发连接级超时 / 429：重建 session 换边缘节点）。"""
    url = CHART_URL.format(sym=sym)
    last_err, status = None, None
    for attempt in range(FETCH_ATTEMPTS):
        status = None                                    # 没有拿到响应（连接、超时）时保持 None
        try:
            r = _get_session(refresh=attempt > 0).get(url, params=params, timeout=15)
            status = r.status_code
            if status == 429:
                raise RuntimeError("HTTP 429")
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("yahoo %s %s 第 %d 次失败: %s", sym, params.get("interval"), attempt + 1, e)
            time.sleep(1.0)
    message = f"yahoo {sym} {params.get('interval')} 重试后仍失败: {last_err}"
    if status is None or status == 429:
        raise ProviderConnectionError(message)
    if status >= 500:
        raise ProviderServerError(message)
    raise ProviderError(message)


def _hkt(ts) -> datetime:
    return datetime.fromtimestamp(int(ts), HKT)


def _epoch(day: str, hour: int = 0) -> int:
    d = date.fromisoformat(day)
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=HKT).timestamp())


def _now_label(now) -> str:
    if now.tzinfo is not None:
        now = now.astimezone(HKT)
    return now.strftime("%Y-%m-%d %H:%M")


class YahooRawProvider:
    name = "yahoo"
    CONTRACT_VERSION = CONTRACT_VERSION

    def __init__(self, fetch=None):
        self._fetch = fetch or default_fetch

    def _chart(self, code, interval, first_day, last_day) -> list[tuple[datetime, dict]]:
        params = {"interval": interval, "period1": _epoch(first_day),
                  "period2": _epoch(last_day) + 86400, "includePrePost": "false"}
        try:
            payload = self._fetch(_yahoo_sym(code), params)
        except ProviderError:
            raise
        except (TimeoutError, OSError) as exc:
            raise ProviderConnectionError(f"yahoo {code} {interval}: {type(exc).__name__}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"yahoo {code} {interval}: {type(exc).__name__}: {exc}") from exc
        chart = (payload or {}).get("chart") or {}
        results = chart.get("result") or []
        if not results:
            raise ProviderError(f"yahoo chart 无数据: {chart.get('error')}")
        res = results[0]
        gran = (res.get("meta") or {}).get("dataGranularity")
        if gran and gran != GRANULARITY[interval]:
            raise ProviderError(f"yahoo 粒度不符: 期望 {GRANULARITY[interval]} 实得 {gran}（边缘节点错档）")
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        out = []
        for i, ts in enumerate(res.get("timestamp") or []):
            try:
                o, h, lo, c = (quote[k][i] for k in ("open", "high", "low", "close"))
                vol = (quote.get("volume") or [None] * (i + 1))[i]
            except (KeyError, IndexError):
                continue
            if None in (o, h, lo, c):
                continue  # 午休等占位 null bar
            out.append((_hkt(ts), {"open": float(o), "high": float(h), "low": float(lo),
                                   "close": float(c), "volume": float(vol or 0)}))
        return out

    def day_history(self, code, start, end) -> list:
        lookback = (date.fromisoformat(start) - timedelta(days=DAY_LOOKBACK_DAYS)).isoformat()
        bars = self._chart(code, INTERVAL["day"], lookback, end)
        batch, rows, prev_close = new_batch_id(), [], None
        for when, b in bars:
            day = when.strftime("%Y-%m-%d")
            if start <= day <= end:
                rows.append(RawDayRow(code, day, b["open"], b["high"], b["low"], b["close"], b["volume"],
                                      "share", None, "HKD", prev_close, 0, "final", batch))
            prev_close = b["close"]
        if any(r.trade_date > end for r in rows) or len({r.trade_date for r in rows}) != len(rows):
            raise ProviderRangeError(f"{code} yahoo day 返回越界或重复日期")
        return rows

    def minute_history(self, code, fact_freq, start, end, *, now) -> list:
        if fact_freq != "m30":
            raise ProviderUnsupported(f"yahoo 冷备分钟只取 m30，收到 {fact_freq}")
        grid = set(sessions.slots("HK", "m30"))
        ends = {end for _, end in sessions.SESSIONS["HK"]}
        limit = min(end, _now_label(now))
        bars = self._chart(code, INTERVAL["m30"], start[:10], end[:10])
        batch, rows = new_batch_id(), []
        for when, b in bars:
            label = when.strftime("%Y-%m-%d %H:%M")
            if label[11:] in ends and rows and rows[-1].slot_end == label:
                prev = rows[-1]                  # 收市竞价：开盘沿用前一根，极值取两者，收盘取竞价，量相加
                rows[-1] = replace(prev, high=max(prev.high, b["high"]), low=min(prev.low, b["low"]),
                                   close=b["close"], volume=prev.volume + b["volume"])
                continue
            slot = (when + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
            if slot[11:] not in grid or slot[:10] != when.strftime("%Y-%m-%d"):
                continue  # 非网格：形成中的非整点尾巴；无前一根可并的竞价
            if not (start <= slot <= limit):
                continue
            rows.append(RawMinuteRow(code, slot[:10], slot, b["open"], b["high"], b["low"], b["close"],
                                     b["volume"], "share", None, "closed", "traded", batch))
        if len({r.slot_end for r in rows}) != len(rows):
            raise ProviderError(f"{code} yahoo m30 归一后 slot_end 重复")
        return rows
