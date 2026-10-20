/* 用户画线：存储格式与图表之间的换算（纯函数）和同步控制器。
   与 app.js 同为 classic script（页面在 app.js 前加载，挂 chanDrawings 全局；node 测试直接 require 纯函数）。

   存储格式（服务端只校验 v 与大小，语义归这里）：
     {v: 1, kind, adjust: 'qfq'|'raw', anchors: [{freq, dt, price}], style: {用户改过的样式键}, ref: {day, close}|null,
      locked?: true}
   - style 只存与默认样式不同的键，默认样式由 installDefaults 统一设定（改默认值会改变没改过样式的旧画线的外观）；
     locked 只在锁定时出现，锁定的画线不能拖动、不能用键盘删除。
   - 锚点记画线时所在周期与该 bar 的原始标签 dt（区间末端），不存图表时间值：日线、周线是日期字符串，分钟线是秒数，
     跨周期不能直接比较。显示时按「锚点 bar 的起始时刻 → 当前周期第一根末端晚于它的 bar」映射；
     晚于最后一根的锚点按交易时段数第 k 根未来槽位，早于已加载数据的按日历时间近似（左拉补历史后重新映射）。
   - 前复权下画的线：每次新除权，除权日之前的价格整体乘同一因子，同一条画线的锚点同比例变化。ref 记一个完整交易日的
     收盘价；显示时拿当前数据里同一天的收盘相比得到比例，缩放全部锚点并换新参照（由控制器写回）。拿不到那一天
     （不在已加载数据里、周线上不是周末交易日）时按上次保存的价格显示。只在画线时的复权模式下显示。 */
