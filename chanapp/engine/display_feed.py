"""Display feeds with cache SWR and independently backed-off baseline sources.

quotes.json retains its transferable data mapping. Refreshes publish only rows
from the current fetch; source timestamps are independent of cache timestamps.
"""

_MARKET_PREFIX = {0: "sz", 1: "sh", 116: "hk"}


def limit_ratio(code: str) -> float | None:
    if code.startswith(("sh000", "sz399")):
        return None
    if code.startswith("hk"):  # 港股无涨跌停
        return None
    if code.startswith(("sh68", "sz30")):
        return 0.20
    return 0.10


from .feeds import baseline_resilience as _resilience


def _num(v):
    return _resilience.number(v)


def parse_quotes(payload: dict) -> dict:
    out = {}
    for row in (payload.get("data") or {}).get("diff") or []:
        f12, f13 = row.get("f12"), row.get("f13")
        if not f12:
            continue
        code = f12 if str(f12).startswith("BK") else _MARKET_PREFIX.get(f13, "") + str(f12)
        price, prev = _resilience.price(row.get("f2")), _resilience.price(row.get("f18"))
        if price == 0:
            price = None  # 上游异常行的字面 0 按缺失处理；pct=0 是平盘真值，不动
        ratio = None if code.startswith("BK") else limit_ratio(code)
        limit_up = bool(price is not None and prev and ratio and price >= round(prev * (1 + ratio), 2))
        out[code] = {"price": price, "pct": _num(row.get("f3")), "prev_close": prev,
                     "name": row.get("f14", ""), "limit_up": limit_up,
                     "source": "baseline_display", "source_ts": _num(row.get("f124"))}
    return out


def parse_f10(data: dict) -> dict:
    return {
        "price": _resilience.price(data.get("f43")), "amount": _num(data.get("f48")),
        "outer": _num(data.get("f49")), "inner": _num(data.get("f161")),
        "volume_ratio": _num(data.get("f50")),
        "limit_up_price": _resilience.price(data.get("f51")), "limit_down_price": _resilience.price(data.get("f52")),
        "total_mv": _num(data.get("f116")), "float_mv": _num(data.get("f117")),
        "industry": data.get("f127") or "",
        "concepts": [c for c in (data.get("f129") or "").split(",") if c],
        "pe_ttm": _num(data.get("f164")), "pb": _num(data.get("f167")),
        "turnover": _num(data.get("f168")), "amplitude": _num(data.get("f171")),
        "board_code": data.get("f198") or "",
    }


def parse_flow(data: dict) -> dict:
    return {"main": _num(data.get("f137")), "super": _num(data.get("f140")),
            "large": _num(data.get("f143")), "medium": _num(data.get("f146")),
            "small": _num(data.get("f149"))}


def parse_board_pct(payload: dict) -> float | None:
    rows = (payload.get("data") or {}).get("diff") or []
    return _num(rows[0].get("f3")) if rows else None


# ---------- 抓取/缓存/退避层 ----------

import json
import logging
import time
from pathlib import Path

from .kline import http as _http
from . import cache_store, data_identity, swr
from .kline.instance import is_demo

THROTTLE_INTERVAL = 0.5
BACKOFF_SECONDS = 30          # 退避冷却（连续 BACKOFF_AFTER_FAILURES 次失败才触发）
BACKOFF_AFTER_FAILURES = 2    # 单次抖动不退避
QUOTE_TTL = 60                  # 与页面报价轮询、采集器盘中增量同为 60 秒
F10_TTL = 300

