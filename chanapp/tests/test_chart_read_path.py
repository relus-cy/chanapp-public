"""/api/chart 读路径：纯读的图表请求不排队等采集器写者锁；条件请求在真实事实库上的 304 与数据变化。

真实事实库放临时目录，采集器关闭（不访问上游）；视图时钟钉住，两次请求的 stale 与状态栏不随跑测试的时刻变。"""
import functools
import threading
from datetime import date, datetime, time
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.engine import data as engine_data
from chanapp.engine.kline import calendar, views
from chanapp.engine.kline.rows import CalendarRow
from chanapp.tests.test_kline_facade import CODE, FacadeBase

URL = f"/api/chart?code={CODE}&freq=day"


def _without_age(body: dict) -> dict:
    meta = {k: v for k, v in body["meta"].items() if k != "stale_age_s"}
    return {**body, "meta": meta}


class ChartReadPathTests(FacadeBase):
    def setUp(self):
        super().setUp()
        with self.writer() as conn:
            calendar.store_rows(conn, [CalendarRow("CN", self.today, True)], source="t")
        now = datetime.combine(date.fromisoformat(self.today), time.fromisoformat("08:00"))
        for name in ("read_view", "read_bundle"):
            patch = mock.patch.object(views, name, functools.partial(getattr(views, name), now=now))
            patch.start()
            self.addCleanup(patch.stop)
        self.commit_days(self.days[:-1])                   # 最后一个交易日的日线留给「新增一根 bar」
        for d in self.days[-6:-1]:
            self.commit_minutes(d)
        from chanapp.api.main import app
        self.client = TestClient(app)

    def test_chart_read_does_not_wait_for_collector_writer_lock(self):
        first = self.client.get(URL)                       # 结论已记录：同一数据再看只是读
        self.assertEqual(first.status_code, 200, first.text[:300])
        held, release, result = threading.Event(), threading.Event(), {}

        def hold_writer():                                 # 持写者锁并开着写事务，如同采集器正在提交
            with engine_data._collector().writer() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    held.set()
                    release.wait(10)
                finally:
                    conn.execute("ROLLBACK")

        def request():
            result["response"] = self.client.get(URL)

        holder = threading.Thread(target=hold_writer, daemon=True)
        reader = threading.Thread(target=request, daemon=True)
        holder.start()
        try:
            self.assertTrue(held.wait(5), "写者锁未能取得")
            reader.start()
            reader.join(1.0)
            self.assertFalse(reader.is_alive(), "采集器持写者锁时，纯读的图表请求 1 秒内未返回")
        finally:
            release.set()
            holder.join(5)
            if reader.is_alive() or reader.ident is not None:
                reader.join(5)
        second = result["response"]
        self.assertEqual(second.status_code, 200, second.text[:300])
        self.assertEqual(second.headers["etag"], first.headers["etag"])
        self.assertEqual(_without_age(second.json()), _without_age(first.json()))

    def test_unchanged_data_is_304_and_a_new_bar_returns_200_with_new_etag(self):
        first = self.client.get(URL)
        self.assertEqual(first.status_code, 200, first.text[:300])
        etag = first.headers["etag"]
        same = self.client.get(URL, headers={"If-None-Match": etag})
        self.assertEqual((same.status_code, same.content, same.headers["etag"]), (304, b"", etag))

        new_day = self.days[-1]
        self.commit_days([new_day], close=12.0)
        changed = self.client.get(URL, headers={"If-None-Match": etag})
        self.assertEqual(changed.status_code, 200, changed.text[:300])
        self.assertNotEqual(changed.headers["etag"], etag)
        self.assertEqual(changed.json()["kline"][-1]["time"], new_day)
