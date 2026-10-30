/* 主图均线设置的前端纯函数：读取与清洗本地保存、开关、改周期、增删与简单移动平均。
   每条线是 {n 周期, on 开关, slot 色位}；颜色只由色位决定（index.html 的 --ma-1…--ma-7），
   改周期、开关、排序都不换色，新线取最小空色位。设置是全局的，各 K 线周期共用。
   与 app.js 同为 classic script（页面在 app.js 前加载，挂 chanMa 全局；node 测试直接 require）。 */
(function (root) {
  'use strict';

  var STORE_KEY = 'chanapp-ma';
  var MAX = 7, MIN_N = 2, MAX_N = 999;
  var DEFAULTS = [5, 13, 20, 55, 144, 250];
  var DEFAULT_ON = [5, 13, 20];
  var ADD_CANDIDATES = [10, 30, 60, 89, 120, 34, 233];   // 添加时依次取第一个未用的周期

  function defaults() {
    return DEFAULTS.map(function (n, i) { return { n: n, on: DEFAULT_ON.indexOf(n) >= 0, slot: i }; });
  }

  function byPeriod(a, b) { return a.n - b.n; }

  function validN(n) { return typeof n === 'number' && n % 1 === 0 && n >= MIN_N && n <= MAX_N; }

  function freeSlot(lines) {
    for (var s = 0; s < MAX; s++) {
      if (!lines.some(function (l) { return l.slot === s; })) return s;
    }
    return -1;
  }

  // 逐条清洗：周期越界、非整数或重复的丢弃，至多保留 7 条；有效且未占用的色位保留，其余按出现顺序分配最小空位
  function sanitize(list) {
    var kept = [];
    list.forEach(function (raw) {
      if (kept.length >= MAX || !raw || typeof raw !== 'object' || !validN(raw.n)) return;
      if (kept.some(function (l) { return l.n === raw.n; })) return;
      kept.push({ n: raw.n, on: raw.on === true, slot: raw.slot });
    });
    var taken = {};
    kept.forEach(function (l) {
      var ok = typeof l.slot === 'number' && l.slot % 1 === 0 && l.slot >= 0 && l.slot < MAX && !taken[l.slot];
      if (ok) taken[l.slot] = true; else l.slot = -1;
    });
    kept.forEach(function (l) {
      if (l.slot >= 0) return;
      l.slot = freeSlot(kept);
    });
    return kept.sort(byPeriod);
  }

  // 没有保存、读不出或不是数组时用默认设置；保存的是空数组表示用户删光了，照样空着；有条目但全部无效时用默认
  function readConfig(get) {
    var raw = null;
    try { raw = get(STORE_KEY); } catch (_) { return defaults(); }
    if (raw == null) return defaults();
    var list;
    try { list = JSON.parse(raw); } catch (_) { return defaults(); }
    if (!Array.isArray(list)) return defaults();
    if (!list.length) return [];
    var cfg = sanitize(list);
    return cfg.length ? cfg : defaults();
  }

  function writeConfig(set, cfg) {
    try { set(STORE_KEY, JSON.stringify(cfg)); } catch (_) {}
  }

  function copy(cfg) { return cfg.map(function (l) { return { n: l.n, on: l.on, slot: l.slot }; }); }

  function toggle(cfg, n) {
    return copy(cfg).map(function (l) { if (l.n === n) l.on = !l.on; return l; });
  }

  // 改周期：返回 {config, error}；拒绝时 error 是给用户看的原因，config 原样返回
  function setPeriod(cfg, n, value) {
    var text = String(value == null ? '' : value).trim();
    var next = /^\d+$/.test(text) ? +text : NaN;
    if (isNaN(next)) return { config: cfg, error: '周期须为整数' };
    if (next < MIN_N || next > MAX_N) return { config: cfg, error: '周期须在 ' + MIN_N + '–' + MAX_N + ' 之间' };
    if (next !== n && cfg.some(function (l) { return l.n === next; })) return { config: cfg, error: '已有 MA' + next };
    return { config: copy(cfg).map(function (l) { if (l.n === n) l.n = next; return l; }).sort(byPeriod), error: null };
  }

  function canAdd(cfg) { return cfg.length < MAX; }

  function add(cfg) {
    if (!canAdd(cfg)) return cfg;
    var used = function (n) { return cfg.some(function (l) { return l.n === n; }); };
    var n = ADD_CANDIDATES.filter(function (c) { return !used(c); })[0];
    for (var c = MIN_N; n == null; c++) if (!used(c)) n = c;
    var next = copy(cfg);
    next.push({ n: n, on: true, slot: freeSlot(cfg) });
    return next.sort(byPeriod);
  }

  function remove(cfg, n) {
    return copy(cfg).filter(function (l) { return l.n !== n; });
  }

  // 简单移动平均：前 n-1 根为 null
  function sma(closes, n) {
    var out = [], s = 0;
    for (var i = 0; i < closes.length; i++) {
      s += closes[i];
      if (i >= n) s -= closes[i - n];
      out.push(i >= n - 1 ? s / n : null);
    }
    return out;
  }

  var api = { STORE_KEY: STORE_KEY, MAX: MAX, MIN_N: MIN_N, MAX_N: MAX_N, defaults: defaults, readConfig: readConfig,
    writeConfig: writeConfig, toggle: toggle, setPeriod: setPeriod, canAdd: canAdd, add: add, remove: remove, sma: sma };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.chanMa = api;
})(this);
