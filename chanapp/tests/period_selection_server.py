"""Isolated real HTTP server for browser_period_selection_e2e.js.

Only the upstream provider is replaced by a deterministic raw-provider fixture. Business
routes, preferences, chart calculations, collector and SQLite writes stay real.
Run from the package parent: python -m chanapp.tests.period_selection_server DIR PORT FREQ
"""
from __future__ import annotations

import json
from dataclasses import replace
import os
import socket
import sys
from pathlib import Path

root, port, freq = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
root.mkdir(parents=True, exist_ok=True)
for key in ("WATCHLIST_PATH", "VIEW_LOG_PATH", "CHANAPP_CACHE_DIR", "ANALYSIS_CACHE_DIR"):
    os.environ.pop(key, None)
os.environ["COLLECTOR_ENABLED"] = "1"
from chanapp.engine.kline import bindings, collector, instance, sessions
from chanapp.engine.kline.providers.catalog import REGISTRY
# The deterministic fixture implements every minute grid; declare that capability
# so the real deployment validator can exercise a future m60-capable adapter.
cn_source = instance.InstanceConfig().markets["CN"].source
REGISTRY[cn_source] = replace(REGISTRY[cn_source], minute_freqs=("m5", "m15", "m60"))
for market in instance.InstanceConfig().markets.values():
    for key in REGISTRY[market.source].credentials:
        os.environ[key] = "offline-fixture"
config_path = root / "config.json"
config_path.write_text(json.dumps({"mode": "real", "instance_dir": str(root / "state"),
    "markets": {"CN": {"minute_fact_freq": None if freq == "none" else freq}},
    "quota": {"per_minute": 1000000, "per_day": 10000000}}))
os.environ["CHANAPP_INSTANCE_CONFIG"] = str(config_path)

# Deny accidental external traffic from any server-side adapter.
_original_connect = socket.socket.connect

def local_connect(sock, address):
    if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise RuntimeError("offline browser fixture forbids outbound connections")
    return _original_connect(sock, address)

socket.socket.connect = local_connect
from chanapp.api import main
from chanapp.engine import data, period_preferences, llm
from chanapp.tests.test_kline_collector import Clock, DAY_VOL, _list_since
from chanapp.tests.test_kline_viewing_tracking import Recording, at

class BrowserProvider(Recording):
    def minute_history(self, code, fact_freq, start, end, *, now):
        rows = super().minute_history(code, fact_freq, start, end, now=now)
        # Preserve the fixture daily volume at every configured minute grid.
        return [replace(row, volume=DAY_VOL / len(sessions.slots("CN", fact_freq))) for row in rows]


# 模型是外部边界；浏览器验证真实 API、组合提示与缓存，不访问 LLM。
os.environ['LLM_API_KEY'] = 'offline-fixture'
def analyze(prompt):
    frames = json.loads(prompt.split('数据（JSON）：\n')[1])['timeframes']
    return json.dumps({'current_state': '本次：' + '/'.join(frames), 'scenarios': []})
llm.analyze = analyze

provider = BrowserProvider()
clock = Clock(at("2026-09-26", 20).timestamp())
worker = None
main.engine_feed = None
main.engine_search = None

def start(watchlist_fn):
    global worker
    paths = main.instance_paths.current(data.CACHE_DIR)
    paths.watchlist.write_text(json.dumps([{"code": "sh600036", "name": "示例股票"}]))
    _list_since(paths.cache_dir, ["sh600036", "sh000001"], "2023-01-04")
    source = bindings.binding("CN", "stock", bindings.F.MINUTE_HISTORY).primary
    worker = collector.Collector(paths.cache_dir, providers={source: provider}, clock=clock,
                                 watchlist_fn=watchlist_fn,
                                 minute_enabled=period_preferences.current().minute_enabled)
    collector._shared[str(paths.cache_dir.resolve())] = worker
    return False  # Deliberately advance scheduler from the test; no background races.

data.start_collector = start

@main.app.get("/__test/evidence")
def evidence():
    rows = worker.conn().execute("SELECT code, fact_freq, COUNT(*) AS count, MIN(slot_end) AS oldest, "
                                 "MAX(slot_end) AS newest FROM current_minute_bars GROUP BY code, fact_freq").fetchall()
    return {"calls": provider.calls, "minute_facts": [dict(row) for row in rows]}

@main.app.post("/__test/history")
def history():
    clock.t += 60
    return worker.history_tick(at("2026-09-26", 20, int((clock.t % 3600) // 60)))

# Register fixture endpoints ahead of the static catch-all mount.
main.app.router.routes[:] = main.app.router.routes[-2:] + main.app.router.routes[:-2]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="warning")
