"""K 线取数核心：原始事实入库，读时出视图。上层只经 engine/data.py 门面访问。

模块分工：
- rows、sessions：原始行契约与词汇；市场会话与周期槽位（区间末端标签）。
- admission：准入纯函数（硬拒绝与软标记）。
- facts：事实库 schema、单写者事务、修订追加、隔离与待核验、缺口、计算审计与快照。
- bindings：(市场, 标的类型, 取数项) → 主源与冷备的绑定、绑定代次、探针结论。
- collector、keepalive：事实层唯一写者（首取、盘前、盘中、定稿、回填、缺口补取）；冷备保活比对。
- calendar：F5 交易日历（过去由指数日线推出，未来由供应商年表给出）。
- qfq、periods、views：等比前复权因子链；周期与周线聚合；一致视图（令牌、新鲜度、提示、来源、行情）。
- hk_vendor_qfq：港股过渡期供应商前复权缓存。
- instance：JSON 实例覆盖与启动校验；providers/registry：静态来源工厂、能力与凭据名。
- ingest：一次性导入（CSV → 规范行 → 准入预检 → Collector.commit_import 单写者提交）。
- config：取数与视图内部参数；实例额度在启动时覆盖；http：显示层共用的 HTTP 小工具。
- providers/：raw provider，每个源一个文件，
  一律不复权、不跨源回落；哪个源是主源、哪个是冷备以 bindings.BINDINGS 为准。

本包初始化不导入任何子模块。
"""
