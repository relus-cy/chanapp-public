/* 副图指标的纯函数实现：与 app.js 同为 classic script（页面在 app.js 前加载，挂 chanIndicators 全局；
   node 测试直接 require）。输入为显式参数，不读页面状态。 */
(function (root) {
  'use strict';

  function rsi(closes, n) {
    var out = closes.map(function () { return null; }), ag = 0, al = 0;
    for (var i = 1; i < closes.length; i++) {
      var ch = closes[i] - closes[i - 1], g = Math.max(ch, 0), l = Math.max(-ch, 0);
      if (i <= n) { ag += g / n; al += l / n; }
      else { ag = (ag * (n - 1) + g) / n; al = (al * (n - 1) + l) / n; out[i] = al ? 100 - 100 / (1 + ag / al) : 100; }
    }
    return out;
  }

  function kdj(bars) {
    var K = [], D = [], J = [], k = 50, d = 50;
    for (var i = 0; i < bars.length; i++) {
      var s = Math.max(0, i - 8), hh = -1e18, ll = 1e18;
      for (var j = s; j <= i; j++) { hh = Math.max(hh, bars[j].high); ll = Math.min(ll, bars[j].low); }
      var rsv = hh === ll ? 50 : (bars[i].close - ll) / (hh - ll) * 100;
      k = 2 / 3 * k + 1 / 3 * rsv; d = 2 / 3 * d + 1 / 3 * k;
      K.push(k); D.push(d); J.push(3 * k - 2 * d);
    }
    return { k: K, d: D, j: J };
  }

  function boll(closes, n, k) {
    var mid = [], up = [], lo = [];
    for (var i = 0; i < closes.length; i++) {
      if (i < n - 1) { mid.push(null); up.push(null); lo.push(null); continue; }
      var s = 0;
      for (var j = i - n + 1; j <= i; j++) s += closes[j];
      var m = s / n, v = 0;
      for (j = i - n + 1; j <= i; j++) v += (closes[j] - m) * (closes[j] - m);
      var sd = Math.sqrt(v / n);
      mid.push(m); up.push(m + k * sd); lo.push(m - k * sd);
    }
    return { mid: mid, up: up, lo: lo };
  }

  var api = { rsi: rsi, kdj: kdj, boll: boll };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.chanIndicators = api;
})(this);
