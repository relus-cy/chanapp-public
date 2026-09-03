"""行情数据门面（公开演示版）。

完整版门面在背后接入可插拔数据源模块；公开仓库不含数据接入层，
get_bars 改为读取内置演示数据（engine/demo_data/ 静态历史快照），
签名与返回结构与完整版冻结契约一致：
{code, freq, bars, source, fqf, fetch_time, degraded, degraded_note,
from_cache, cache_ttl, stale}；bars 元素 {dt,open,high,low,close,volume}，
日线 dt "YYYY-MM-DD"、分钟 "YYYY-MM-DD HH:mm"，A 股 volume 单位为手。
v1.3.1 起契约增量扩展 stale 键（演示数据恒为 False）。

未覆盖的 (code, freq) 组合抛 DataSourceError，由 API 层统一转 502。
接入自有数据源时保持本契约不动，上层（结构/信号/证据/缓存/路由）无需改动。
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

_DEMO_DIR = Path(__file__).resolve().parent / "demo_data"

# (code, freq) → demo_data/ 下的 CSV（表头 dt,open,high,low,close,volume，列序以表头为准）
_DEMO_SETS = {
    ("sh000001", "day"): "sh000001_day_qfq.csv",
    ("sh000688", "m30"): "sh000688_m30.csv",
    ("sz399006", "m30"): "sz399006_m30.csv",
}


class DataSourceError(Exception):
    """演示数据未覆盖该 (code, freq) 组合。"""


def get_bars(code: str, freq: str) -> dict:
    key = (code, freq)
    if key not in _DEMO_SETS:
        raise DataSourceError(
            f"演示数据未覆盖 {code}/{freq}：公开仓库仅内置静态演示快照，"
            "完整数据接入层未包含（见 README「数据说明」）")
    with open(_DEMO_DIR / _DEMO_SETS[key], encoding="utf-8") as f:
        bars = [
            {"dt": r["dt"], "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]),
             "volume": float(r["volume"])}
            for r in csv.DictReader(f)
        ]
    return {
        "code": code, "freq": freq, "bars": bars,
        "source": "demo", "fqf": "qfq",
        "fetch_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "degraded": False, "degraded_note": "",
        "from_cache": False, "cache_ttl": 0,
        "stale": False,
    }
