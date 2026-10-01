"""麦蕊 A 股个股与指数 raw provider（普通 history/latest 端点，HTTP，无 SDK）。

- 只用不复权端点（d/n、15/n，回退用 5/n）；原生 f/fr 复权在 300209 重整上错误，永不请求；
- 分钟事实 2026-09-30 起为原生 m15（目标 2026-09-29 第三阶段；m5 当日历史部分个股迟迟不追加），m5 只为回退保留；
- latest 多日行会把近期交易日填成 sf=1 平价，只取当日行；
- 日线 st/et 用 YYYYMMDD，分钟用 'YYYY-MM-DD HH:MM:SS'（14 位紧凑格式有空返与越界），并校验返回范围；
- 证书在 URL 路径里：异常与日志一律替换为 [REDACTED]，并切断异常链。
"""
from __future__ import annotations

import os
import urllib.parse
from datetime import date, datetime, timedelta

from chanapp.engine.kline.providers.raw import (ProviderConnectionError, ProviderError, ProviderRangeError,
                                                ProviderServerError)
from chanapp.engine.kline.rows import CalendarRow, InstrumentRow, RawDayRow, RawMinuteRow, kind_of, new_batch_id

CONTRACT_VERSION = "mairui-raw-2"         # 2：分钟端点改原生 15 分钟
API = "https://api.mairuiapi.com"
_TIMEOUT_SMALL = (8, 30)
_TIMEOUT_HISTORY = (8, 180)
_CALENDAR_MIN_OPEN = 200        # A 股每年约 242 个交易日；少于此数视为截断
_MINUTE_PATH = {"m5": ("5/n", "5"), "m15": ("15/n", "15")}   # 分钟事实粒度 → (个股, 指数) 路径段


def _vendor_code(code: str) -> str:
    return f"{code[2:]}.{code[:2].upper()}"


