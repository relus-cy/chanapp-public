"""来源绑定：单一事实源、手动切换推进代次、探针结论随契约失效（spec §5.1、§8）。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chanapp.engine.kline import bindings, facts
from chanapp.engine.kline.rows import FetchItem


class BindingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = facts.open_facts(Path(tmp.name) / facts.DB_NAME)
        self.addCleanup(self.conn.close)

    def test_every_key_bound_once(self):
        keys = [(b.market, b.kind, b.item) for b in bindings.BINDINGS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(bindings.binding("CN", "stock", FetchItem.DAY_HISTORY).cold, "baostock")
        self.assertEqual(bindings.binding("CN", "index", FetchItem.DAY_HISTORY).cold, "pytdx")
        self.assertEqual(bindings.binding("HK", "stock", FetchItem.DAY_HISTORY).cold, "yahoo")
        self.assertEqual(bindings.binding("CN", "stock", FetchItem.MINUTE_HISTORY).minute_fact_freq, "m15")   # 2026-09-30 起原生 m15
        self.assertIsNone(bindings.binding("CN", "stock", FetchItem.PREOPEN_REF).cold)

    def test_default_active_is_primary_gen_one(self):
        self.assertEqual(bindings.active(self.conn, "CN", "stock", FetchItem.DAY_HISTORY), ("mairui", 1))

    def test_switch_advances_gen_and_rejects_unknown_source(self):
        gen = bindings.switch(self.conn, "CN", "stock", FetchItem.DAY_HISTORY, "baostock",
                              reason="P1 fail drill")
        self.assertEqual(gen, 2)
        self.assertEqual(bindings.active(self.conn, "CN", "stock", FetchItem.DAY_HISTORY), ("baostock", 2))
        self.assertEqual(facts.binding_gen(self.conn, "CN", "stock", "day_history"), 2)
        with self.assertRaises(ValueError):
            bindings.switch(self.conn, "CN", "stock", FetchItem.DAY_HISTORY, "tencent", reason="x")

    def test_instance_source_change_advances_generation_once(self):
        # 实例换来源：推进一次代次并记下新来源，重复启动不再推进
        key = "instance_source:CN:stock:day_history"
        with facts.write_txn(self.conn):
            facts.set_setting(self.conn, key, "baostock")
        bindings.sync_sources(self.conn)
        self.assertEqual(bindings.active(self.conn, "CN", "stock", FetchItem.DAY_HISTORY), ("mairui", 2))
        self.assertEqual(facts.setting(self.conn, key), "mairui")
        bindings.sync_sources(self.conn)
        self.assertEqual(facts.binding_gen(self.conn, "CN", "stock", "day_history"), 2)
        self.assertEqual(facts.binding_gen(self.conn, "CN", "stock", "minute_history"), 1)

    def test_unchanged_instance_keeps_operator_cold_standby(self):
        bindings.switch(self.conn, "CN", "stock", FetchItem.DAY_HISTORY, "baostock", reason="ops")
        bindings.sync_sources(self.conn)
        self.assertEqual(bindings.active(self.conn, "CN", "stock", FetchItem.DAY_HISTORY), ("baostock", 2))

    def test_probe_pass_invalidated_by_contract_change(self):
        scope = {"endpoint": "history d/n", "sample": "12x10y", "criteria_version": "p1-v1"}
        with mock.patch.object(bindings, "contract_hash", return_value="h1"):
            bindings.record_probe(self.conn, "P1", "CN", "stock", FetchItem.DAY_HISTORY, "mairui",
                                  "pass", scope)
            self.assertEqual(bindings.verdict_of(self.conn, "CN", "stock",
                                                 FetchItem.DAY_HISTORY, "mairui"), "pass")
        with mock.patch.object(bindings, "contract_hash", return_value="h2"):
            self.assertEqual(bindings.verdict_of(self.conn, "CN", "stock",
                                                 FetchItem.DAY_HISTORY, "mairui"), "pending")

    def test_probe_scope_must_be_complete(self):
        with self.assertRaises(ValueError):
            bindings.record_probe(self.conn, "P1", "CN", "stock", FetchItem.DAY_HISTORY, "mairui",
                                  "pass", {"endpoint": "x"})

    def test_switch_to_hk_cold_freezes_vendor_cache_and_switch_back_keeps_it(self):
        from chanapp.engine.kline import hk_vendor_qfq as vq
        bar = {"trade_date": "2026-09-24", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
               "volume_unit": "share"}
        vq.publish(self.conn, "hk00700", "day", [bar], closed_through="2026-09-24")
        bindings.switch(self.conn, "CN", "stock", FetchItem.DAY_HISTORY, "baostock", reason="drill")
        self.assertEqual(vq.cache_version(self.conn, "hk00700")[0][2], 0)          # A 股切换与港股缓存无关
        bindings.switch(self.conn, "HK", "stock", FetchItem.MINUTE_HISTORY, "yahoo", reason="drill")
        self.assertEqual(vq.cache_version(self.conn, "hk00700")[0][2], 1)
        bindings.switch(self.conn, "HK", "stock", FetchItem.MINUTE_HISTORY, "longbridge", reason="drill")
        self.assertEqual(vq.cache_version(self.conn, "hk00700")[0][2], 1)          # 解冻只由重取成功完成

