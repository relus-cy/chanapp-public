"""BaoStock raw 冷备：A 股个股日线（不复权，含 preclose 与停牌状态）与交易日历。spec §5.1 F1/F5、§8。

- 只请求 adjustflag="3"（不复权）；preclose 是交易所参考前收，tradestatus "1" 为交易、其余为停牌（sf=1）。
- volume 为股（volume_unit="share"），provider 不换算；空串（停牌日的量额）转 None。
- 指数日线不在本冷备范围（指数冷备是 pytdx；BaoStock 早年指数日线有自相矛盾行，spec §8）。
- 登录与看门狗（计划 A 决定 11；P2-14 修正）：
  - 客户端 recv 无超时，服务端停摆会永久阻塞 → 每次「按需登录 + 查询」在一个新的 daemon 线程里跑，
    调用方最多等 QUERY_TIMEOUT；daemon 线程不会拖住解释器退出。
  - 超时或任何失败都丢弃会话：关掉库的全局 socket 唤醒卡住的 recv，下次查询重新登录。
    不调 bs.logout()：它在同一条死连接上收包，会把调用方和串行锁一起挂住。
  - 仍卡着的线程计数，达到 MAX_STUCK_WORKERS 时直接快速失败，不再新建线程。
  - 未覆盖：卡在 connect()/DNS 超过超时的旧线程之后又连上，会改写库的全局 socket 并继续跑已放弃的查询；
    若此时有新查询，两者可能在同一 socket 上交错收发（最坏读到别的标的的响应）。要求 DNS/connect 先挂住
    再成功，且冷备只在手动切换后启用，概率很低，登记在 backlog。
  - rs.next() 必须与 rs.get_row_data() 成对调用，否则 cur_row_num 不前进 → 永真死循环。
- 失败归类：超时（看门狗）、断连与 BaoStock 网络类错误码（10002xxx）抛 ProviderConnectionError，
  由采集器计入单源冷却、不消耗缺口重试次数；其余错误抛 ProviderError。
- baostock 库 lazy import；单测注入 query，不联网。
"""
from __future__ import annotations

import logging
import re
import socket
import threading

from chanapp.engine.kline.providers.raw import (ProviderConnectionError, ProviderError, ProviderRangeError,
                                                ProviderUnsupported)
from chanapp.engine.kline.rows import CalendarRow, RawDayRow, kind_of, new_batch_id

log = logging.getLogger(__name__)

CONTRACT_VERSION = "baostock-raw-1"
DAY_FIELDS = "date,open,high,low,close,preclose,volume,amount,tradestatus"
QUERY_TIMEOUT = 120  # 看门狗：单查询超时（全深度 30m 实测 max ~62s）
MAX_STUCK_WORKERS = 2  # 超时后仍未退出的查询线程上限，达到即快速失败
_NETWORK_CODE = re.compile(r"\b10002\d{3}\b")   # BaoStock 网络类错误码（如 10002007 网络接收错误）

_lock = threading.Lock()
_logged_in = False
_stuck: list[threading.Thread] = []   # 超时后仍存活的查询线程


def _bs_code(code: str) -> str:
    """sh600036 → sh.600036。"""
    return f"{code[:2]}.{code[2:]}"


def _login():
    global _logged_in
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login 失败: {lg.error_code} {lg.error_msg}")
    _logged_in = True


def _discard_session():
    """丢弃会话：标记未登录，并关掉库的全局 socket，让卡在 recv 上的线程出错退出。"""
    global _logged_in
    _logged_in = False
    try:
        import baostock.common.context as context
    except ImportError:
        return
    sock = getattr(context, "default_socket", None)
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)   # 只 close 唤不醒别的线程里阻塞的 recv
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _run_with_watchdog(fn, timeout: float):
    """看门狗执行：fn 在新 daemon 线程里跑，超时抛 TimeoutError；超时或失败都丢弃会话。"""
    _stuck[:] = [t for t in _stuck if t.is_alive()]
    if len(_stuck) >= MAX_STUCK_WORKERS:
        raise ProviderConnectionError(f"baostock 有 {len(_stuck)} 个查询线程超时未退出，暂停查询")
    box = {}

    def work():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — 交回调用方线程再抛
            box["error"] = exc

    worker = threading.Thread(target=work, name="baostock-raw", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        _stuck.append(worker)
        _discard_session()
        raise TimeoutError(f"baostock 查询 {timeout}s 未返回")
    if "error" in box:
        _discard_session()
        raise box["error"]
    return box["value"]


def _query_rows(name: str, params: dict) -> tuple[list, list]:
    """login（按需）+ 调用 bs.<name>(**params)；next()/get_row_data() 必须成对。"""
    import baostock as bs
    if not _logged_in:
        _login()
    rs = getattr(bs, name)(**params)
    if rs.error_code != "0":
        raise RuntimeError(f"baostock {name} 失败: {rs.error_code} {rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())  # 必须消费行，否则 next() 永真死循环
    return list(rs.fields), rows


def default_query(name: str, **params) -> tuple[list, list]:
    """串行 + 看门狗的 BaoStock 调用；返回 (fields, rows)。"""
    with _lock:
        return _run_with_watchdog(lambda: _query_rows(name, params), QUERY_TIMEOUT)


def _num(value) -> float | None:
    return None if value in ("", None) else float(value)


class BaostockRawProvider:
    name = "baostock"
    CONTRACT_VERSION = CONTRACT_VERSION

    def __init__(self, query=None):
        self._query = query or default_query

    def _call(self, name: str, **params) -> list[dict]:
        try:
            fields, rows = self._query(name, **params)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — 统一成 ProviderError，供采集器计入退避
            message = f"baostock {name}: {type(exc).__name__}: {exc}"
            if isinstance(exc, (TimeoutError, OSError)) or _NETWORK_CODE.search(str(exc)):
                raise ProviderConnectionError(message) from exc
            raise ProviderError(message) from exc
        return [dict(zip(fields, r)) for r in rows]

    def day_history(self, code, start, end) -> list:
        if kind_of(code) != "stock":
            raise ProviderUnsupported(f"baostock 冷备只服务 A 股个股日线：{code}")
        records = self._call("query_history_k_data_plus", code=_bs_code(code), fields=DAY_FIELDS,
                             start_date=start, end_date=end, frequency="d", adjustflag="3")
        batch = new_batch_id()
        rows = [RawDayRow(code, d["date"], _num(d["open"]), _num(d["high"]), _num(d["low"]),
                          _num(d["close"]), _num(d["volume"]), "share", _num(d["amount"]), "CNY",
                          _num(d["preclose"]), 0 if d["tradestatus"] == "1" else 1, "final", batch)
                for d in records]
        if any(not (start <= r.trade_date <= end) for r in rows):
            raise ProviderRangeError(f"{code} day 返回越出 {start}..{end}")
        return rows

    def calendar(self, year) -> list:
        records = self._call("query_trade_dates", start_date=f"{year}-01-01", end_date=f"{year}-12-31")
        return [CalendarRow("CN", d["calendar_date"], d["is_trading_day"] == "1") for d in records]
