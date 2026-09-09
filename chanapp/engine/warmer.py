"""交易时段自选股缓存预热：daemon 线程每 60s 唤醒一次，cn/hk 任一在开市时
遍历 watchlist × (day, m30, m60) 调 get_bars——TTL 内命中仅本地读（~2ms）；
过期 key 由 get_bars 秒回旧数据并触发后台异步刷新（同 key 防踩踏，v1.3.1 起），
预热职责由后台线程完成，warm_once 本身不再现场等抓取（errors 仅含首冷失败）。
单条异常记录不中断；切换提交后旧身份当轮立即终止（obsolete）。版本缓存的
保留清理由本循环每日最多执行一轮（cache_store.collect_versions）。

开关：WARMER_ENABLED（默认 1，置 0 关闭；测试与本地调试时关闭）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from chanapp.engine import data as engine_data
from chanapp.engine import supply
from chanapp.engine.session import is_session_open

try:
    from chanapp.engine import cache_store
    _Obsolete = cache_store.ObsoletePublication
except ImportError:
    # 公开演示不含版本化缓存层：该异常永远不会被抛出，GC 职责随之关闭。
    cache_store = None
    class _Obsolete(Exception):
        pass

_PKG_ROOT = Path(__file__).resolve().parent.parent
FREQS = ("day", "m30", "m60")
INTERVAL = 60

_started = False
_start_lock = threading.Lock()


def is_enabled() -> bool:
    return os.environ.get("WARMER_ENABLED", "1") != "0"


def _watchlist_codes() -> list[str]:
    p = Path(os.environ.get("WATCHLIST_PATH") or _PKG_ROOT / "watchlist.json")
    if not p.exists():
        return []
    return [w["code"] for w in json.loads(p.read_text(encoding="utf-8"))]


def warm_once(now: datetime | None = None,
              get_bars_fn=engine_data.get_bars,
              codes_fn=_watchlist_codes) -> dict:
    """交易时段（cn/hk 任一）遍历预热一轮；非时段跳过。"""
    permit = supply.capture_write_permit()  # 单次捕获，快照与许可同源
    with supply.use(permit.snapshot, permit):
        now = now or datetime.now()
        if not (is_session_open(now, "cn") or is_session_open(now, "hk")):
            return {"skipped": True, "ok": 0, "errors": []}
        ok, errors = 0, []
        for code in codes_fn():
            for freq in FREQS:
                try:
                    get_bars_fn(code, freq)
                    ok += 1
                except _Obsolete:
                    # 切换已提交：以旧身份继续预热无意义，当轮到此为止
                    return {"skipped": False, "ok": ok, "errors": errors, "obsolete": True}
                except Exception as e:
                    errors.append(f"{code}/{freq}: {e}")
        return {"skipped": False, "ok": ok, "errors": errors}


_GC_INTERVAL_S = 86400
_last_gc = 0.0


def _maybe_collect(now: float, collect) -> bool:
    """缓存版本保留清理：每天最多一轮，失败计入下一天重试。"""
    global _last_gc
    if now - _last_gc < _GC_INTERVAL_S:
        return False
    _last_gc = now
    collect()
    return True


def _loop() -> None:
    while True:
        time.sleep(INTERVAL)
        try:
            r = warm_once()
            if r["errors"]:
                print(f"[warmer] 本轮 {r['ok']} 成功，{len(r['errors'])} 失败：{r['errors']}")
        except Exception as e:
            print(f"[warmer] 异常：{e}")
        if cache_store is not None:
            try:
                _maybe_collect(time.monotonic(),
                               lambda: cache_store.collect_versions(engine_data.CACHE_DIR))
            except Exception as e:
                print(f"[warmer] 缓存清理异常：{e}")


def start() -> bool:
    """启用且未启动过 → 起 daemon 线程，返回是否新启动。"""
    global _started
    if not is_enabled():
        return False
    with _start_lock:
        if _started:
            return False
        threading.Thread(target=_loop, name="chanapp-warmer", daemon=True).start()
        _started = True
        return True
