"""交易时段自选股缓存预热：daemon 线程每 60s 唤醒一次，cn/hk 任一在开市时
遍历 watchlist × (day, m30, m60) 调 get_bars——TTL 内命中仅本地读（~2ms），
过期的才发 HTTP（0.3s 限速自然排队）。单条异常记录不中断。

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
from chanapp.engine.session import is_session_open

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
    now = now or datetime.now()
    if not (is_session_open(now, "cn") or is_session_open(now, "hk")):
        return {"skipped": True, "ok": 0, "errors": []}
    ok, errors = 0, []
    for code in codes_fn():
        for freq in FREQS:
            try:
                get_bars_fn(code, freq)
                ok += 1
            except Exception as e:
                errors.append(f"{code}/{freq}: {e}")
    return {"skipped": False, "ok": ok, "errors": errors}


def _loop() -> None:
    while True:
        time.sleep(INTERVAL)
        try:
            r = warm_once()
            if r["errors"]:
                print(f"[warmer] 本轮 {r['ok']} 成功，{len(r['errors'])} 失败：{r['errors']}")
        except Exception as e:
            print(f"[warmer] 异常：{e}")


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
