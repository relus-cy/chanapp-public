"""/llms.txt：给 AI 与脚本的数据读取说明，随应用同源下发（源文件 web/llms.txt），且不与接口脱节。

可能出错的方式：
- 文件没被静态目录服务，读取方拿到 404；
- 说明里写了已改名或删除的接口；新增的 /api 接口没写进说明；
- /api/chart 新增顶层字段，或 meta、structure、信号、唐奇安分组的字段变了，说明没跟上。
图表字段取自真实门面与事实库（采集器关闭），不用手写的字段表。
"""
import math
import re
from datetime import date, timedelta

from fastapi.testclient import TestClient

from chanapp.api.main import app
from chanapp.engine.kline import calendar, facts
from chanapp.engine.kline.rows import CalendarRow, RawDayRow, new_batch_id
from chanapp.tests.test_kline_facade import KW, FacadeBase, weekdays

INDEX = "sh000001"                                    # 指数恒为不复权，夹具不必搭前复权因子链

DOC_ENDPOINT = re.compile(r"`(GET|POST|PUT|DELETE) (/[^\s`?]*)")


def api_routes():
    """应用实际注册的 /api 接口（取 OpenAPI 路由表，含 include 进来的子路由）。"""
    return {(method.upper(), path) for path, ops in app.openapi()["paths"].items() if path.startswith("/api/")
            for method in ops if method.upper() in ("GET", "POST", "PUT", "DELETE")}


class LlmsTxtTests(FacadeBase):
    def setUp(self):
        super().setUp()
        self.pin_view_now("16:00")
        # 200 根振荡行情：足以产生笔、中枢与买卖点，信号字段的检查才不会空过
        days = weekdays(200, date.today() - timedelta(days=7))
        closes = [round(20 + 3 * math.sin(i / 2.2) + 1.5 * math.sin(i / 7) + i * 0.01, 2) for i in range(len(days))]
        with self.writer() as conn:
            calendar.store_rows(conn, [CalendarRow("CN", d, True) for d in days], source="t")
            opens = [closes[i - 1] if i else c for i, c in enumerate(closes)]
            facts.commit_day_rows(conn, [
                RawDayRow(INDEX, d, o, max(o, c) + 0.3, min(o, c) - 0.3, c, 1000.0, "lot", 1.0, "CNY", o, 0, "final",
                          new_batch_id())
                for d, o, c in zip(days, opens, closes)], item="day_history", today=self.today, **{**KW, "kind": "index"})
        self.client = TestClient(app)
        response = self.client.get("/llms.txt")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.doc = response.text

    def documented(self, name):
        """字段名出现在某个代码片段里（按完整单词，`time` 不算进 `entry_time`）。"""
        return any(re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", span) for span in re.findall(r"`([^`]+)`", self.doc))

    def test_documented_endpoints_exist_and_every_api_route_is_documented(self):
        documented = set(DOC_ENDPOINT.findall(self.doc))
        self.assertEqual(documented - api_routes(), set())        # 没有写错或过时的接口
        self.assertEqual(api_routes() - documented, set())        # 每个 /api 接口都有说明

    def test_chart_fields_are_documented(self):
        body = self.client.get("/api/chart", params={"code": INDEX, "freq": "day"}).json()
        zones = body["structure"]["zs"] + body["structure"]["zs_xd"]            # 笔中枢与线段中枢字段相同
        self.assertTrue(body["signals"] and zones and body["channels"])       # 夹具确有信号、中枢与通道
        donchian = body["donchian"]["20/10"]
        groups = {
            "chart": body, "meta": body["meta"], "structure": body["structure"],
            "signal": body["signals"][0], "zs": zones[0], "bi": body["structure"]["bi"][0],
            "channel": body["channels"][0], "resonance": body["resonance"][0],
            "donchian": donchian,
            "donchian.state": donchian["modes"]["close"]["state"], "donchian.rows": donchian["rows"][-1],
        }
        missing = {group: sorted(k for k in fields if not self.documented(k)) for group, fields in groups.items()}
        self.assertEqual({g: m for g, m in missing.items() if m}, {})