(function (root) {
  'use strict';

  var SESSIONS = { cn: [['09:30', '11:30'], ['13:00', '15:00']], hk: [['09:30', '12:00'], ['13:00', '16:00']] };
  var FREQ_MIN = { m5: 5, m15: 15, m30: 30, m60: 60 };
  var DAY = 1440;   // 键以分钟计：交易日序号 × 1440 + 当日分钟

  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function minutes(hhmm) { var p = hhmm.split(':'); return +p[0] * 60 + +p[1]; }
  function hhmm(total) { return pad2(Math.floor(total / 60)) + ':' + pad2(total % 60); }
  function dayIndex(date) { var p = date.split('-'); return Date.UTC(+p[0], +p[1] - 1, +p[2]) / 86400000; }
  function dateOf(index) {
    var t = new Date(index * 86400000);
    return t.getUTCFullYear() + '-' + pad2(t.getUTCMonth() + 1) + '-' + pad2(t.getUTCDate());
  }
  function mondayOf(index) { return index - (index + 3) % 7; }     // 1970-01-01 是周四
  function isWeekday(index) { return (index + 3) % 7 < 5; }
  function isMinute(freq) { return !!FREQ_MIN[freq]; }
  function marketKey(market) { return String(market || 'cn').toLowerCase() === 'hk' ? 'hk' : 'cn'; }
  function datePart(label) { return label.split(' ')[0]; }

  // 会话内按步长给区间末端标签，会话末尾不足一个步长的短桶保留（与 engine/kline/sessions.py 同规则）
  var grids = {};
  function grid(market, freq) {
    var key = marketKey(market) + freq;
    if (grids[key]) return grids[key];
    var out = [], step = FREQ_MIN[freq];
    SESSIONS[marketKey(market)].forEach(function (s) {
      var begin = minutes(s[0]), finish = minutes(s[1]);
      for (var t = begin + step; t < finish; t += step) out.push(t);
      out.push(finish);
    });
    return (grids[key] = out);
  }

  // bar 的末端：周线取该周周一 + 7 天（形成中的周线标签是当天，不能拿标签日当周末，否则同周稍后几天被算成下一周）
  function endKey(freq, label) {
    var p = label.split(' '), index = dayIndex(p[0]);
    if (freq === 'week') return (mondayOf(index) + 7) * DAY;
    return p[1] ? index * DAY + minutes(p[1]) : (index + 1) * DAY;
  }

  // bar 的起始时刻：分钟线为同一会话里上一槽位（会话第一槽为会话开始），日线为当日 0 点，周线为该周周一 0 点
  function startKey(freq, label, market) {
    var p = label.split(' '), index = dayIndex(p[0]);
    if (freq === 'week') return mondayOf(index) * DAY;
    if (!p[1]) return index * DAY;
    var t = minutes(p[1]), sessions = SESSIONS[marketKey(market)], g = grid(market, freq);
    for (var i = 0; i < sessions.length; i++) {
      var begin = minutes(sessions[i][0]), finish = minutes(sessions[i][1]);
      if (t > begin && t <= finish) {
        var start = begin;
        g.forEach(function (m) { if (m > begin && m < t) start = m; });
        return index * DAY + start;
      }
    }
    return index * DAY + t - (FREQ_MIN[freq] || 0);
  }

  function nextSlot(label, freq, market) {
    var p = label.split(' '), index = dayIndex(p[0]);
    if (freq === 'week') return dateOf(mondayOf(index) + 11);              // 下周五
    if (!isMinute(freq)) {
      do { index++; } while (!isWeekday(index));
      return dateOf(index);
    }
    var g = grid(market, freq), t = p[1] ? minutes(p[1]) : -1;
    for (var i = 0; i < g.length; i++) if (g[i] > t) return p[0] + ' ' + hhmm(g[i]);
    do { index++; } while (!isWeekday(index));
    return dateOf(index) + ' ' + hhmm(g[0]);
  }

  // 未来槽位只按周一到周五排，不知道节假日；节假日后落到的 bar 会晚一个交易日
  function futureLabel(last, freq, market, k) {
    var label = last;
    for (var i = 0; i < k; i++) label = nextSlot(label, freq, market);
    return label;
  }

  function futureSlots(last, freq, market, key) {
    var label = last;
    for (var k = 1; k <= 5000; k++) {
      label = nextSlot(label, freq, market);
      if (endKey(freq, label) > key) return k;
    }
    return 5000;
  }

  // 当前周期里承载锚点的 bar：{index} 已加载，{future: k} 最后一根之后第 k 根，{past: true, key} 早于已加载数据
  function locate(anchor, labels, freq, market) {
    var n = labels.length;
    if (!n) return null;
    var key;
    if (anchor.freq === freq) {
      var target = endKey(freq, anchor.dt), lo = 0, hi = n - 1;
      while (lo <= hi) {
        var mid = (lo + hi) >> 1, k = endKey(freq, labels[mid]);
        if (k === target) return { index: mid };
        if (k < target) lo = mid + 1; else hi = mid - 1;
      }
      key = target - 1;
    } else {
      key = startKey(anchor.freq, anchor.dt, market);
    }
    if (key >= endKey(freq, labels[n - 1])) return { future: futureSlots(labels[n - 1], freq, market, key) };
    // 锚点整根早于第一根已加载 bar 才算 past；部分重叠时落在第一根上
    if (endKey(anchor.freq, anchor.dt) <= startKey(freq, labels[0], market)) return { past: true, key: key };
    var a = 0, b = n - 1;
    while (a < b) {
      var m = (a + b) >> 1;
      if (endKey(freq, labels[m]) > key) b = m; else a = m + 1;
    }
    return { index: a };
  }

  function timeAsSec(t) {
    if (typeof t === 'number') return t;
    if (typeof t === 'string') { var ms = Date.parse(t); return isFinite(ms) ? ms / 1000 : null; }
    return null;
  }

  // 与画线库外推未来时间同口径：最近 50 个间隔的中位数（周末、午休被中位数滤掉）
  function medianInterval(secs) {
    if (secs.length < 2) return 86400;
    var deltas = [];
    for (var i = Math.max(1, secs.length - 50); i < secs.length; i++) {
      if (secs[i] > secs[i - 1]) deltas.push(secs[i] - secs[i - 1]);
    }
    if (!deltas.length) return 86400;
    deltas.sort(function (x, y) { return x - y; });
    return deltas[Math.floor(deltas.length / 2)];
  }

  // 当前视图：bars 为 app 的 klineData（time 为原始标签），toTime 与主图 setData 同一换算
  function buildView(o) {
    var labels = o.bars.map(function (b) { return b.time; });
    var times = labels.map(o.toTime);
    var secs = times.map(timeAsSec);
    var byTime = {};
    times.forEach(function (t, i) { byTime[String(t)] = i; });
    return { code: o.code, freq: o.freq, market: marketKey(o.market), adjust: o.adjust, bars: o.bars,
      labels: labels, times: times, secs: secs, byTime: byTime, iv: medianInterval(secs), toTime: o.toTime };
  }

  function labelFromKey(key, freq) {
    var date = dateOf(Math.floor(key / DAY));
    return isMinute(freq) ? date + ' ' + hhmm(((key % DAY) + DAY) % DAY) : date;
  }

  function labelFromSec(sec, freq) {
    var t = new Date(sec * 1000);
    var date = t.getUTCFullYear() + '-' + pad2(t.getUTCMonth() + 1) + '-' + pad2(t.getUTCDate());
    return isMinute(freq) ? date + ' ' + pad2(t.getUTCHours()) + ':' + pad2(t.getUTCMinutes()) : date;
  }

  // 锚点 → 交给画线库的图表时间；已加载的给 bar 的时间值，未来给「最后一根 + k × 中位间隔」（库按同一间隔外推）
  function chartTime(anchor, view) {
    var loc = locate(anchor, view.labels, view.freq, view.market);
    if (!loc) return null;
    if (loc.index != null) return view.times[loc.index];
    var n = view.secs.length;
    if (loc.future != null) return view.secs[n - 1] + loc.future * view.iv;
    return view.toTime(labelFromKey(loc.key, view.freq));
  }

  // 图表时间 → 当前周期的锚点标签（chartTime 的逆）
  function anchorDt(time, view) {
    var i = view.byTime[String(time)];
    if (i != null) return view.labels[i];
    var sec = timeAsSec(time), n = view.secs.length;
    if (sec == null || !n) return null;
    if (sec > view.secs[n - 1]) {
      var k = Math.max(1, Math.round((sec - view.secs[n - 1]) / view.iv));
      return futureLabel(view.labels[n - 1], view.freq, view.market, k);
    }
    if (sec < view.secs[0]) return labelFromSec(sec, view.freq);
    var lo = 0, hi = n - 1;                               // 区间内非 bar 时间：落在它之前最近的一根
    while (lo < hi) {
      var mid = (lo + hi + 1) >> 1;
      if (view.secs[mid] <= sec) lo = mid; else hi = mid - 1;
    }
    return view.labels[lo];
  }

  // 某个完整交易日的收盘：日线取当日；分钟线取当日最后一根，且之后要有更晚日期的 bar（当天已收完）；周线只认周末交易日
  function dayClose(bars, freq, day) {
    if (!isMinute(freq)) {
      for (var i = bars.length - 1; i >= 0; i--) if (bars[i].time === day) return bars[i].close;
      return null;
    }
    for (var j = bars.length - 1; j > 0; j--) {
      if (datePart(bars[j - 1].time) === day && datePart(bars[j].time) > day) return bars[j - 1].close;
    }
    return null;
  }

  // 前复权参照：最后一根之前最近的完整交易日
  function refFor(bars, freq) {
    var n = bars.length;
    if (n < 2) return null;
    if (!isMinute(freq)) return { day: bars[n - 2].time, close: bars[n - 2].close };
    var lastDay = datePart(bars[n - 1].time);
    for (var i = n - 2; i >= 0; i--) {
      if (datePart(bars[i].time) < lastDay) return { day: datePart(bars[i].time), close: bars[i].close };
    }
    return null;
  }

  // 读服务端记录：按 v 迁移到当前格式（目前只有 v1）；缺字段、未知版本返回 null，由调用方跳过并告警
  var FREQS = { week: 1, day: 1, m60: 1, m30: 1, m15: 1, m5: 1 };
  function readBody(body) {
    if (!body || typeof body !== 'object' || body.v !== 1) return null;
    if (typeof body.kind !== 'string' || (body.adjust !== 'qfq' && body.adjust !== 'raw')) return null;
    if (!Array.isArray(body.anchors) || !body.anchors.length) return null;
    var ok = body.anchors.every(function (a) {
      return a && FREQS[a.freq] && typeof a.dt === 'string' && /^\d{4}-\d\d-\d\d( \d\d:\d\d)?$/.test(a.dt) &&
        typeof a.price === 'number' && isFinite(a.price);
    });
    if (!ok) return null;
    var ref = body.ref && typeof body.ref.day === 'string' && body.ref.close > 0 ? body.ref : null;
    var style = body.style && typeof body.style === 'object' && !Array.isArray(body.style) ? body.style : {};
    var out = { v: 1, kind: body.kind, adjust: body.adjust, anchors: body.anchors, style: style, ref: ref };
    if (body.locked === true) out.locked = true;
    return out;
  }

  // ---------- 默认样式（看齐长桥：细线、单一蓝色、斐波那契单色不填充；A 股红涨绿跌） ----------
  var DEFAULT_COLOR = '#2a98fe';
  var LIB_BLUES = { '#2962ff': 1, '#1e53e5': 1, '#3d85c6': 1 };
  var SOLO_SHAPES = { 'rectangle': 1, 'triangle': 1, 'rotated-rectangle': 1, 'circle': 1, 'ellipse': 1,
    'disjoint-channel': 1, 'flat-top-bottom': 1, 'abcd-pattern': 1, 'xabcd-pattern': 1, 'head-and-shoulders': 1,
    'triangle-pattern': 1, 'cyclic-lines': 1 };
  var FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 1];
  function defaultsFor(Lib, kind) {
    var base = Lib.factoryStyleFor(kind), o = { width: 1 };
    ['color', 'textColor', 'backgroundColor', 'customTextColor'].forEach(function (k) {
      if (typeof base[k] === 'string' && (LIB_BLUES[base[k].toLowerCase()] || SOLO_SHAPES[kind])) o[k] = DEFAULT_COLOR;
    });
    if ('transparency' in base && base.fillBackground) o.transparency = Math.max(base.transparency, 85);
    if (kind === 'arrow-mark-up') o.color = '#f23645';
    if (kind === 'arrow-mark-down') o.color = '#089981';
    if (kind === 'fib-retracement' || kind === 'trend-based-fib-extension') {
      o.levels = base.levels.map(function (l) {
        return Object.assign({}, l, { color: DEFAULT_COLOR, visible: kind === 'fib-retracement' ? FIB_LEVELS.indexOf(l.coeff) >= 0 : l.visible });
      });
      o.fillBackground = false;
      o.fibLevelsAsPercents = true;
    }
    if (kind === 'parallel-channel') {
      o.levels = base.levels.map(function (l) { return Object.assign({}, l, { color: DEFAULT_COLOR, width: 1 }); });
      o.transparency = 90;
    }
    if (kind === 'fib-speed-resistance-fan' || kind === 'gann-fan') o.fillBackground = false;
    return o;
  }
  // 画线库的默认样式是模块级的，装一次即可；新放置的画线和 styleDiff 的比较基准都用它
  function installDefaults(Lib) {
    if (Lib.__chanDefaults) return;
    Lib.__chanDefaults = true;
    Lib.setDefaultStyleOverride(function (kind) { return defaultsFor(Lib, kind); });
  }

  // 改线条颜色：和旧线条颜色相同的文字、填充、各档位颜色一起换（单色画线保持单色，用户另设的颜色不动）
  function recolorPatch(style, color) {
    var old = String(style.color || '').toLowerCase(), patch = { color: color };
    ['textColor', 'backgroundColor', 'customTextColor'].forEach(function (k) {
      if (typeof style[k] === 'string' && style[k].toLowerCase() === old) patch[k] = color;
    });
    if (Array.isArray(style.levels) && style.levels.length &&
        style.levels.every(function (l) { return String(l.color || '').toLowerCase() === old; })) {
      patch.levels = style.levels.map(function (l) { return Object.assign({}, l, { color: color }); });
    }
    return patch;
  }

  // 返回换到当前口径的新 body；不需要改动（不复权、比例为 1、拿不到参照那天）时返回 null。
  // 比例为 1 时不前移参照：只看不改不写服务端；参照随用户下一次改动按当时数据刷新（bodyFrom）
  function rescale(body, bars, freq) {
    if (body.adjust !== 'qfq' || !body.ref) return null;
    var close = dayClose(bars, freq, body.ref.day);
    if (close == null || !(close > 0) || !(body.ref.close > 0)) return null;
    var ratio = close / body.ref.close;
    if (Math.abs(ratio - 1) < 1e-9) return null;
    var out = JSON.parse(JSON.stringify(body));
    out.anchors.forEach(function (a) { a.price = a.price * ratio; });
    out.ref = refFor(bars, freq) || { day: body.ref.day, close: close };
    return out;
  }

  // ---------- 同步控制器（画线库实例 + 服务端存储） ----------
  // o: {Lib, chart, series, fetch, view() → {code, freq, market, adjust, bars, toTime}|null, magnet,
  //     styleFor(kind) → 用户样式键, onChange(state), notify(msg)}
  function createController(o) {
    var Lib = o.Lib;
    installDefaults(Lib);
    var view = null;
    var dm = new Lib.DrawingManager(o.chart, o.series, {
      magnet: o.magnet || 'weak',
      timeInfo: function () { return { timeZone: 'UTC', intraday: !!(view && isMinute(view.freq)) }; },
    });
    var code = null, records = {}, order = [], loadSeq = 0, suppress = 0, hidden = 0, skipped = 0, lockAll = !!o.lockAll;
    var gesture = false, renderPending = false;

    function quiet(fn) { suppress++; try { fn(); } finally { suppress--; } }
    function state() {
      var unsaved = 0;
      order.forEach(function (id) { if (records[id] && records[id].state === 'error') unsaved++; });
      return { code: code, count: order.length, hidden: hidden, skipped: skipped, unsaved: unsaved, selection: dm.selection().slice(),
        tool: dm.tool(), visible: dm.drawingsVisible(), lockAll: lockAll };
    }
    function changed() { if (o.onChange) o.onChange(state()); }
    function notify(msg) { if (o.notify) o.notify(msg); }

    function defaults(kind) { return Lib.defaultStyleFor(kind); }
    function styleDiff(kind, style) {
      var base = defaults(kind), out = {};
      Object.keys(style || {}).forEach(function (k) {
        if (JSON.stringify(style[k]) !== JSON.stringify(base[k])) out[k] = style[k];
      });
      return out;
    }
    function fullStyle(kind, style) { return Object.assign({}, defaults(kind), style || {}); }

    // 画线库里的画线 → 存储格式；没被拖动的锚点保留原来的周期与标签，只换价格（价格都已是当前口径）
    function bodyFrom(d, rec) {
      var prev = rec && (rec.local || rec.body);
      var anchors = d.points.map(function (p, i) {
        var old = prev && prev.anchors[i];
        if (old && rec.mapped && String(rec.mapped[i]) === String(p.time)) {
          return { freq: old.freq, dt: old.dt, price: p.price };
        }
        return { freq: view.freq, dt: anchorDt(p.time, view), price: p.price };
      });
      if (anchors.some(function (a) { return !a.dt; })) return null;
      var out = { v: 1, kind: d.kind, adjust: view.adjust, anchors: anchors, style: styleDiff(d.kind, d.style),
        ref: view.adjust === 'qfq' ? refFor(view.bars, view.freq) : null };
      if (prev && prev.locked === true) out.locked = true;      // 「锁定全部」只是本页的显示状态，不写进画线
      return out;
    }
    function ownBody(rec) { return rec.local || readBody(rec.body); }

    function api(method, path, body, keepalive) {
      return o.fetch(path, Object.assign({ method: method, cache: 'no-store' }, keepalive ? { keepalive: true } : {},
        body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }))
        .then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (data) { return { status: r.status, data: data }; });
        });
    }

    // 记录都带所属标的 rec.code：换标的后在途的保存、删除仍按原标的收尾，不去新标的的 records 里找
    function record(id, owner, fields) {
      return Object.assign({ id: id, code: owner, body: null, revision: null, local: null, state: 'saved' }, fields);
    }
    function drop(rec) {
      clearTimeout(rec.timer);
      if (records[rec.id] === rec) {
        delete records[rec.id];
        order = order.filter(function (x) { return x !== rec.id; });
      }
      if (rec.code === code) quiet(function () { if (dm.get(rec.id)) dm.remove(rec.id); });
    }

    function schedule(rec) {
      clearTimeout(rec.timer);
      rec.timer = setTimeout(function () { save(rec); }, 1000);
    }

    function save(rec, keepalive) {
      if (!rec.local) return;
      clearTimeout(rec.timer); rec.timer = null;
      if (rec.state === 'saving') { rec.again = true; return; }
      var body = rec.local, owner = rec.code, id = rec.id;
      rec.state = 'saving'; rec.again = false;
      api('PUT', '/api/drawings/' + owner + '/' + id, { body: body, base_revision: rec.revision }, keepalive)
        .then(function (r) {
          if (r.status === 200) {
            rec.revision = r.data.revision; rec.body = r.data.body;
            if (rec.local === body) rec.local = null;
            rec.state = 'saved';
            if (rec.deleteAfter) { rec.deleteAfter = false; remove(rec); }
            else if (rec.again || rec.local) { rec.state = 'dirty'; save(rec); }
          } else if (r.status === 409 && 'current' in r.data) {
            adopt(rec, r.data.current);
          } else {
            failed(rec, '画线未保存：' + ((r.data && r.data.detail) || ('HTTP ' + r.status)));
          }
          if (owner === code) changed();
        }, function () {
          failed(rec, '画线未保存：网络错误');
          if (owner === code) changed();
        });
    }

    // 保存失败：保存途中已被用户删掉的，服务端没有就直接丢弃，有旧版本就接着删；否则标「未保存」等重试
    function failed(rec, msg) {
      if (rec.deleteAfter) {
        rec.deleteAfter = false;
        if (rec.revision == null) { rec.state = 'saved'; drop(rec); } else { rec.local = null; remove(rec); }
        return;
      }
      rec.state = 'error';
      if (rec.code === code) notify(msg);
    }

    // 服务端版本为准：被删就去掉，被改就换成服务端内容
    function adopt(rec, current) {
      clearTimeout(rec.timer);
      if (rec.code !== code) return;                 // 已换走的标的：下次打开时从服务端读
      if (!current || current.deleted_at) {
        drop(rec);
      } else {
        records[rec.id] = record(rec.id, rec.code, { body: current.body, revision: current.revision });
        if (order.indexOf(rec.id) < 0) order.push(rec.id);
      }
      notify('画线已在其他设备修改或删除，已载入最新版本');
      render();
    }

    // 删除在途时画线不再画出（render 跳过 deleting）；失败才恢复显示
    function remove(rec) {
      var owner = rec.code;
      rec.deleting = true;
      api('DELETE', '/api/drawings/' + owner + '/' + rec.id + '?base_revision=' + rec.revision).then(function (r) {
        if (r.status === 200 || r.status === 404) {
          drop(rec);
          if (owner === code) changed();
        } else if (r.status === 409 && 'current' in r.data) {
          rec.deleting = false;
          adopt(rec, r.data.current);
        } else {
          rec.deleting = false;
          if (owner === code) { notify('画线删除失败：' + ((r.data && r.data.detail) || ('HTTP ' + r.status))); render(); }
        }
      }, function () {
        rec.deleting = false;
        if (owner === code) { notify('画线删除失败：网络错误'); render(); }
      });
    }

    dm.on('add', function (d) {
      if (suppress) return;
      if (!view || view.code !== code) { quiet(function () { dm.remove(d.id); }); return; }   // 新标的的视图还没建好
      var remembered = o.styleFor ? o.styleFor(d.kind) : null;
      if (remembered && Object.keys(remembered).length) {
        quiet(function () { dm.update(Object.assign({}, d, { style: fullStyle(d.kind, Object.assign(styleDiff(d.kind, d.style), remembered)) })); });
        d = dm.get(d.id);
      }
      if (lockAll) { quiet(function () { dm.update(Object.assign({}, d, { locked: true })); }); d = dm.get(d.id); }   // 新画的也随「锁定全部」
      var rec = records[d.id] = record(d.id, code, { state: 'dirty' });
      order.push(d.id);
      rec.local = bodyFrom(d, rec);
      rec.mapped = d.points.map(function (p) { return p.time; });
      schedule(rec);
      changed();
    });
    dm.on('update', function (d) {
      if (suppress) return;
      var rec = records[d.id];
      if (!rec) return;
      rec.local = bodyFrom(d, rec) || rec.local;
      rec.mapped = d.points.map(function (p) { return p.time; });
      if (rec.state !== 'saving') rec.state = 'dirty';
      schedule(rec);
    });
    dm.on('remove', function (d) {
      if (suppress) return;
      var rec = records[d.id];
      if (!rec) return;
      clearTimeout(rec.timer); rec.timer = null;
      if (rec.state === 'saving') rec.deleteAfter = true;      // 保存回来后再删（或失败时直接丢弃）
      else if (rec.revision == null) drop(rec);
      else { rec.local = null; remove(rec); }
      changed();
    });
    dm.on('selection', changed);
    dm.on('tool', changed);
    dm.on('visibility', changed);

    // 拖动、放置进行中不重建画线（重建会丢掉手里的手势），松手后补做
    var chartEl = o.chart.chartElement();
    chartEl.addEventListener('pointerdown', function () { gesture = true; }, true);
    function release() {
      if (!gesture) return;
      gesture = false;
      if (renderPending) { renderPending = false; setTimeout(render, 0); }
    }
    window.addEventListener('pointerup', release);
    window.addEventListener('pointercancel', release);
    // 关页时把还在 1 秒合并窗口里的改动立即发出（keepalive 让请求在页面卸载后继续）
    window.addEventListener('pagehide', function () {
      order.forEach(function (id) { var rec = records[id]; if (rec && rec.timer) save(rec, true); });
    });

    function render() {
      var v = o.view();
      if (!v || v.code !== code || !v.bars.length) return;
      if (gesture) { renderPending = true; return; }
      view = buildView(v);
      var keep = dm.selection().slice();
      hidden = 0; skipped = 0;
      quiet(function () {
        dm.clear();
        order.forEach(function (id) {
          var rec = records[id];
          if (rec.deleting || rec.deleteAfter) { rec.mapped = null; return; }
          var body = rec.local || readBody(rec.body);
          if (!body) {
            skipped++; rec.mapped = null;
            if (!rec.warned) { rec.warned = true; console.warn('画线格式无法识别，已跳过', id, rec.body); }
            return;
          }
          if (body.adjust !== view.adjust) { hidden++; rec.mapped = null; return; }
          var scaled = rescale(body, view.bars, view.freq);
          if (scaled) { rec.local = scaled; body = scaled; if (rec.state !== 'saving') rec.state = 'dirty'; schedule(rec); }
          var points = body.anchors.map(function (a) { return { time: chartTime(a, view), price: a.price }; });
          if (points.some(function (p) { return p.time == null; })) { rec.mapped = null; return; }
          try {
            dm.add({ id: id, kind: body.kind, points: points, style: fullStyle(body.kind, body.style), locked: !!body.locked || lockAll });
            rec.mapped = points.map(function (p) { return p.time; });
          } catch (err) {
            skipped++; rec.mapped = null;
            if (!rec.warned) { rec.warned = true; console.warn('画线无法显示，已跳过', id, err); }
          }
        });
        dm.select(keep.filter(function (id) { return dm.get(id); }));
      });
      changed();
    }

    function flush() {
      order.forEach(function (id) { var rec = records[id]; if (rec && rec.timer) save(rec); });
    }

    function load() {
      var seq = ++loadSeq, owner = code;
      return api('GET', '/api/drawings?code=' + encodeURIComponent(owner)).then(function (r) {
        if (seq !== loadSeq || owner !== code) return;
        if (r.status !== 200) { notify('画线读取失败：' + ((r.data && r.data.detail) || ('HTTP ' + r.status))); return; }
        var next = {}, nextOrder = [];
        r.data.drawings.forEach(function (d) {
          var mine = records[d.id];
          // 本地还没存上的改动不被服务端旧版本冲掉
          next[d.id] = mine && (mine.state !== 'saved' || mine.deleting) ? mine
            : record(d.id, owner, { body: d.body, revision: d.revision });
          nextOrder.push(d.id);
        });
        order.forEach(function (id) {
          var mine = records[id];
          if (!next[id] && mine && mine.revision == null) { next[id] = mine; nextOrder.push(id); }
        });
        records = next; order = nextOrder;
        render();
      }, function () {
        if (seq === loadSeq) notify('画线读取失败：网络错误');
      });
    }

    return {
      manager: dm,
      // 图表换了数据（标的、周期、复权、分页、刷新）后调用：换标的先读服务端，否则按新数据重新映射
      sync: function () {
        var v = o.view();
        if (!v) return;
        if (v.code !== code) {
          flush();
          code = v.code; records = {}; order = []; hidden = 0; skipped = 0;
          view = buildView(v);                         // 读回之前就画的线按新标的的数据记锚点与参照
          quiet(function () { dm.clear(); });
          changed();
          load();
          return;
        }
        render();
      },
      // 回到前台：先重试没存上的，再（没有在途改动时）读服务端拿其他设备的修改
      reload: function () {
        if (!code) return;
        order.forEach(function (id) { var rec = records[id]; if (rec && rec.state === 'error') save(rec); });
        var busy = order.some(function (id) { return records[id] && records[id].state !== 'saved'; });
        if (!busy) load();
      },
      retry: function () {
        order.forEach(function (id) { var rec = records[id]; if (rec && rec.state === 'error') save(rec); });
      },
      clearAll: function () {
        if (!code) return Promise.resolve();
        var owner = code;
        return api('DELETE', '/api/drawings/' + owner).then(function (r) {
          if (owner !== code) return;
          if (r.status !== 200) { notify('清空画线失败：' + ((r.data && r.data.detail) || ('HTTP ' + r.status))); return; }
          order.forEach(function (id) { if (records[id]) clearTimeout(records[id].timer); });
          records = {}; order = []; hidden = 0; skipped = 0;
          quiet(function () { dm.clear(); });
          changed();
        }, function () { notify('清空画线失败：网络错误'); });
      },
      setTool: function (kind) { dm.setTool(kind); },
      // 单条锁定写进画线；锁定全部只改本页显示（解锁全部后各条回到自己的锁定状态）
      setLocked: function (id, on) {
        var rec = records[id], d = dm.get(id), body = rec && ownBody(rec);
        if (!d || !body) return;
        body = JSON.parse(JSON.stringify(body));
        if (on) body.locked = true; else delete body.locked;
        rec.local = body;
        if (rec.state !== 'saving') rec.state = 'dirty';
        quiet(function () { dm.update(Object.assign({}, d, { locked: on || lockAll })); });
        schedule(rec);
        changed();
      },
      isLocked: function (id) { var rec = records[id], body = rec && ownBody(rec); return !!(body && body.locked); },
      setLockAll: function (on) {
        lockAll = !!on;
        quiet(function () {
          dm.drawings().slice().forEach(function (d) {
            var rec = records[d.id], body = rec && ownBody(rec);
            dm.update(Object.assign({}, d, { locked: lockAll || !!(body && body.locked) }));
          });
        });
        changed();
      },
      setMagnet: function (mode) { dm.setMagnet(mode); },
      setVisible: function (on) { dm.setDrawingsVisible(on); },
      selected: function () {
        var sel = dm.selection();
        return sel.length === 1 ? dm.get(sel[0]) || null : null;
      },
      setStyle: function (id, patch) {
        var d = dm.get(id);
        if (!d) return null;
        var style = Object.assign({}, d.style, patch);
        dm.update(Object.assign({}, d, { style: style }));
        return styleDiff(d.kind, style);
      },
      removeSelected: function () {
        dm.selection().slice().forEach(function (id) { var d = dm.get(id); if (d && !d.locked) dm.remove(id); });
      },
      // Escape 先交给画线（退出工具、取消选中），这时不再关侧栏或弹层
      consumesEscape: function () { return dm.tool() != null || dm.selection().length > 0; },
      state: state,
      redraw: function () { dm.redraw(); },
    };
  }

  var api = { locate: locate, futureLabel: futureLabel, buildView: buildView, chartTime: chartTime, anchorDt: anchorDt,
    dayClose: dayClose, refFor: refFor, rescale: rescale, readBody: readBody, createController: createController,
    DEFAULT_COLOR: DEFAULT_COLOR, recolorPatch: recolorPatch };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.chanDrawings = api;
})(this);
