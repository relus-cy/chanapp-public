"""上游不可自愈失败（ProviderAuthError）：整个来源停发请求，每 5 分钟放行一个请求作探测，探测成功即恢复；
经 /api/status 原样带出供应商错误码与原话。不按标的计失败，不把缺口推向 known_gap。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline import collector, facts
from chanapp.engine.kline.providers.raw import ProviderAuthError, ProviderError
from chanapp.tests.test_kline_collector import FACT, Clock, FakeProvider, _enable_collector

CODES = ["sh600036", "sz300209", "sz000001"]


class Rejecting(FakeProvider):
    """rejection 非空时每个请求都被供应商以该错误拒绝；置 None 即恢复正常返回。"""

    def __init__(self, rejection):
        super().__init__()
        self.rejection = rejection

    def minute_history(self, code, fact_freq, start, end, *, now):
        if self.rejection is not None:
            self.calls.append((fact_freq, code, start, end))
            raise self.rejection
        return super().minute_history(code, fact_freq, start, end, now=now)


class UpstreamBlockTests(unittest.TestCase):
    def setUp(self):
        _enable_collector(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.start = datetime(2026, 9, 26, 20, 0)
        self.clock = Clock(self.start.timestamp())

    def make(self, provider):
        c = collector.Collector(self.dir, providers={"mairui": provider}, clock=self.clock,
                                watchlist_fn=lambda: list(CODES))
        conn = facts.open_facts(self.dir / facts.DB_NAME)
        self.addCleanup(conn.close)
        with facts.write_txn(conn):
            for code in CODES:
                facts.record_gap(conn, code, FACT, "2026-08-03 09:30", "2026-08-03 15:00", "backfill")
        return c, conn

    def upstream(self, c):
        return c.status(CODES, now=c.now())["upstream"]

    def test_quota_rejection_stops_whole_source_and_probes_every_five_minutes(self):
        p = Rejecting(ProviderAuthError("101", "Licence证书当日请求已超限"))
        c, conn = self.make(p)
        c.drain_gaps(max_requests=10)
        self.assertEqual(len(p.calls), 1)                     # 第一只被拒后，同源其余标的零请求
        self.assertEqual(self.upstream(c), [{
            "source": "mairui", "markets": ["CN"], "vendor_code": "101",
            "vendor_message": "Licence证书当日请求已超限", "since": "2026-09-26T20:00:00",
            "next_probe_at": "2026-09-26T20:05:00"}])
        self.assertEqual([r["attempts"] for r in conn.execute("SELECT attempts FROM coverage_gaps")], [0, 0, 0])

        self.clock.t += 299
        c.drain_gaps(max_requests=10)
        self.assertEqual(len(p.calls), 1)                     # 未到 5 分钟：不探测
        self.clock.t += 1
        c.drain_gaps(max_requests=10)
        self.assertEqual(len(p.calls), 2)                     # 到点恰放行一个请求，仍被拒
        self.assertEqual(self.upstream(c)[0]["next_probe_at"], "2026-09-26T20:10:00")

        p.rejection = None
        self.clock.t += 300
        c.drain_gaps(max_requests=10)
        self.assertEqual(self.upstream(c), [])                # 探测成功即恢复，其余标的随即照常取数
        self.assertEqual(sorted(call[1] for call in p.calls[2:]), sorted(CODES))

    def test_missing_credentials_block_without_spending_quota(self):
        p = Rejecting(ProviderAuthError("未设置", "MAIRUI_LICENCE 未设置"))
        c, _ = self.make(p)
        c.drain_gaps(max_requests=10)
        self.assertEqual(self.upstream(c)[0]["vendor_code"], "未设置")
        self.assertEqual(c._budget("mairui").used(), 0)      # 一个请求都没发出，不占自限额度

    def test_ordinary_failures_do_not_block_the_source(self):
        p = Rejecting(ProviderError("HTTP 404 /hsstock/history"))   # 没有供应商错误码：标的级
        c, _ = self.make(p)
        c.drain_gaps(max_requests=10)
        self.assertEqual(len(p.calls), len(CODES))            # 其余标的照常请求
        self.assertEqual(self.upstream(c), [])


if __name__ == "__main__":
    unittest.main()