_HEADERS = {"Accept": "*/*", "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids={secids}&fields={fields}"
_STOCK_URL = "https://push2.eastmoney.com/api/qt/stock/get?fltt=2&secid={secid}&fields={fields}"
_QUOTE_FIELDS = "f2,f3,f12,f13,f14,f18,f124"
_F10_FIELDS = ("f124,f43,f48,f49,f50,f51,f52,f116,f117,f127,f129,f161,f164,f167,f168,f171,f198,"
               "f135,f136,f137,f138,f139,f140,f141,f142,f143,f144,f145,f146,f147,f148,f149")

import functools

_throttle = functools.partial(_http.throttle, THROTTLE_INTERVAL)  # 限速统一由本模块做

log = logging.getLogger(__name__)


def _secid(code: str) -> str:
    market = {"sh": "1", "sz": "0", "hk": "116"}.get(code[:2])
    if market is None:
        raise ValueError(f"unsupported code prefix: {code}")
    return f"{market}.{code[2:]}"


class Push2Blocked(RuntimeError):
    pass


_primary_backoff = swr.BackoffPolicy(BACKOFF_AFTER_FAILURES, BACKOFF_SECONDS,
                                     scope=swr.Scope.GLOBAL, count_source=swr.CountSource.EMBEDDED,
                                     never_count=(Push2Blocked,))
_backup_backoff = swr.BackoffPolicy(BACKOFF_AFTER_FAILURES, BACKOFF_SECONDS,
                                    scope=swr.Scope.GLOBAL, count_source=swr.CountSource.EMBEDDED,
                                    never_count=(Push2Blocked,))


def _reset_blocked() -> None:  # 测试辅助
    _primary_backoff.reset()
    _backup_backoff.reset()


def _check_blocked() -> None:
    if _primary_backoff.blocked():
        raise Push2Blocked(f"push2 退避中（连续断连 {BACKOFF_SECONDS}s 冷却）")


def _record_success() -> None:
    _primary_backoff.record_success(None)


def _record_failure() -> None:
    """连续 BACKOFF_AFTER_FAILURES 次失败才进入退避（单次抖动不退避）。"""
    _primary_backoff.record_failure(None)


def _sources_blocked() -> bool:
    return _primary_backoff.blocked() and _backup_backoff.blocked()


def _cache_dir() -> Path:
    """显示层缓存固定在门面缓存根下的 display/。

    调用时经模块属性读取 CACHE_DIR（不在导入时绑定），测试对门面 CACHE_DIR 的隔离才能生效。
    """
    from . import data as engine_data
    d = Path(engine_data.CACHE_DIR) / "display"
    d.mkdir(parents=True, exist_ok=True)
    return d


_session = None


def _get_session(refresh: bool = False):
    """进程级 curl_cffi Session（lazy import，仿浏览器指纹——上游对 urllib
    指纹按 IP 动态 reset，2026-09-03 实测）；refresh=True 丢弃重建（换边缘节点）。"""
    global _session
    if _session is None or refresh:
        from curl_cffi import requests as creq
        old, _session = _session, creq.Session(impersonate="chrome")
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
    return _session


def _fetch_json(url: str) -> dict:
    """限速 + Accept 头（stock/get 缺 Accept 直接 TCP reset）。可被测试 mock。

    失败计数/退避由调用方（get_quotes/get_f10 冷路径与后台刷新线程）统一记录，
    本层不置退避。
    """
    _check_blocked()
    _throttle()
    r = _get_session().get(url, headers=_HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def _read_cache(name: str):
    p = _cache_dir() / name
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("data"), dict) or not cache_store.valid_timestamp(obj.get("ts")):
        return None
    return obj, obj["ts"]


def _write_cache(name: str, data, requested_codes=None) -> str:
    identity = {"data": data, "source": "baseline_display", "normalization": "display-v1"}
    if requested_codes is not None:
        identity["requested_codes"] = sorted(set(requested_codes))
    result = cache_store.publish_json(_cache_dir() / name,
                             {"ts": time.time(), "data": data,
                              **({"requested_codes": sorted(set(requested_codes))} if requested_codes is not None else {}),
                              "source": "baseline_display", "normalization": "display-v1",
                              "data_version": data_identity.version([], identity)})
    return cache_store.require_published(result)


def _fetch_quotes(codes: list[str]) -> dict:
    """同步抓取批量快照；源调用层统一记录失败与退避。"""
    payload = _fetch_json(_ULIST_URL.format(secids=",".join(_secid(c) for c in codes),
                                            fields=_QUOTE_FIELDS))
    return parse_quotes(payload)


def _fetch_f10(code: str) -> dict:
    """向上游同步抓取个股 F10+资金流+板块涨幅（冷路径与后台刷新共用）。"""
    data = (_fetch_json(_STOCK_URL.format(secid=_secid(code), fields=_F10_FIELDS)).get("data") or {})
    f10 = parse_f10(data)
    industry_pct = None
    if f10["board_code"]:
        try:
            payload = _fetch_json(_ULIST_URL.format(secids=f"90.{f10['board_code']}", fields="f3,f12"))
            # 只取目标板块行（真实响应单行；多行 payload 下避免串到首行）
            rows = [r for r in (payload.get("data") or {}).get("diff") or []
                    if r.get("f12") == f10["board_code"]]
            industry_pct = parse_board_pct({"data": {"diff": rows}})
        except Exception:
            log.warning("行业涨幅暂不可用")
    return {"f10": f10, "flow": parse_flow(data), "industry_pct": industry_pct,
            "source": "baseline_display", "source_ts": _num(data.get("f124"))}


def _backup_quotes(codes):
    from .feeds.baseline_backup import fetch_quotes
    return fetch_quotes(codes)


def _backup_f10(code):
    from .feeds.baseline_backup import fetch_f10
    return fetch_f10(code)


def _call_source(fetch, *args, backup=False, usable=None):
    if backup:
        if _backup_backoff.blocked():
            raise Push2Blocked("备用显示源暂时退避")
    else:
        _check_blocked()
    try:
        result = fetch(*args)
        if not result or (usable is not None and not usable(result)):
            raise RuntimeError("显示源返回空数据")
    except Push2Blocked:
        raise
    except Exception:
        if backup:
            _backup_backoff.record_failure(None)
        else:
            _record_failure()
        raise
    if backup:
        _backup_backoff.record_success(None)
    else:
        _record_success()
    return result


def _resilient_quotes(codes):
    primary_error = None
    try:
        rows = _call_source(_fetch_quotes, codes, usable=lambda rows: bool(_resilience.quote_rows(rows, codes, "baseline_display")))
    except Exception as exc:
        rows, primary_error = {}, exc
    rows = _resilience.quote_rows(rows, codes, "baseline_display")
    missing = [code for code in codes if code not in rows]
    if missing:
        try:
            backup = _call_source(_backup_quotes, missing, backup=True,
                                  usable=lambda rows: bool(_resilience.quote_rows(rows, missing, "baseline_backup")))
            rows.update(_resilience.quote_rows(backup, missing, "baseline_backup"))
        except Exception:
            if not rows:
                raise (primary_error if isinstance(primary_error, Push2Blocked) else RuntimeError("显示快照无可用数据")) from primary_error
    return rows


def _usable_f10(result):
    merged = _resilience.merge_f10(result, {})
    return any(merged["f10"].get(key) is not None for key in _resilience.BASIC_FIELDS)


def _resilient_f10(code):
    primary_error = None
    try:
        primary = _call_source(_fetch_f10, code, usable=_usable_f10)
    except Exception as exc:
        primary, primary_error = {}, exc
    backup = {}
    if any(_resilience.merge_f10(primary, {})['f10'].get(key) is None for key in _resilience.BASIC_FIELDS):
        try:
            backup = _call_source(_backup_f10, code, backup=True, usable=_usable_f10)
        except Exception:
            if not primary:
                raise (primary_error if isinstance(primary_error, Push2Blocked) else RuntimeError("显示资料无可用数据")) from primary_error
    result = _resilience.merge_f10(primary, backup)
    if not any(result['f10'].get(key) is not None for key in _resilience.BASIC_FIELDS):
        raise RuntimeError("显示资料无可用数据")
    return result


def _quotes_response(cached, codes, stale=False):
    rows = {code: (_resilience.stamp(row, 'baseline_display') if 'source' in row else row)
            for code, row in cached['data'].items() if code in codes}
    missing = [code for code in codes if code not in rows or _resilience.price(rows[code].get('price')) is None]
    return {'quotes': rows, 'missing_codes': missing,
            'meta': _resilience.metadata(rows.values(), ['quotes'] if missing else []),
            'degraded': stale, 'ts': cached['ts']}


def _baseline_quotes(codes, require_fresh=False, cache_name="quotes.json"):
    """cache_name：自选表用 quotes.json；单代码报价（get_quote）用各自的文件，不覆盖自选缓存。"""
    codes = list(dict.fromkeys(codes))

    def hit_when(payload, ts):
        return ((time.time() - ts) <= QUOTE_TTL and
                set(codes).issubset(payload.get('requested_codes', payload['data'])))

    def read():
        return _read_cache(cache_name)

    def sync_fetch():
        quotes = _resilient_quotes(codes) if codes else {}
        status = _write_cache(cache_name, quotes, requested_codes=codes)
        if status == "superseded":
            newer = _read_cache(cache_name)
            if newer is None:
                raise cache_store.CachePublicationError("更新后的快照缓存不可用")
            return newer[0]
        return {"ts": time.time(), "data": quotes}

    result = swr.serve(
        swr.make_key(cache_name),
        QUOTE_TTL, swr.SwrIO(read=read, sync_fetch=sync_fetch),
        hit_when=hit_when, require_fresh=require_fresh,
        serve_stale_on_cold_failure=True,
        should_trigger=lambda key: not _sources_blocked(),
        log_failure=lambda key, exc: log.warning("后台刷新失败 %s", cache_name, exc_info=True),
        domain=cache_name)
    return _quotes_response(result.payload, codes, stale=result.stale)


def _baseline_f10(code, require_fresh=False):
    name = f"f10_{code}.json"

    def read():
        return _read_cache(name)

    def sync_fetch():
        result = _resilient_f10(code)
        if _write_cache(name, result) == "superseded":
            newer = _read_cache(name)
            if newer is None:
                raise cache_store.CachePublicationError("更新后的资料缓存不可用")
            return newer[0]
        return {"ts": time.time(), "data": result}

    result = swr.serve(
        swr.make_key(name),
        F10_TTL, swr.SwrIO(read=read, sync_fetch=sync_fetch),
        require_fresh=require_fresh,
        should_trigger=lambda key: not _sources_blocked(),
        log_failure=lambda key, exc: log.warning("后台刷新失败 %s", name, exc_info=True),
        domain=name)
    cached = result.payload
    return {**_resilience.refresh_f10_metadata(cached['data']),
            'degraded': result.stale, 'ts': cached['ts']}


def get_quotes(codes: list[str], *, require_fresh=False) -> dict:
    if is_demo():
        return _demo_quotes(codes)
    result = _baseline_quotes(codes, require_fresh)
    result["data_version"] = data_identity.version([], {
        "data": result["quotes"], "source": "baseline_display", "normalization": "display-v1",
        "requested_codes": sorted(set(codes))})
    return result


def get_quote(code: str, *, require_fresh=False) -> dict:
    """单代码报价（搜索查看的非自选代码）：与 get_quotes 同一口径与 TTL，缓存按代码单独存放，不覆盖自选报价缓存。"""
    if is_demo():
        return _demo_quotes([code])
    result = _baseline_quotes([code], require_fresh, cache_name=f"quote_{code}.json")
    result["data_version"] = data_identity.version([], {
        "data": result["quotes"], "source": "baseline_display", "normalization": "display-v1",
        "requested_codes": [code]})
    return result


def get_f10(code: str, *, require_fresh=False) -> dict:
    if is_demo():
        return {"f10": {}, "flow": {}, "industry_pct": None, "degraded": False,
                "ts": 0, "meta": {"mode": "demo"}}
    result = _baseline_f10(code, require_fresh)
    content = {k: result.get(k) for k in ("f10", "flow", "industry_pct")}
    result["data_version"] = data_identity.version([], {
        "data": content, "source": "baseline_display", "normalization": "display-v1"})
    return result


def _demo_quotes(codes):
    from . import data
    quotes = {code: q for code in codes if (q := data.quote(code)) is not None}
    return {"quotes": quotes, "degraded": False, "ts": 0, "meta": {"mode": "demo"},
            "missing_codes": [code for code in codes if code not in quotes], "invalid_codes": []}