class MairuiProvider:
    name = "mairui"
    CONTRACT_VERSION = CONTRACT_VERSION

    def __init__(self, transport=None, licence=None):
        self._licence = licence if licence is not None else os.environ.get("MAIRUI_LICENCE", "")
        self._transport = transport or self._http

    def _redact(self, text) -> str:
        text = str(text)
        if self._licence:
            text = text.replace(self._licence, "[REDACTED]")
            text = text.replace(urllib.parse.quote(self._licence, safe=""), "[REDACTED]")
        return text

    def _http(self, path, params, timeout):
        import requests
        if not self._licence:
            raise ProviderConnectionError("MAIRUI_LICENCE 未设置")   # 整个源不可用，不消耗缺口重试
        url = API + path.replace("{L}", urllib.parse.quote(self._licence, safe=""))
        response = requests.get(url, params=params, timeout=timeout)
        try:
            body = response.json()
        except ValueError:
            body = response.text[:500]
        return response.status_code, body

    def _get(self, path, params, timeout=_TIMEOUT_SMALL):
        try:
            status, body = self._transport(path, params, timeout)
        except ProviderError:
            raise                                   # 证书未设置等：provider 自身判定，非连接类
        except Exception as exc:  # noqa: BLE001 — 传输层失败即连接类；统一脱敏并切断异常链
            raise ProviderConnectionError(self._redact(f"{type(exc).__name__}: {exc}")) from None
        if status >= 500:
            raise ProviderServerError(f"HTTP {status} {path.split('/{L}')[0]}")
        if status != 200:
            raise ProviderError(f"HTTP {status} {path.split('/{L}')[0]}: {self._redact(body)[:200]}")
        return body

    def _base(self, code, suffix_stock, suffix_index) -> str:
        v = _vendor_code(code)
        return (f"/hsindex/{{op}}/{v}/{suffix_index}/{{L}}" if kind_of(code) == "index"
                else f"/hsstock/{{op}}/{v}/{suffix_stock}/{{L}}")

    def day_history(self, code, start, end) -> list:
        path = self._base(code, "d/n", "d").format(op="history", L="{L}")
        body = self._get(path, {"st": start.replace("-", ""), "et": end.replace("-", "")},
                         _TIMEOUT_HISTORY)
        batch = new_batch_id()
        rows = [RawDayRow(code, r["t"][:10], r["o"], r["h"], r["l"], r["c"], r["v"], "lot",
                          r.get("a"), "CNY", r.get("pc"), int(r.get("sf", 0)), "final", batch)
                for r in body or []]
        if any(not (start <= r.trade_date <= end) for r in rows):
            raise ProviderRangeError(f"{code} day 返回越出 {start}..{end}")
        return rows

    def _minute_rows(self, code, body, *, closed) -> list:
        batch = new_batch_id()
        state = "closed" if closed else "forming"
        out = []
        for r in body or []:
            slot = r["t"][:16]
            trade_state = "suspended" if int(r.get("sf", 0)) == 1 else "traded"
            out.append(RawMinuteRow(code, slot[:10], slot, r["o"], r["h"], r["l"], r["c"], r["v"],
                                    "lot", r.get("a"), state, trade_state, batch))
        return out

    @staticmethod
    def _minute_path(fact_freq):
        if fact_freq not in _MINUTE_PATH:
            raise ProviderError(f"麦蕊分钟事实只取 m15（回退 m5），收到 {fact_freq}")
        return _MINUTE_PATH[fact_freq]

    def minute_history(self, code, fact_freq, start, end, *, now) -> list:
        path = self._base(code, *self._minute_path(fact_freq)).format(op="history", L="{L}")
        body = self._get(path, {"st": f"{start}:00", "et": f"{end}:00"}, _TIMEOUT_HISTORY)
        rows = self._minute_rows(code, body, closed=True)
        if any(not (start <= r.slot_end <= end) for r in rows):
            raise ProviderRangeError(f"{code} {fact_freq} 返回越出 {start}..{end}")
        return rows

    def minute_live(self, code, fact_freq, *, now) -> list:
        # 个股与指数同一路径形态（P3：2026-09-28 盘中实测指数 latest 同样推进）
        path = self._base(code, *self._minute_path(fact_freq)).format(op="latest", L="{L}")
        body = self._get(path, {"lt": 2})
        today = now.date().isoformat()
        return [r for r in self._minute_rows(code, body, closed=False)
                if r.trade_date == today]

    def preopen_ref(self, code, trade_date) -> RawDayRow | None:
        body = self._get(f"/hsstock/real/time/{code[2:]}/{{L}}", {})
        if not isinstance(body, dict) or str(body.get("t", ""))[:10] != trade_date:
            return None
        return RawDayRow(code, trade_date, None, None, None, None, None, "lot", None, "CNY",
                         body.get("yc"), 0, "preopen", new_batch_id())

    def calendar(self, year) -> list:
        """年表只列开市日。年表完整（覆盖年初到年末、开市日数合理）时缺席工作日记为休市；
        空表或截断表只返回开市日，缺席工作日留作未知（计划 A 决定 5），不因此停掉这些日子的采集。"""
        body = self._get(f"/tcalendar/list/{year}/{{L}}", {})
        opened = {f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in body or []}
        complete = (len(opened) >= _CALENDAR_MIN_OPEN and min(opened) <= f"{year}-01-10"
                    and max(opened) >= f"{year}-12-20")
        rows, day = [], date(year, 1, 1)
        while day.year == year:
            iso = day.isoformat()
            if iso in opened or (complete and day.weekday() < 5):
                rows.append(CalendarRow("CN", iso, iso in opened))
            day += timedelta(days=1)
        return rows

    def instruments(self) -> list:
        body = self._get("/hslt/list/{L}", {})
        return [InstrumentRow(f"{r['jys'].lower()}{r['dm'][:6]}", r["mc"], None, None, "stock")
                for r in body or []]

    def instrument(self, code) -> InstrumentRow:
        body = self._get(f"/hsstock/instrument/{_vendor_code(code)}/{{L}}", {})
        od = str(body.get("od") or "").replace("-", "")   # 录制值为 ISO（2002-04-09），兼容 YYYYMMDD
        listed = f"{od[:4]}-{od[4:6]}-{od[6:8]}" if len(od) >= 8 and od[:8].isdigit() else None
        return InstrumentRow(code, body.get("name", code), listed, None, kind_of(code))
