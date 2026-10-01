"""#38 实例配置的启动与读取契约（录制输入，不访问网络）。

失败方式（先列后测）：默认配置意外联网；格式/类型/未知字段静默接受；
非法来源或粒度进入采集；缺凭据迟到首取才失败或错误泄密；额度未生效；
旧绑定覆盖实例选择；粒度切换混读或令牌不变；作者覆盖改变原行为。
seams：load_instance、应用 lifespan、get_bars；实例目录的实际隔离属于 #39，未覆盖。
"""
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from chanapp.engine.kline import bindings, collector, config, facts, instance


class InstanceConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "instance.json"

    def load(self, value, env=None):
        self.path.write_text(json.dumps(value))
        return instance.load_instance(self.path, environ={} if env is None else env)

    def test_instance_defaults_come_from_binding_table(self):
        # 产品默认只有一处：实例默认与库级调用读到的绑定表一致
        for b in bindings._DEFAULT_BINDINGS:
            selected = instance.InstanceConfig().markets[b.market]
            self.assertEqual(selected.source, b.primary)
            if b.item in (bindings.F.MINUTE_HISTORY, bindings.F.MINUTE_LIVE):
                self.assertEqual(selected.minute_fact_freq, b.minute_fact_freq)

    def test_registry_matches_provider_capabilities(self):
        # 能力与凭据名必须与 provider 实现一致，否则启动校验放行、首取才失败
        from chanapp.engine.kline.providers import longbridge, mairui
        from chanapp.engine.kline.providers.registry import REGISTRY
        self.assertEqual(set(REGISTRY["mairui"].minute_freqs), set(mairui._MINUTE_PATH))
        self.assertEqual(REGISTRY["longbridge"].credentials, longbridge._CRED_NAMES)
        for name in REGISTRY["mairui"].credentials:
            with mock.patch.dict(os.environ, {name: "registry-probe"}):
                self.assertEqual(mairui.MairuiProvider()._licence, "registry-probe")

    def test_no_file_is_demo_with_product_defaults(self):
        settings = instance.load_instance(environ={})
        self.assertEqual(settings.mode, "demo")
        self.assertEqual(settings.markets["CN"].minute_fact_freq, "m15")
        self.assertEqual(settings.markets["HK"].minute_fact_freq, "m30")
        self.assertEqual((settings.per_minute, settings.per_day), (300, 1000))

    def test_invalid_config_is_rejected_before_startup(self):
        cases = [([], "object"), ({"mode": "live"}, "mode"),
                 ({"refresh": 60}, "refresh"), ({"quota": {"reserve": 0.2}}, "reserve"),
                 ({"quota": {"per_day": 0}}, "per_day"),
                 ({"quota": {"per_minute": True}}, "per_minute"),
                 ({"quota": {"per_day": 1.5}}, "per_day"),
                 ({"instance_dir": ""}, "instance_dir"),
                 ({"markets": {"US": {}}}, "US"),
                 ({"markets": {"CN": {"source": "missing"}}}, "source"),
                 ({"markets": {"CN": {"source": "longbridge"}}}, "CN"),
                 ({"markets": {"CN": {"minute_fact_freq": "m30"}}}, "m30"),
                 ({"markets": {"CN": {"minute_fact_freq": "m60"}}}, "m60"),
                 ({"markets": {"HK": {"minute_fact_freq": "m5"}}}, "m5"),
                 ({"markets": {"CN": {"source": "baostock", "minute_fact_freq": None}}}, "能力")]
        for value, message in cases:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                self.load(value)

    def test_real_requires_all_selected_credentials_without_exposing_values(self):
        with self.assertRaises(ValueError) as caught:
            self.load({"mode": "real"}, {"MAIRUI_LICENCE": "private-value"})
        self.assertIn("LONGBRIDGE_APP_KEY", str(caught.exception))
        self.assertIn("LONGBRIDGE_APP_SECRET", str(caught.exception))
        self.assertIn("LONGBRIDGE_ACCESS_TOKEN", str(caught.exception))
        self.assertNotIn("private-value", str(caught.exception))

    def test_author_override_preserves_bindings_and_budget(self):
        env = {key: "test-only" for key in ("MAIRUI_LICENCE", "LONGBRIDGE_APP_KEY",
                                           "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN")}
        settings = self.load({"mode": "real", "quota": {"per_day": 10000},
                              "instance_dir": "state"}, env)
        self.assertEqual(settings.mode, "real")
        self.assertEqual((settings.per_minute, settings.per_day), (300, 10000))
        self.assertEqual(settings.instance_dir, (self.path.parent / "state").resolve())

    def test_daily_only_and_m5_are_valid_choices(self):
        for fact in (None, "m5", "m15"):
            with self.subTest(fact=fact):
                settings = self.load({"markets": {"CN": {"minute_fact_freq": fact}}})
                self.assertEqual(settings.markets["CN"].minute_fact_freq, fact)

    def test_demo_sample_config_ignores_installed_source_minute_capability(self):
        # demo 不调用在线来源：装上只支持 m15 的来源后，随包 m5 样本配置仍可启动；真实模式照常按来源能力拒绝
        from dataclasses import replace
        sample = Path(instance.__file__).resolve().parents[2] / "samples" / "demo" / "instance.json"
        source = instance.InstanceConfig().markets["CN"].source
        registry = dict(instance.REGISTRY)
        registry[source] = replace(registry[source], minute_freqs=("m15",))
        with mock.patch.object(instance, "REGISTRY", registry):
            settings = instance.load_instance(sample, environ={})
            self.assertEqual((settings.mode, settings.markets["CN"].minute_fact_freq), ("demo", "m5"))
            with self.assertRaisesRegex(ValueError, "minute_fact_freq 不支持 'm5'"):
                self.load({"mode": "real", "markets": {"CN": {"minute_fact_freq": "m5"}}},
                          env={key: "x" for spec in registry.values() for key in spec.credentials})
            with self.assertRaisesRegex(ValueError, "minute_fact_freq 不支持 'm60'"):
                self.load({"markets": {"HK": {"minute_fact_freq": "m60"}}})

    def test_file_errors_are_explicit_and_json_payload_is_not_echoed(self):
        with self.assertRaisesRegex(ValueError, "配置文件"):
            instance.load_instance(self.path, environ={})
        self.path.write_text('{"mode": "private-value" broken')
        with self.assertRaises(ValueError) as caught:
            instance.load_instance(self.path, environ={})
        self.assertNotIn("private-value", str(caught.exception))

    def test_demo_lifecycle_disables_collection_even_with_enable_env(self):
        with mock.patch.dict(os.environ, {"COLLECTOR_ENABLED": "1"}):
            with instance.activate(instance.load_instance(environ={}), self.tmp.name):
                self.assertFalse(collector.is_enabled())
                worker = collector.Collector(self.tmp.name)
                from datetime import datetime
                self.assertEqual(worker.status([], now=datetime.now())["mode"], "demo")
                worker.conn().close()
        self.assertIsNone(instance.current())

    def test_author_lifecycle_applies_budget_without_changing_bindings(self):
        before = bindings.BINDINGS
        settings = self.load({"quota": {"per_day": 10000}})
        with instance.activate(settings, self.tmp.name):
            self.assertEqual(bindings.BINDINGS, before)
            self.assertEqual(config.QUOTA["mairui"], {"per_minute": 300, "per_day": 10000, "reserve": 0.1})
            self.assertEqual(set(config.QUOTA), {"mairui"}, "作者实例不应给原先未限额的来源新增预算")
        self.assertEqual(bindings.BINDINGS, before)

    def test_startup_sync_failure_degrades_like_collector_start(self):
        # 写者锁被占或库不可写不挡启动（配置错误仍拒绝）：实例选择照常生效，代次推进留待下次启动
        holder = collector.Collector(self.tmp.name)
        self.addCleanup(lambda: holder.conn().close())
        with holder.writer():
            with instance.activate(self.load({"markets": {"CN": {"minute_fact_freq": "m5"}}}), self.tmp.name):
                self.assertEqual(bindings.binding("CN", "stock", bindings.F.MINUTE_HISTORY).minute_fact_freq, "m5")
        with mock.patch.object(collector.Collector, "writer", side_effect=sqlite3.OperationalError("readonly")):
            with instance.activate(self.load({}), self.tmp.name) as settings:
                self.assertEqual(settings.mode, "demo")

    def test_application_rejects_missing_credentials_before_collector_start(self):
        from fastapi.testclient import TestClient
        from chanapp.api.main import app
        self.path.write_text('{"mode": "real"}')
        with mock.patch.dict(os.environ, {"CHANAPP_INSTANCE_CONFIG": str(self.path)}, clear=True):
            with self.assertRaisesRegex(ValueError, "MAIRUI_LICENCE"):
                with TestClient(app):
                    pass


if __name__ == "__main__":
    unittest.main()
