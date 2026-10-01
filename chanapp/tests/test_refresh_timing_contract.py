"""刷新时间关系：防止正常减速被报 stale、页面空轮询、报价缓存漂移和查看租期断档。

后端读取运行常量，前端通过现有 Node 沙盒读取真实定时器；不复制时间值。
这是跨 Python/JS 的关系测试，运行需要 node（与 JS 完整套件相同）。
"""
import json
import subprocess
import unittest
from pathlib import Path

from chanapp.engine import display_feed
from chanapp.engine.kline import config


ROOT = Path(__file__).resolve().parents[1]


class RefreshTimingContractTests(unittest.TestCase):
    def test_refresh_covers_collection_and_keeps_viewing_alive(self):
        frontend = subprocess.run(
            ["node", "-e", """
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { appSource } = require('./tests/support/dom.js');
const { timerEnv, loadEnv } = require('./tests/support/app.js');
const intervals = timerEnv()._harness.intervalMs;
assert.equal(intervals.length, 2, '图表和报价各注册一个轮询定时器');
const timeouts = [];
const env = loadEnv({setTimeout: (fn, ms) => { timeouts.push(ms); return timeouts.length; }});
// 覆盖共享桩的默认超时，使用 app.js 的真实声明，再观察 load() 实际使用的值。
const declaration = appSource.match(/\\bvar CHART_TIMEOUT_MS\\s*=\\s*[^;]+;/);
assert.ok(declaration, '未找到图表请求超时声明');
vm.runInContext(declaration[0], env);
env.load({refresh: true});
assert.equal(timeouts.length, 1, '常规图表请求注册一个超时');
console.log(JSON.stringify({chart: intervals[0] / 1000, quote: intervals[1] / 1000,
                            timeout: timeouts[0] / 1000}));
"""],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(frontend.returncode, 0, frontend.stderr)
        timing = json.loads(frontend.stdout)

        # 额度触发后间隔加倍；QUOTA_SLOWDOWN_RATIO 是触发阈值，不是倍数。
        # 个股与指数的加倍调度行为由 test_kline_intraday_cadence 覆盖。
        for kind, interval in (("stock", config.INTRADAY_INTERVAL_S),
                               ("index", config.INTRADAY_INDEX_INTERVAL_S)):
            with self.subTest(relation="stale_after_slowed_collection", kind=kind):
                self.assertGreater(
                    config.STALE_IN_SESSION_S, 2 * interval,
                    "盘中过期阈值必须大于额度加倍后的采集间隔",
                )
            for poll in ("chart", "quote"):
                with self.subTest(relation="poll_not_faster_than_collection", kind=kind, poll=poll):
                    self.assertGreaterEqual(
                        timing[poll], interval,
                        "页面刷新不得快于普通采集间隔",
                    )
        with self.subTest(relation="quote_cache_matches_poll"):
            self.assertEqual(
                display_feed.QUOTE_TTL, timing["quote"],
                "报价缓存时长必须等于报价刷新间隔",
            )
        with self.subTest(relation="viewing_survives_missed_refresh"):
            self.assertGreaterEqual(
                config.VIEWING_WINDOW_S, 2 * timing["chart"] + timing["timeout"],
                "查看租期必须覆盖两次图表刷新加一次请求超时",
            )


if __name__ == "__main__":
    unittest.main()
