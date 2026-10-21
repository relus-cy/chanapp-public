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

  // 规则状态为持有时，止损线从入场根画到最后一根（止损入场后固定）；空仓不画
  function stopData(rows, state, toTime) {
    if (!state || !state.holding || state.stop == null) return [];
    var out = [], on = false;
    (rows || []).forEach(function (r) {
      if (r.time === state.entry_time) on = true;
      if (on) out.push({ time: toTime(r.time), value: state.stop });
    });
    return out;
  }

  // 标记样式：假突破空心；形成中（盘中、未定稿）淡色并加 ~；其余实心
  function markerStyle(sig) {
    return {
      up: sig.side === 'buy',
      fill: sig.kind === 'false_break' ? 'hollow' : sig.status === 'provisional' ? 'faint' : 'solid',
      tilde: sig.status === 'provisional',
    };
  }

  var KIND_TEXT = {
    buy: { close: '收盘突破入场线', touch: '盘中触及入场线', false_break: '假突破：盘中越过入场线，收盘未站上' },
    sell: { close: '收盘跌破离场价', touch: '盘中触及离场价', false_break: '假跌破：盘中跌破离场价，收盘收回' },
  };

  function signalText(sig) {
    var t = (KIND_TEXT[sig.side] || {})[sig.kind] || '';
    return sig.status === 'provisional' ? t + '（形成中）' : t;
  }

  function fmt(v) { return v == null ? '—' : (+v).toFixed(2); }

  // 图例：第 i 根的入场线、离场线、N；规则状态持有且在入场之后显示止损；最后一根追加建议仓位与明日预估线；
  // 该根有信号时追加信号说明。返回 [{label, value}]，由 app.js 拼 HTML
  function legendParts(dc, i, mode) {
    if (!dc || !dc.rows || i == null || !dc.rows[i]) return [];
    var r = dc.rows[i], last = i === dc.rows.length - 1;
    var m = (dc.modes || {})[mode] || {};
    var st = m.state || {};
    var parts = [{ label: '入', value: fmt(r.entry) }, { label: '出', value: fmt(r.exit) }, { label: 'N', value: fmt(r.n) }];
    if (st.holding && st.entry_time && r.time >= st.entry_time) parts.push({ label: '止', value: fmt(st.stop) });
    if (last && dc.next && (dc.next.entry != null || dc.next.exit != null)) {
      parts.push({ label: '下一根', value: '入 ' + fmt(dc.next.entry) + ' 出 ' + fmt(dc.next.exit) });
    }
    if (last && dc.suggestion) {
      parts.push({ label: '建议仓位', value: (dc.suggestion.weight * 100).toFixed(1) + '%' });
    }
    (m.signals || []).forEach(function (s) {
      if (s.time === r.time) parts.push({ label: '', value: signalText(s) });
    });
    return parts;
  }

  var api = { KEYS: KEYS, MODES: MODES, STORE_KEY: STORE_KEY, STORE_MODE: STORE_MODE, readSettings: readSettings,
    lineData: lineData, stopData: stopData, markerStyle: markerStyle, signalText: signalText, legendParts: legendParts };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.chanDonchian = api;
})(this);
