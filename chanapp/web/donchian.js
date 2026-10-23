/* 唐奇安通道的前端纯函数：设置读取、绘制数据、标记样式与图例文字。数值全部来自 /api/chart 的 donchian 字段
   （后端 engine/donchian.py 计算，AI 读接口拿到的是同一份），这里不重算。
   与 app.js 同为 classic script（页面在 app.js 前加载，挂 chanDonchian 全局；node 测试直接 require）。 */
(function (root) {
  'use strict';

  var KEYS = ['off', '20/10', '55/20'];
  var MODES = ['close', 'touch'];
  var STORE_KEY = 'chanapp-donchian', STORE_MODE = 'chanapp-donchian-mode';

  // 设置：参数组（默认关闭）与判定方式（默认收盘确认）；读到无法识别的值按默认处理
  function readSettings(get) {
    var key = null, mode = null;
    try { key = get(STORE_KEY); mode = get(STORE_MODE); } catch (_) {}
    return { key: KEYS.indexOf(key) >= 0 ? key : 'off', mode: MODES.indexOf(mode) >= 0 ? mode : 'close' };
  }

  // rows[field] → [{time, value}]，null（不足 n 根）不出点
  function lineData(rows, field, toTime) {
    var out = [];
    (rows || []).forEach(function (r) {
      if (r[field] != null) out.push({ time: toTime(r.time), value: r[field] });
    });
    return out;
  }

  // 第 i 根所在的那笔规则进出（入场根到离场根，含两端；持有中的到末根），不在任何一笔里为 null。
  // time 为同格式字符串，字典序即时间序
  function tradeAt(trades, time) {
    var hit = null;
    (trades || []).forEach(function (t) {
      if (t.entry_time <= time && (t.exit_time == null || time <= t.exit_time)) hit = t;
    });
    return hit;
  }

  // 止损线逐笔画（每笔入场后固定），从入场根画到离场根，持有中的画到末根。图表库（lightweight-charts 5.0.9 实测）
  // 每段线取起点的颜色，只有 time 的空白点也不会断线，所以离场根与笔外的点都给透明色来断开，前后两笔紧挨着时也分得开。
  // 笔外的点取下一笔（之后没有就取上一笔）的止损作值，不把价格轴拉偏；一笔都没有时不出点
  var GAP = 'transparent';
  function stopData(rows, trades, toTime) {
    if (!trades || !trades.length) return [];
    var pts = (rows || []).map(function (r) {
      var t = tradeAt(trades, r.time);
      if (!t) return { time: toTime(r.time), value: null, color: GAP };
      var p = { time: toTime(r.time), value: t.stop };
      if (r.time === t.exit_time) p.color = GAP;
      return p;
    });
    var fill = null, i;
    for (i = pts.length - 1; i >= 0; i--) { if (pts[i].value == null) pts[i].value = fill; else fill = pts[i].value; }
    for (i = 0; i < pts.length; i++) { if (pts[i].value == null) pts[i].value = fill; else fill = pts[i].value; }
    return pts;
  }

  // 标记样式：假突破空心；盘中触价与形成中（盘中、未定稿）的信号淡色并加 ~；收盘确认的实心
  function markerStyle(sig) {
    var soft = sig.kind === 'touch' || sig.status === 'provisional';
    return {
      up: sig.side === 'buy',
      fill: sig.kind === 'false_break' ? 'hollow' : soft ? 'faint' : 'solid',
      tilde: soft,
    };
  }

  // 两种判定方式的信号都画（判定方式靠标记样式区分；规则状态与止损线才跟随所选方式）。
  // 同根同方向同类同线位的只留一个；淡色先画、空心其次、实心最后，实心压在上面
  var FILL_ORDER = { faint: 0, hollow: 1, solid: 2 };
  function allSignals(dc) {
    var seen = {}, out = [];
    ['close', 'touch'].forEach(function (m) {
      (((dc && dc.modes) || {})[m] || {}).signals && dc.modes[m].signals.forEach(function (s) {
        var k = s.time + '|' + s.side + '|' + s.kind + '|' + s.level;
        if (!seen[k]) { seen[k] = true; out.push(s); }
      });
    });
    return out.map(function (s, i) { return { s: s, i: i, r: FILL_ORDER[markerStyle(s).fill] }; })
      .sort(function (a, b) {   // time 为同格式字符串，字典序即时间序
        return a.r - b.r || (a.s.time < b.s.time ? -1 : a.s.time > b.s.time ? 1 : 0) || a.i - b.i;
      })
      .map(function (x) { return x.s; });
  }

  // {line} 是被越过的线：买入为入场线；卖出按 basis 为止损或离场线，没有 basis（旧数据）时统称离场价
  var KIND_TEXT = {
    buy: { close: '收盘突破{line}', touch: '盘中触及{line}', false_break: '假突破：盘中越过{line}，收盘未站上' },
    sell: { close: '收盘跌破{line}', touch: '盘中触及{line}', false_break: '假跌破：盘中跌破{line}，收盘收回' },
  };
  var LINE_TEXT = { buy: '入场线', stop: '止损', exit: '离场线', sell: '离场价' };

  // withLevel：线名后带上被越过的线位（图例用），「形成中」放在最后
  function signalText(sig, withLevel) {
    var line = LINE_TEXT[sig.side === 'buy' ? 'buy' : sig.basis] || LINE_TEXT.sell;
    if (withLevel && sig.level != null) line += ' ' + fmt(sig.level);
    var t = ((KIND_TEXT[sig.side] || {})[sig.kind] || '').replace('{line}', line);
    return sig.status === 'provisional' ? t + '（形成中）' : t;
  }

  function fmt(v) { return v == null ? '—' : (+v).toFixed(2); }

  // 图例：第 i 根的入场线、离场线、N；该根落在所选方式的某笔规则进出里（含已平仓的）时显示那笔的止损；
  // 该根有信号时追加信号说明与被越过的线位（两种判定方式的都列，所选方式在前，文字相同的只列一次）。
  // 返回 [{label, value}]，由 app.js 拼 HTML。
  // 规则状态、接下来判定的线位与建议仓位不在图例里，见 statusSummary（右栏状态卡）
  function legendParts(dc, i, mode) {
    if (!dc || !dc.rows || i == null || !dc.rows[i]) return [];
    var r = dc.rows[i];
    var trade = tradeAt(((dc.modes || {})[mode] || {}).trades, r.time);
    var parts = [{ label: '入', value: fmt(r.entry) }, { label: '出', value: fmt(r.exit) }, { label: 'N', value: fmt(r.n) }];
    if (trade) parts.push({ label: '止损', value: fmt(trade.stop) });
    var texts = {};
    [mode, mode === 'close' ? 'touch' : 'close'].forEach(function (k) {   // 所选方式的信号在前
      (((dc.modes || {})[k] || {}).signals || []).forEach(function (s) {
        var t = signalText(s, true);
        if (s.time === r.time && !texts[t]) { texts[t] = true; parts.push({ label: '', value: t }); }
      });
    });
    return parts;
  }

  function pctText(v) { return (v * 100).toFixed(1) + '%'; }

  // 右栏状态卡：所选判定方式的规则状态（按规则假设，不是实际持仓）。watch 是接下来要判定的那条线——
  // 持有时盯有效离场价，空仓时盯入场线（都由后端按最后一根是否形成中选好），dist 是它相对最新收盘的距离。
  // 数值格式化为字符串，时间保持原样由 app.js 格式化
  function statusSummary(dc, mode, close) {
    var m = dc && dc.modes && dc.modes[mode];
    if (!m || !m.state || !dc.rows || !dc.rows.length) return null;
    var st = m.state, holding = !!st.holding;
    var line = holding ? st.effective_exit : st.effective_entry;
    var sigs = m.signals || [], sig = sigs[sigs.length - 1];
    var sg = dc.suggestion;
    var dist = line == null || !close ? null : (line >= close ? '+' : '') + pctText(line / close - 1);
    return {
      holding: holding, provisional: !!st.provisional,
      watch: { side: holding ? 'exit' : 'entry', price: fmt(line), dist: dist },
      entry: holding ? { time: st.entry_time, price: fmt(st.entry_price) } : null,
      stop: holding ? fmt(st.stop) : null,
      n: fmt(dc.rows[dc.rows.length - 1].n),
      suggestion: sg ? { weight: pctText(sg.weight), risk: +(sg.risk_budget * 100).toFixed(2) + '%' } : null,
      last: sig ? { time: sig.time, text: signalText(sig), price: fmt(sig.price) } : null,
    };
  }

  var api = { KEYS: KEYS, MODES: MODES, STORE_KEY: STORE_KEY, STORE_MODE: STORE_MODE, readSettings: readSettings,
    lineData: lineData, stopData: stopData, markerStyle: markerStyle, allSignals: allSignals, signalText: signalText,
    legendParts: legendParts, statusSummary: statusSummary };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.chanDonchian = api;
})(this);
