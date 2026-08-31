/* chanapp 前端：无框架无构建，lightweight-charts v5 standalone。
   视觉口径对齐 docs/v1.2.0-demo：双主题 token（默认日间）、金=结构色、红涨青跌。 */
(function () {
  'use strict';

  // ---------- 主题调色板（图表内无法走 CSS 变量，双份维护，数值同 index.html tokens） ----------

  var PAL = {
    light: {
      bg: '#ffffff', text: '#686e78', grid: 'rgba(0,0,12,.05)', border: 'rgba(0,0,12,.14)',
      up: '#d63f33', down: '#2e8ca3', upA: 'rgba(214,63,51,.45)', downA: 'rgba(46,140,163,.45)',
      upD: '#b23228', downD: '#226b78',
      gold: '#96700f', goldA: 'rgba(150,112,15,.55)',
      zsFill: 'rgba(150,112,15,.07)', zsLine: 'rgba(150,112,15,.45)',
      bi: 'rgba(90,98,110,.6)', biForming: 'rgba(90,98,110,.45)',
      ma5: '#6a7079', ma13: '#96700f', ma20: '#2e8ca3', ma60: '#d63f33', ma144: '#8a72b8', ma250: '#4f7fb8'
    },
    dark: {
      bg: '#0b0c0e', text: '#84898f', grid: 'rgba(255,255,255,.04)', border: 'rgba(255,255,255,.1)',
      up: '#df4b3e', down: '#3aa6b9', upA: 'rgba(223,75,62,.45)', downA: 'rgba(58,166,185,.45)',
      upD: '#b23c33', downD: '#2e8494',
      gold: '#c9a24d', goldA: 'rgba(201,162,77,.55)',
      zsFill: 'rgba(201,162,77,.09)', zsLine: 'rgba(201,162,77,.5)',
      bi: 'rgba(150,158,170,.7)', biForming: 'rgba(150,158,170,.5)',
      ma5: '#9aa2ad', ma13: '#c9a24d', ma20: '#3aa6b9', ma60: '#df4b3e', ma144: '#a58fd6', ma250: '#6f9fd8'
    }
  };
  var theme = document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
  var P = PAL[theme];

  function hexA(hex, a) {  // '#rrggbb' → 'rgba(r,g,b,a)'
    return 'rgba(' + parseInt(hex.slice(1, 3), 16) + ',' + parseInt(hex.slice(3, 5), 16) +
      ',' + parseInt(hex.slice(5, 7), 16) + ',' + a + ')';
  }

  var DISPLAY_BARS = 120;  // 14-16 寸屏：约 9-12px/根，密度适中；日线约半年，够看中枢/线段结构

  // 分钟级时间：lightweight-charts 字符串时间只支持 'yyyy-mm-dd'；
  // 带时分的 dt 转 UTC 秒（按 UTC 展示墙钟时间，K线/信号/中枢坐标口径一致）。
  function toTime(dt) {
    if (dt.indexOf(' ') < 0) return dt;
    return Date.parse(dt.replace(' ', 'T') + 'Z') / 1000;
  }

  var state = { code: null, freq: 'day', watchlist: [], quotes: {} };
  (function () {
    var qs = new URLSearchParams(location.search);
    if (qs.get('code')) state.code = qs.get('code');
    if (qs.get('freq')) state.freq = qs.get('freq');
  })();
  var charts = null; // {main, macdChart, candleSeries, ...}
  var chartAbort = null, analysisAbort = null;
  var klineData = [];    // 当前 K 线原始数据（time 为原始字符串，供图例/指标计算）
  var klineByTime = {};  // toTime(time) → {bar, prev}，十字光标定位用
  var macdRowsRaw = [];  // 后端 MACD 行（原始 dt 字符串）
  var beichiLinksRaw = [];
  var lastChartData = null;  // 最近一次 /api/chart 响应，主题切换时原路径重渲染
  var legendEl = null;

  // ---------- UI 骨架 ----------

  function el(id) { return document.getElementById(id); }

  function renderWatchlist() {
    var box = el('wlItems');
    box.innerHTML = '';
    state.watchlist.forEach(function (w) {
      var q = (state.quotes || {})[w.code] || {};
      var pctCls = q.pct > 0 ? 'up' : (q.pct < 0 ? 'down' : 'flat');
      var div = document.createElement('div');
      div.className = 'item' + (w.code === state.code ? ' active' : '');
      div.innerHTML = '<div class="row"><span>' + (w.starred ? '★ ' : '') + esc(w.name) +
        (q.limit_up ? '<span class="tag-limit">涨停</span>' : '') + '</span>' +
        '<span class="chg ' + pctCls + '">' +
        (q.pct == null ? '' : (q.pct > 0 ? '+' : '') + q.pct.toFixed(2) + '%') + '</span>' +
        '<span class="btns">' +
        '<button class="star' + (w.starred ? ' on' : '') + '" title="置顶">★</button>' +
        '<button class="del" title="删除">×</button></span></div>' +
        '<div class="row2"><span class="code">' + esc(w.code) + '</span>' +
        '<span class="px ' + pctCls + '">' + (q.price == null ? '' : q.price.toFixed(2)) + '</span></div>';
      div.querySelector('.star').onclick = function (ev) {
        ev.stopPropagation();
        toggleStar(w.code);
      };
      div.querySelector('.del').onclick = function (ev) {
        ev.stopPropagation();
        removeWatch(w.code);
      };
      div.onclick = function () { state.code = w.code; renderWatchlist(); load(); };
      box.appendChild(div);
    });

    var formBox = el('wlForm');
    formBox.innerHTML = '';
    var form = document.createElement('form');
    form.className = 'wl-add';
    form.innerHTML = '<input name="q" placeholder="代码 / 名称，回车添加" autocomplete="off" required>' +
      '<div class="wl-drop" hidden></div>';
    wireSearch(form);
    formBox.appendChild(form);
    updateAiTitle();  // watchlist 晚于首次 load() 返回时修正标题
    if (lastF10 && lastF10.code === state.code) renderF10Header(lastF10.json.f10 || {});  // 同上，修正 F10 卡头
  }

  // ---------- 搜索式添加（防抖 300ms → /api/search，点击候选即添加） ----------

  var searchTimer = null;

  function wireSearch(form) {
    var input = form.q;
    var drop = form.querySelector('.wl-drop');

    function hideDrop() { drop.hidden = true; drop.innerHTML = ''; }

    function showCandidates(list) {
      drop.innerHTML = '';
      if (!list.length) {
        drop.innerHTML = '<div class="cand none">无匹配结果</div>';
        drop.hidden = false;
        return;
      }
      list.forEach(function (c) {
        var div = document.createElement('div');
        div.className = 'cand';
        div.innerHTML = '<span class="c-name">' + esc(c.name) + '</span>' +
          '<span class="c-code">' + esc(c.code) + '</span>';
        div.onmousedown = function (ev) {  // mousedown 先于 input blur
          ev.preventDefault();
          hideDrop();
          input.value = '';
          addWatch(c.code, c.name, null);
        };
        drop.appendChild(div);
      });
      drop.hidden = false;
    }

    input.oninput = function () {
      var q = input.value.trim();
      if (searchTimer) clearTimeout(searchTimer);
      if (!q) { hideDrop(); return; }
      searchTimer = setTimeout(function () {
        fetch('/api/search?q=' + encodeURIComponent(q))
          .then(function (r) {
            if (!r.ok) throw new Error('搜索失败 ' + r.status);
            return r.json();
          })
          .then(function (list) {
            if (input.value.trim() === q) showCandidates(list);
          })
          .catch(function (e) { setStatus(e.message); });
      }, 300);
    };

    input.onkeydown = function (ev) {
      if (ev.key === 'Escape') { hideDrop(); input.blur(); }
    };
    input.onblur = function () { hideDrop(); };
    form.onsubmit = function (ev) { ev.preventDefault(); };  // 回车不提交，只点候选
  }

  // 点搜索区外部收起下拉
  document.addEventListener('mousedown', function (ev) {
    if (!ev.target.closest || !ev.target.closest('.wl-add')) {
      var drop = document.querySelector('.wl-drop');
      if (drop) { drop.hidden = true; drop.innerHTML = ''; }
    }
  });

  function loadWatchlist() {
    return fetch('/api/watchlist')
      .then(function (r) { return r.json(); })
      .then(function (items) {
        state.watchlist = items;
        if (!items.some(function (w) { return w.code === state.code; })) {
          state.code = items.length ? items[0].code : null;
        }
        renderWatchlist();
        if (state.code) load();
      })
      .catch(function (e) { setStatus('自选股加载失败：' + e.message); });
  }

  // 左栏行情快照：静默失败（价格只是增强，失败保留旧值）
  function loadQuotes() {
    fetch('/api/quotes')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { if (j) { state.quotes = j.quotes || {}; renderWatchlist(); } })
      .catch(function () {});
  }

  function addWatch(code, name, form) {
    fetch('/api/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: code, name: name }),
    }).then(function (r) {
      if (r.status === 409) throw new Error(code + ' 已在自选中');
      if (!r.ok) throw new Error('添加失败 ' + r.status);
      return r.json();
    }).then(function (items) {
      state.watchlist = items;
      if (form) form.reset();
      renderWatchlist();
      setStatus('');
    }).catch(function (e) { setStatus(e.message); });
  }

  function removeWatch(code) {
    fetch('/api/watchlist/' + encodeURIComponent(code), { method: 'DELETE' })
      .then(function (r) {
        if (!r.ok) throw new Error('删除失败 ' + r.status);
        return r.json();
      })
      .then(function (items) {
        state.watchlist = items;
        if (state.code === code) {
          state.code = items.length ? items[0].code : null;
          if (state.code) load();
        }
        renderWatchlist();
      })
      .catch(function (e) { setStatus(e.message); });
  }

  function toggleStar(code) {
    fetch('/api/watchlist/' + encodeURIComponent(code) + '/star', { method: 'POST' })
      .then(function (r) {
        if (!r.ok) throw new Error('置顶失败 ' + r.status);
        return r.json();
      })
      .then(function (items) {
        state.watchlist = items;
        renderWatchlist();
      })
      .catch(function (e) { setStatus(e.message); });
  }

  function renderTabs() {
    var btns = el('freqTabs').querySelectorAll('button');
    btns.forEach(function (b) {
      b.classList.toggle('active', b.dataset.freq === state.freq);
      b.onclick = function () { state.freq = b.dataset.freq; renderTabs(); load(); };
    });
  }

  // ---------- header 状态（常驻：交易时段 + 抓取时间；临时消息优先） ----------

  var statusMsg = null;   // setStatus 的临时消息，空串/null 回落到常驻状态
  var lastMeta = null;    // 最近一次 /api/chart 的 meta（抓取时间/缓存标记）

  function setStatus(msg) { statusMsg = msg || null; renderStatus(); }

  function renderStatus() {
    var s = el('status');
    if (statusMsg) { s.textContent = statusMsg; return; }
    if (!state.code) { s.textContent = ''; return; }
    var open = isSessionOpen(new Date(), marketOf(state.code));
    var html = '<span class="dot" style="color:' +
      (open ? 'var(--down)' : 'var(--faint)') + '">●</span> ' + (open ? '交易中' : '已收盘');
    if (lastMeta && lastMeta.fetch_time) {
      var m = String(lastMeta.fetch_time).match(/(\d{2}:\d{2}:\d{2})/);
      html += ' · 抓取 ' + (m ? m[1] : lastMeta.fetch_time) + (lastMeta.from_cache ? '（缓存）' : '');
    }
    s.innerHTML = html;
  }

  // ---------- 中枢矩形：覆盖 canvas（primitive 在本环境下不渲染，改自绘覆盖层） ----------

  function makeZsOverlay(container, chart, series) {
    var canvas = document.createElement('canvas');
    canvas.style.cssText = 'position:absolute;left:0;top:0;pointer-events:none;z-index:5;';
    container.appendChild(canvas);
    var overlay = {
      _boxes: [],
      setBoxes: function (boxes) { this._boxes = boxes; this.redraw(); },
      redraw: function () {
        var dpr = window.devicePixelRatio || 1;
        var w = container.clientWidth, hgt = container.clientHeight;
        if (canvas.width !== w * dpr || canvas.height !== hgt * dpr) {
          canvas.width = w * dpr; canvas.height = hgt * dpr;
          canvas.style.width = w + 'px'; canvas.style.height = hgt + 'px';
        }
        var ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, hgt);
        var ts = chart.timeScale();
        overlay._boxes.forEach(function (b) {
          var x0 = ts.timeToCoordinate(b.t0);
          var x1 = ts.timeToCoordinate(b.t1);
          var y0 = series.priceToCoordinate(b.zg);
          var y1 = series.priceToCoordinate(b.zd);
          if (x0 === null || x1 === null || y0 === null || y1 === null) return;
          var rx = Math.min(x0, x1), ry = Math.min(y0, y1);
          ctx.fillStyle = P.zsFill;
          ctx.fillRect(rx, ry, Math.abs(x1 - x0), Math.abs(y1 - y0));
          ctx.strokeStyle = P.zsLine;
          ctx.lineWidth = 1;
          ctx.setLineDash([4, 4]);
          ctx.strokeRect(rx, ry, Math.abs(x1 - x0), Math.abs(y1 - y0));
          ctx.setLineDash([]);
        });
      },
    };
    // 缩放/平移/resize 后重绘（lightweight-charts 坐标转换为异步，延迟一帧）
    var deferred = function () { requestAnimationFrame(function () { overlay.redraw(); }); };
    chart.timeScale().subscribeVisibleLogicalRangeChange(deferred);
    new ResizeObserver(deferred).observe(container);
    return overlay;
  }

  // ---------- 十字光标图例（主图左上角，默认显示最新一根） ----------

  // 成交量单位是股，图例换算为万/亿
  function fmtVol(v) {
    if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿';
    if (v >= 1e4) return (v / 1e4).toFixed(2) + '万';
    return String(v);
  }

  // 图例/依据卡/AI 缓存行日期口径（MM-DD，跨年 MM-DD-YY）——与时间轴刻度口径独立
  function fmtBarTime(t) {
    var p = t.split(' ');
    var md = p[0].slice(5);
    if (p.length === 2) return md + ' ' + p[1];
    return (+p[0].slice(0, 4) === new Date().getFullYear())
      ? md : md + '-' + p[0].slice(2, 4);
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  // ---------- 时间轴（刻度 + 十字线标签 + 左端绝对日期锚点） ----------
  // 口径（2026-08-28 裁定）：日K 刻度 MM/DD，分钟K 日界刻度 MM/DD、日内刻度 HH:MM，
  // 年界刻度恒 YYYY/MM/DD；十字线标签日K YYYY/MM/DD、分钟K YYYY/MM/DD HH:MM（金底高亮）。
  // 「最左侧刻度完整日期」用轴左端锚点实现：刻度必须是 (time,type) 的纯函数
  // （库按 mark 缓存标签且居中绘制，动态"最左"判定会残留脏标签、贴边必裁掉年份）。
  function axisTick(time, tickMarkType) {
    var T = LightweightCharts.TickMarkType;
    if (typeof time === 'string') {  // 日线 'yyyy-mm-dd'
      var p = time.split('-');
      if (tickMarkType === T.Year) return p[0] + '/' + p[1] + '/' + p[2];
      return p[1] + '/' + p[2];
    }
    if (typeof time === 'object') {  // business day 对象（兜底）
      return tickMarkType === T.Year
        ? time.year + '/' + pad2(time.month) + '/' + pad2(time.day)
        : pad2(time.month) + '/' + pad2(time.day);
    }
    var d = new Date(time * 1000);
    var md = pad2(d.getUTCMonth() + 1) + '/' + pad2(d.getUTCDate());
    if (tickMarkType === T.Year) return d.getUTCFullYear() + '/' + md;
    if (tickMarkType === T.Month || tickMarkType === T.DayOfMonth) return md;
    return pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes());
  }

  function axisLabel(time) {  // 十字线高亮标签
    if (typeof time === 'string') { var p = time.split('-'); return p[0] + '/' + p[1] + '/' + p[2]; }
    if (typeof time === 'object') return time.year + '/' + pad2(time.month) + '/' + pad2(time.day);
    var d = new Date(time * 1000);
    return d.getUTCFullYear() + '/' + pad2(d.getUTCMonth() + 1) + '/' + pad2(d.getUTCDate()) +
      ' ' + pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes());
  }

  // 轴左端锚点：日K 'YYYY/MM/DD'，分钟K 'MM/DD HH:MM'；取可视区间第一根 bar
  function updateAxisAnchor() {
    if (!charts || !klineData.length) return;
    var range = charts.main.timeScale().getVisibleRange();
    if (!range) return;
    var from = range.from;
    var bar = null;
    for (var i = 0; i < klineData.length; i++) {
      var t = toTime(klineData[i].time);
      if (t >= from) { bar = klineData[i]; break; }
    }
    if (!bar) bar = klineData[klineData.length - 1];
    var p = bar.time.split(' ');
    var ymd = p[0].split('-');
    el('axisAnchor').textContent = p.length === 2
      ? ymd[1] + '/' + ymd[2] + ' ' + p[1]
      : ymd[0] + '/' + ymd[1] + '/' + ymd[2];
  }

  function renderLegend(bar, prev) {
    if (!legendEl) return;
    if (!bar) { legendEl.textContent = ''; return; }
    var pct = '', pctCls = 'dim';
    if (prev && prev.close) {
      var chg = (bar.close - prev.close) / prev.close * 100;
      pct = (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%';
      pctCls = chg >= 0 ? 'up' : 'down';  // 涨红跌青
    }
    legendEl.innerHTML =
      '<span class="dim">' + fmtBarTime(bar.time) + '</span>' +
      ' 开 <b>' + bar.open.toFixed(2) + '</b>' +
      ' 高 <b>' + bar.high.toFixed(2) + '</b>' +
      ' 低 <b>' + bar.low.toFixed(2) + '</b>' +
      ' 收 <b>' + bar.close.toFixed(2) + '</b>' +
      ' <span class="' + pctCls + '">' + pct + '</span>' +
      ' <span class="dim">量 ' + fmtVol(bar.volume) + '</span>';
  }

  function legendLatest() {
    var n = klineData.length;
    renderLegend(n ? klineData[n - 1] : null, n > 1 ? klineData[n - 2] : null);
  }

  // ---------- 图表 ----------

  function chartOpts() {
    return {
      layout: { background: { color: P.bg }, textColor: P.text,
                fontFamily: 'ui-monospace, "SF Mono", Menlo, monospace', fontSize: 11 },
      grid: { vertLines: { color: P.grid }, horzLines: { color: P.grid } },
      crosshair: { mode: 0,
        vertLine: { color: P.goldA, labelBackgroundColor: P.gold },
        horzLine: { color: P.goldA, labelBackgroundColor: P.gold } },
      rightPriceScale: { borderColor: P.border },
      timeScale: { borderColor: P.border, rightOffset: 3, timeVisible: true, secondsVisible: false },
    };
  }

  function ensureCharts() {
    if (charts) return charts;
    var LW = LightweightCharts;
    function withAxis() {
      var o = chartOpts();
      o.timeScale.tickMarkFormatter = axisTick;
      o.localization = { timeFormatter: axisLabel };
      return o;
    }
    var main = LW.createChart(el('chart'), Object.assign(withAxis(), { autoSize: true }));
    var macdChart = LW.createChart(el('sub'), Object.assign(withAxis(), { autoSize: true }));
    // 轴左端绝对日期锚点跟随可视区间
    main.timeScale().subscribeVisibleLogicalRangeChange(updateAxisAnchor);

    var candleSeries = main.addSeries(LW.CandlestickSeries, {
      upColor: P.up, downColor: P.down, wickUpColor: P.up, wickDownColor: P.down, borderVisible: false,
    });
    var volumeSeries = main.addSeries(LW.HistogramSeries, {
      priceFormat: { type: 'volume' }, priceScaleId: 'vol',
    });
    main.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    main.priceScale('right').applyOptions({ scaleMargins: { top: 0.05, bottom: 0.22 } });

    var biSeries = main.addSeries(LW.LineSeries, {
      color: P.bi, lineWidth: 1, crosshairMarkerVisible: false,
      lastValueVisible: false, priceLineVisible: false,
    });
    var xdSeries = main.addSeries(LW.LineSeries, {
      color: P.gold, lineWidth: 2, crosshairMarkerVisible: false,
      lastValueVisible: false, priceLineVisible: false,
    });
    var formingSeries = main.addSeries(LW.LineSeries, {
      color: P.biForming, lineWidth: 1, lineStyle: LW.LineStyle.Dashed,
      crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
    });
    var formingXdSeries = main.addSeries(LW.LineSeries, {
      color: P.goldA, lineWidth: 2, lineStyle: LW.LineStyle.Dashed,
      crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
    });

    var zsOverlay = makeZsOverlay(el('chart'), main, candleSeries);

    // 十字光标图例：charts 只建一次，订阅一次即可；数据经 klineData/klineByTime 按 load 更新
    legendEl = el('ohlc');
    main.subscribeCrosshairMove(function (param) {
      if (!param.time) { legendLatest(); return; }  // 移出图表回落到最新一根
      var hit = klineByTime[param.time];
      if (hit) renderLegend(hit.bar, hit.prev); else legendLatest();
    });

    var markers = LW.createSeriesMarkers(candleSeries, []);

    // 同步时间轴（双向，防回环）
    var syncing = false;
    function sync(src, dst) {
      src.timeScale().subscribeVisibleLogicalRangeChange(function (range) {
        if (syncing || !range) return;
        syncing = true;
        dst.timeScale().setVisibleLogicalRange(range);
        syncing = false;
      });
    }
    sync(main, macdChart);
    sync(macdChart, main);

    charts = {
      main: main, macdChart: macdChart, candleSeries: candleSeries,
      volumeSeries: volumeSeries, biSeries: biSeries, xdSeries: xdSeries,
      formingSeries: formingSeries, formingXdSeries: formingXdSeries,
      zsOverlay: zsOverlay, markers: markers, channelSeries: [],
    };
    return charts;
  }

  // ---------- 日间/夜间主题 ----------

  function applyTheme(mode) {
    theme = mode; P = PAL[mode];
    document.documentElement.dataset.theme = mode;
    localStorage.setItem('chanapp-theme', mode);
    if (!charts) return;
    charts.main.applyOptions(chartOpts());
    charts.macdChart.applyOptions(chartOpts());
    charts.candleSeries.applyOptions({ upColor: P.up, downColor: P.down, wickUpColor: P.up, wickDownColor: P.down });
    charts.biSeries.applyOptions({ color: P.bi });
    charts.xdSeries.applyOptions({ color: P.gold });
    charts.formingSeries.applyOptions({ color: P.biForming });
    charts.formingXdSeries.applyOptions({ color: P.goldA });
    Object.keys(maSeries).forEach(function (n) { maSeries[n].applyOptions({ color: P['ma' + n] }); });
    // 成交量/信号箭头/中枢/通道/背驰线/副图：走同一渲染路径换色，不重置可视区间
    if (lastChartData) renderChart(lastChartData, { resetRange: false });
  }

  // ---------- MA 均线开关组（主图，可叠加多条，默认 5/13/20） ----------

  var MA_PERIODS = [5, 13, 20, 60, 144, 250];
  var activeMa = { 5: true, 13: true, 20: true };
  var maSeries = {};
  var maData = {};

  function computeMAs() {
    maData = {};
    var closes = klineData.map(function (b) { return b.close; });
    MA_PERIODS.forEach(function (n) {
      var out = [], s = 0;
      for (var i = 0; i < closes.length; i++) {
        s += closes[i]; if (i >= n) s -= closes[i - n];
        out.push(i >= n - 1 ? s / n : null);
      }
      maData[n] = out;
    });
  }

  function refreshMaSeries() {
    if (!charts) return;
    MA_PERIODS.forEach(function (n) {
      if (activeMa[n]) {
        if (!maSeries[n]) {
          maSeries[n] = charts.main.addSeries(LightweightCharts.LineSeries, {
            color: P['ma' + n], lineWidth: 1,
            priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
          });
        }
        var arr = maData[n] || [];
        maSeries[n].setData(klineData.map(function (b, i) {
          return arr[i] == null ? { time: toTime(b.time) } : { time: toTime(b.time), value: +arr[i].toFixed(2) };
        }).filter(function (x) { return x.value !== undefined; }));
      } else if (maSeries[n]) {
        charts.main.removeSeries(maSeries[n]);
        delete maSeries[n];
      }
    });
  }

  function wireMaSeg() {
    el('maSeg').addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      var n = +b.dataset.ma;
      activeMa[n] = !activeMa[n];
      b.classList.toggle('active', activeMa[n]);
      refreshMaSeries();
    });
  }

  // ---------- 副图指标（MACD 信号骨架；KDJ/RSI/BOLL 纯显示层，前端自算） ----------

  var subSeries = [];
  var curInd = 'macd';
  var SUB_NOTES = {
    macd: '背驰信号骨架 · hist=2×(DIF−DEA)',
    kdj: 'KDJ(9,3,3) · 显示用，不参与信号',
    rsi: 'RSI(14) · 显示用，不参与信号',
    boll: 'BOLL(20,2) · 显示用，不参与信号',
  };

  function clearSub() {
    if (!charts) return;
    subSeries.forEach(function (s) { charts.macdChart.removeSeries(s); });
    subSeries = [];
  }

  function subLine(color, w) {
    return { color: color, lineWidth: w || 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
  }

  function rsiArr(n) {
    var closes = klineData.map(function (b) { return b.close; });
    var out = [], ag = 0, al = 0;
    for (var i = 1; i < closes.length; i++) {
      var ch = closes[i] - closes[i - 1], g = Math.max(ch, 0), l = Math.max(-ch, 0);
      if (i <= n) { ag += g / n; al += l / n; out.push(null); }
      else { ag = (ag * (n - 1) + g) / n; al = (al * (n - 1) + l) / n; out.push(al ? 100 - 100 / (1 + ag / al) : 100); }
    }
    out.unshift(null);
    return out;
  }

  function kdjArr() {
    var K = [], D = [], J = [], k = 50, d = 50;
    for (var i = 0; i < klineData.length; i++) {
      var s = Math.max(0, i - 8), hh = -1e18, ll = 1e18;
      for (var j = s; j <= i; j++) { hh = Math.max(hh, klineData[j].high); ll = Math.min(ll, klineData[j].low); }
      var rsv = hh === ll ? 50 : (klineData[i].close - ll) / (hh - ll) * 100;
      k = 2 / 3 * k + 1 / 3 * rsv; d = 2 / 3 * d + 1 / 3 * k;
      K.push(k); D.push(d); J.push(3 * k - 2 * d);
    }
    return { k: K, d: D, j: J };
  }

  function bollArr(n, k) {
    var closes = klineData.map(function (b) { return b.close; });
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

  function lastVal(arr) { for (var i = arr.length - 1; i >= 0; i--) if (arr[i] != null) return arr[i]; }

  function showInd(name) {
    curInd = name;
    document.querySelectorAll('#subSeg button').forEach(function (b) {
      b.classList.toggle('active', b.dataset.ind === name);
    });
    if (!charts) return;
    var LW = LightweightCharts, sub = charts.macdChart;
    clearSub();
    el('subNote').textContent = SUB_NOTES[name];
    if (!klineData.length) return;
    var toPt = function (arr) {
      return klineData.map(function (b, i) {
        return arr[i] == null ? { time: toTime(b.time) } : { time: toTime(b.time), value: +arr[i].toFixed(4) };
      }).filter(function (x) { return x.value !== undefined; });
    };
    if (name === 'macd') {
      var h = sub.addSeries(LW.HistogramSeries, { priceLineVisible: false, lastValueVisible: false });
      h.setData(macdRowsRaw.map(function (m) {
        return { time: toTime(m.time), value: m.hist, color: m.hist >= 0 ? P.upA : P.downA };
      }));
      var d1 = sub.addSeries(LW.LineSeries, subLine(P.gold));
      d1.setData(macdRowsRaw.map(function (m) { return { time: toTime(m.time), value: m.dif }; }));
      var d2 = sub.addSeries(LW.LineSeries, subLine(P.bi));
      d2.setData(macdRowsRaw.map(function (m) { return { time: toTime(m.time), value: m.dea }; }));
      subSeries = [h, d1, d2];
      // 背驰虚线：锚点 bar → 信号 bar 的 DIF 连线（买点红、卖点青，与信号配色一致）
      beichiLinksRaw.forEach(function (lk) {
        if (lk.x0 >= macdRowsRaw.length || lk.x1 >= macdRowsRaw.length) return;
        var s = sub.addSeries(LW.LineSeries, {
          color: lk.label.indexOf('B1a') >= 0 ? hexA(P.up, 0.9) : hexA(P.down, 0.9),
          lineWidth: 1, lineStyle: LW.LineStyle.Dashed,
          crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
        });
        s.setData([
          { time: toTime(macdRowsRaw[lk.x0].time), value: lk.dif0 },
          { time: toTime(macdRowsRaw[lk.x1].time), value: lk.dif1 },
        ]);
        subSeries.push(s);
      });
    } else if (name === 'kdj') {
      var kdj = kdjArr();
      var k = sub.addSeries(LW.LineSeries, subLine(P.gold)); k.setData(toPt(kdj.k));
      var d = sub.addSeries(LW.LineSeries, subLine(P.bi)); d.setData(toPt(kdj.d));
      var j = sub.addSeries(LW.LineSeries, subLine(P.up)); j.setData(toPt(kdj.j));
      subSeries = [k, d, j];
    } else if (name === 'rsi') {
      var r = sub.addSeries(LW.LineSeries, subLine(P.gold));
      r.setData(toPt(rsiArr(14)));
      subSeries = [r];
    } else if (name === 'boll') {
      var boll = bollArr(20, 2);
      var u = sub.addSeries(LW.LineSeries, subLine(P.bi)); u.setData(toPt(boll.up));
      var m = sub.addSeries(LW.LineSeries, Object.assign(subLine(P.gold), { lineStyle: LW.LineStyle.Dashed }));
      m.setData(toPt(boll.mid));
      var l = sub.addSeries(LW.LineSeries, subLine(P.bi)); l.setData(toPt(boll.lo));
      // 价格线（收盘），让轨道有参照
      var closes = klineData.map(function (b) { return b.close; });
      var c = sub.addSeries(LW.LineSeries, subLine(P.up)); c.setData(toPt(closes));
      subSeries = [u, m, l, c];
      // 附注带最新数值
      el('subNote').innerHTML =
        'BOLL(20,2) · MID <b style="color:' + P.gold + '">' + lastVal(boll.mid).toFixed(2) + '</b>' +
        ' · UP <b style="color:' + P.bi + '">' + lastVal(boll.up).toFixed(2) + '</b>' +
        ' · LOW <b style="color:' + P.bi + '">' + lastVal(boll.lo).toFixed(2) + '</b>' +
        ' · C <b style="color:' + P.up + '">' + closes[closes.length - 1].toFixed(2) + '</b> · 显示用';
    }
  }

  function wireSubSeg() {
    el('subSeg').addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      showInd(b.dataset.ind);
    });
  }

  // ---------- 多级别共振角标 ----------

  var FREQ_NAME = { day: '日线', m60: '60分', m30: '30分' };

  function sigSide(label) {  // 'B1a' / '段:B3' / '~S1-d' → buy/sell
    return label.replace(/^~/, '').replace(/^段:/, '').charAt(0) === 'B' ? 'b' : 's';
  }

  function renderResonance(list) {
    var box = el('resonance');
    box.innerHTML = '';
    (list || []).forEach(function (lv) {
      var html = '<span class="lv">' + (FREQ_NAME[lv.freq] || lv.freq) + '</span>';
      if (lv.forming_signal) {
        html += '<span class="sig-' + sigSide(lv.forming_signal.label) + '">' +
          esc(lv.forming_signal.label) + '</span> 雏形';
      } else if (lv.signal) {
        var d = String(lv.signal.dt).split(' ')[0].slice(5).replace('-', '/');
        html += '<span class="sig-' + sigSide(lv.signal.label) + '">' +
          esc(lv.signal.label) + '</span> ' + d;
      } else if (lv.zs) {
        html += '<span class="zs">' + (lv.zs.inside ? '中枢内 ' : '中枢外 ') +
          lv.zs.zd + '–' + lv.zs.zg + '</span>';
      } else {
        html += '<span class="lv">—</span>';
      }
      var chip = document.createElement('span');
      chip.className = 'res-chip';
      chip.innerHTML = html;
      box.appendChild(chip);
    });
  }

  // ---------- 信号标注 ----------

  // 信号配色：段级金色粗箭头；笔级 B/S 用涨跌色，B2/S2 降饱和，B3/S3 加深
  function signalColor(s) {
    if (s.label.indexOf('段:') === 0) return P.gold;
    switch (s.label) {
      case 'B1a': case 'B1a-d': return P.up;
      case 'S1': case 'S1-d': return P.down;
      case 'B2': return hexA(P.up, 0.6);
      case 'S2': return hexA(P.down, 0.6);
      case 'B3': return P.upD;
      case 'S3': return P.downD;
    }
    return s.side === 'buy' ? P.up : P.down;
  }

  function buildMarkers(signals, formingSignal) {
    var ms = signals.map(function (s) {
      var buy = s.side === 'buy';
      var seg = s.label.indexOf('段:') === 0;   // 段级信号：金色粗箭头
      return {
        time: toTime(s.dt),
        position: buy ? 'belowBar' : 'aboveBar',
        shape: buy ? 'arrowUp' : 'arrowDown',
        color: signalColor(s),
        text: s.label,
        size: seg ? 2 : 1,
      };
    });
    // 雏形买卖点预判：半透明箭头 + "~" 前缀标签（未确认，区别于实色确认信号）
    if (formingSignal) {
      var fbuy = formingSignal.side === 'buy';
      ms.push({
        time: toTime(formingSignal.dt),
        position: fbuy ? 'belowBar' : 'aboveBar',
        shape: fbuy ? 'arrowUp' : 'arrowDown',
        color: fbuy ? P.upA : P.downA,
        text: formingSignal.label,
        size: 1,
      });
    }
    return ms.sort(function (a, b) { return a.time < b.time ? -1 : 1; });
  }

  // ---------- 依据卡 ----------

  function renderEvidence(evidence) {
    var box = el('cards');
    box.innerHTML = '';
    var sorted = evidence.slice().sort(function (a, b) { return a.dt < b.dt ? 1 : -1; });
    if (!sorted.length) {
      box.innerHTML = '<div class="card"><span class="text">当前区间无信号</span></div>';
      return;
    }
    sorted.forEach(function (c) {
      var div = document.createElement('div');
      div.className = 'card';
      var sideCls = c.side === 'buy' ? 'lbl-buy' : 'lbl-sell';
      div.innerHTML =
        '<div class="head"><span class="' + sideCls + '">' + c.type + '</span>' +
        '<span class="price">' + c.price.toFixed(2) + '</span></div>' +
        '<div class="text">' + fmtBarTime(c.dt) + '</div>' +
        '<div class="text">' + c.text + '</div>';
      box.appendChild(div);
    });
  }

  // ---------- AI 完全分类面板 ----------

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  // AI 面板标题跟随当前 code：自选股名称优先，查不到（URL 直入未加自选）回落 code
  function updateAiTitle() {
    if (!state.code) return;
    var w = null;
    state.watchlist.forEach(function (x) { if (x.code === state.code) w = x; });
    el('aiTitle').textContent = (w ? w.name : state.code) + '-AI完全分类';
  }

  // 缓存行的结构参考时点：分钟级当日 'HH:mm'（如「10:30后结构未变化」），
  // 跨日复用 fmtBarTime（'MM-DD HH:mm'）；日线 fmtBarTime（MM-DD / MM-DD-YY）
  function fmtRefDt(dt) {
    var p = String(dt).split(' ');
    if (p.length !== 2) return fmtBarTime(dt);
    var t = new Date();
    var ymd = t.getFullYear() + '-' + pad2(t.getMonth() + 1) + '-' + pad2(t.getDate());
    return (p[0] === ymd) ? p[1] : fmtBarTime(dt);
  }

  // noteMode: null（真实结果）| 'unconfigured'（未配置 LLM）| 'fallback'（接口失败回落样例）
  function renderAnalysis(j, noteMode) {
    var box = el('aiPanel');
    var html = '';
    if (noteMode === 'unconfigured') {
      html += '<div class="ai-note">未配置 LLM，仅显示依据卡（以下为静态样例）</div>';
    } else if (noteMode === 'fallback') {
      html += '<div class="ai-note">完全分类接口不可用，以下为静态样例</div>';
    } else if (j.cached) {
      html += '<div class="ai-note">缓存结果（' +
        (j.ref_bar_dt ? fmtRefDt(j.ref_bar_dt) + '后结构未变化' : '结构未变化') +
        '）</div>';
    }
    if (j.current_state) {
      html += '<div class="ai-state">' + esc(j.current_state) + '</div>';
    }
    if (j.raw) {  // LLM 输出解析失败：展示原文
      html += '<div class="card"><div class="kv">' + esc(j.raw) + '</div></div>';
    }
    (j.scenarios || []).slice(0, 3).forEach(function (s) {
      html += renderScenario(s);
    });
    if (!j.current_state && !(j.scenarios || []).length && !j.raw) {
      html += '<div class="card"><span class="text">暂无完全分类结果</span></div>';
    }
    box.innerHTML = html;
  }

  // 后验置信徽章：从 posterior 文本开头提取 高/中/低 档位
  function posteriorBadge(posterior) {
    var lv = { '高': 'hi', '中': 'mid', '低': 'lo' }[String(posterior || '').charAt(0)];
    if (!lv) return '';
    var txt = { hi: '后验·高', mid: '后验·中', lo: '后验·低' }[lv];
    return '<span class="badge ' + lv + '">' + txt + '</span>';
  }

  function kv(label, value, cls) {
    if (value == null || value === '') return '';
    return '<div class="kv' + (cls ? ' ' + cls : '') + '"><b>' + label + '</b>' +
      esc(value) + '</div>';
  }

  function kvList(label, items, cls) {
    if (!items) return '';
    var list = Array.isArray(items) ? items : [items];
    if (!list.length) return '';
    return '<div class="kv ' + cls + '"><b>' + label + '</b>' +
      list.map(esc).join('；') + '</div>';
  }

  // 贝叶斯情景卡：先验/证据（支持红、削弱青）/后验徽章/更新观察，旧字段保留；
  // 新字段缺失（旧格式缓存）时对应行不渲染
  function renderScenario(s) {
    return '<div class="card">' +
      '<div class="head"><span>' + esc(s.name) + '</span>' +
      posteriorBadge(s.posterior) + '</div>' +
      kv('先验：', s.prior) +
      kvList('支持：', s.evidence_for, 'ev-for') +
      kvList('削弱：', s.evidence_against, 'ev-against') +
      kv('后验：', s.posterior) +
      kv('触发：', s.trigger) +
      kv('边界：', s.boundary) +
      kv('应对：', s.action) +
      kv('依据：', s.basis) +
      kv('更新观察：', s.update_watch) +
      '</div>';
  }

  function fetchAnalysisSample() {
    return fetch('/analysis.sample.json').then(function (r) {
      if (!r.ok) throw new Error('sample ' + r.status);
      return r.json();
    });
  }

  // fetch 失败或未配置 LLM 时回落到静态样例渲染，保证无 key 也能验收 UI
  function loadAnalysis() {
    if (!state.code) { el('aiPanel').innerHTML = ''; return; }
    if (analysisAbort) analysisAbort.abort();
    analysisAbort = new AbortController();
    fetch('/api/analysis?code=' + encodeURIComponent(state.code) + '&freq=' + state.freq, { signal: analysisAbort.signal })
      .then(function (r) {
        if (!r.ok) throw new Error('analysis ' + r.status);
        return r.json();
      })
      .then(function (j) {
        if (j.status === 'ok') { renderAnalysis(j, null); return; }
        return fetchAnalysisSample().then(function (s) { renderAnalysis(s, 'unconfigured'); });
      })
      .catch(function (e) {
        if (e && e.name === 'AbortError') return;  // 被新请求中止，静默
        fetchAnalysisSample()
          .then(function (s) { renderAnalysis(s, 'fallback'); })
          .catch(function () {
            el('aiPanel').innerHTML = '<div class="ai-note">完全分类不可用</div>';
          });
      });
  }

  // ---------- 数据口径条 ----------

  function renderMeta(meta) {
    lastMeta = meta;
    renderStatus();
    var bar = el('metaBar');
    var parts = [
      '数据源：' + meta.source,
      '复权口径：' + meta.fqf,
      '抓取时间：' + meta.fetch_time + (meta.from_cache ? '（缓存）' : ''),
      'K线：' + meta.bars + ' 根（' + meta.first_dt + ' ~ ' + meta.last_dt + '）',
    ];
    bar.innerHTML = parts.map(function (p) { return '<span>' + p + '</span>'; }).join('');
    if (meta.degraded) {
      var warn = document.createElement('span');
      warn.className = 'warn';
      warn.textContent = meta.degraded_note;
      bar.appendChild(warn);
    }
  }

  // ---------- F10 摘要 + 当日资金流（行情显示层，右栏顶部两卡） ----------

  var F10_TTL = 300 * 1000;  // F10 盘中变化慢：同一代码 300s 内不重复拉取（60s 自动刷新不刷 F10）
  var f10Last = { code: null, ts: 0 };
  var lastF10 = null;        // {code, json}，watchlist/quotes 晚到时重同步卡头

  // 金额格式化：亿/万；null → '--'
  function fmtYi(v) {
    if (v == null) return '--';
    if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(2) + '亿';
    return (v / 1e4).toFixed(2) + '万';
  }

  function numOr(v) { return v == null ? '--' : String(v); }
  function pctOr(v) { return v == null ? '--' : v + '%'; }
  function signCls(v) { return v > 0 ? 'up' : (v < 0 ? 'down' : 'flat'); }

  function watchName(code) {
    var w = null;
    state.watchlist.forEach(function (x) { if (x.code === code) w = x; });
    return w ? w.name : code;
  }

  function hideF10Cards() {
    el('f10Sec').style.display = 'none';
    el('flowSec').style.display = 'none';
  }

  // 卡头：名称取自选股（查不到回落 code）；价/涨幅优先用 state.quotes，缺失用 f.price
  function renderF10Header(f) {
    var q = (state.quotes || {})[state.code] || {};
    el('f10Name').textContent = watchName(state.code);
    el('f10Code').textContent = state.code;
    var price = q.price != null ? q.price : f.price;
    var pct = q.pct;
    var px = el('f10Px');
    px.textContent = price == null ? '--' : price.toFixed(2);
    px.className = 'px' + (pct == null ? '' : ' ' + signCls(pct));
    var chg = el('f10Chg');
    chg.textContent = pct == null ? '' : (pct > 0 ? '+' : '') + pct.toFixed(2) + '%';
    chg.className = 'chg' + (pct == null ? '' : ' ' + signCls(pct));
  }

  function renderF10(j) {
    var f = j.f10 || {};
    el('f10Sec').style.display = '';
    renderF10Header(f);
    var tags = '';
    if (f.industry) {
      var ip = j.industry_pct;
      tags += '<span class="chip ind">' + esc(f.industry) +
        (ip == null ? '' : '<span class="pct ' + signCls(ip) + '">' +
          (ip > 0 ? '+' : '') + ip.toFixed(2) + '%</span>') + '</span>';
    }
    (f.concepts || []).forEach(function (c) {
      tags += '<span class="chip">' + esc(c) + '</span>';
    });
    el('f10Tags').innerHTML = tags;
    var cells = [
      ['总市值', fmtYi(f.total_mv)], ['PE(TTM)', numOr(f.pe_ttm)], ['PB', numOr(f.pb)],
      ['成交额', fmtYi(f.amount)], ['换手', pctOr(f.turnover)], ['量比', numOr(f.volume_ratio)],
      ['外盘', fmtYi(f.outer)], ['内盘', fmtYi(f.inner)], ['振幅', pctOr(f.amplitude)],
    ];
    el('f10Grid').innerHTML = cells.map(function (c) {
      return '<div class="kv"><div class="k">' + c[0] + '</div><div class="v">' + c[1] + '</div></div>';
    }).join('');
  }

  function renderFlow(j) {
    var fl = j.flow || {};
    el('flowSec').style.display = '';
    var m = fl.main;
    var main = el('flowMain');
    main.textContent = m == null ? '--' : (m > 0 ? '+' : '') + fmtYi(m);
    main.className = 'v ' + (m == null ? 'flat' : signCls(m));
    var rows = [['超大单', fl['super']], ['大单', fl.large], ['中单', fl.medium], ['小单', fl.small]];
    el('flowBars').innerHTML = rows.map(function (r) {
      var v = r[1];
      var w = v == null ? 0 : Math.min(Math.abs(v) / 1.2e8 * 50, 50);
      var pos = v != null && v >= 0;
      return '<div class="frow"><span class="k">' + r[0] + '</span>' +
        '<span class="track"><i class="' + (pos ? 'pos' : 'neg') + '" style="width:' + w.toFixed(1) + '%"></i></span>' +
        '<span class="v ' + (v == null ? 'flat' : signCls(v)) + '">' +
        (v == null ? '--' : (v > 0 ? '+' : '') + fmtYi(v)) + '</span></div>';
    }).join('');
  }

  // 与 /api/chart 并行调用；失败（含 502/退避）→ 两卡整卡隐藏，不显示报错
  function loadF10(code) {
    if (!code) return;
    var now = Date.now();
    if (f10Last.code === code && now - f10Last.ts < F10_TTL) return;
    if (f10Last.code !== code) { hideF10Cards(); lastF10 = null; }  // 切换代码先清旧数据
    f10Last = { code: code, ts: now };
    fetch('/api/f10?code=' + encodeURIComponent(code))
      .then(function (r) { if (!r.ok) throw new Error('f10 ' + r.status); return r.json(); })
      .then(function (j) {
        if (code !== state.code) return;  // 响应期间已切走，丢弃
        lastF10 = { code: code, json: j };
        renderF10(j);
        renderFlow(j);
      })
      .catch(function () {
        if (f10Last.code === code) f10Last.ts = 0;  // 失败不计入 TTL，下次 load 重试（仅当未已切走）
        if (code !== state.code) return;
        lastF10 = null;
        hideF10Cards();
      });
  }

  // ---------- 数据加载 ----------

  function segsToPoints(segs) {
    return segs.map(function (b) {
      return [{ time: toTime(b.dt0), value: b.y0 }, { time: toTime(b.dt1), value: b.y1 }];
    }).reduce(function (acc, p) {
      if (acc.length && acc[acc.length - 1].time === p[0].time) acc.pop();
      return acc.concat(p);
    }, []);
  }

  // 图表渲染主路径：fetch 后与主题切换共用（opts.resetRange=false 时保留可视区间）
  function renderChart(data, opts) {
    var c = ensureCharts();
    opts = opts || {};
    var kline = data.kline;
    klineData = kline;
    klineByTime = {};
    kline.forEach(function (b, i) {
      klineByTime[toTime(b.time)] = { bar: b, prev: i > 0 ? kline[i - 1] : null };
    });
    macdRowsRaw = data.macd.rows;
    beichiLinksRaw = data.macd.beichi_links;
    lastChartData = data;

    c.candleSeries.setData(kline.map(function (b) {
      return { time: toTime(b.time), open: b.open, high: b.high, low: b.low, close: b.close };
    }));
    c.volumeSeries.setData(kline.map(function (b) {
      return { time: toTime(b.time), value: b.volume, color: b.close >= b.open ? P.upA : P.downA };
    }));

    // 笔：端点连续（zigzag），单线即可；雏形笔虚线
    c.biSeries.setData(segsToPoints(data.structure.bi));
    var xdConfirmed = data.structure.xd.filter(function (s) { return !s.forming; });
    var xdForming = data.structure.xd.filter(function (s) { return s.forming; });
    c.xdSeries.setData(segsToPoints(xdConfirmed));
    c.formingXdSeries.setData(xdForming.map(function (s) {
      return [{ time: toTime(s.dt0), value: s.y0 }, { time: toTime(s.dt1), value: s.y1 }];
    }).reduce(function (acc, p) { return acc.concat(p); }, []));
    var f = data.structure.forming;
    c.formingSeries.setData(f ? [{ time: toTime(f.dt0), value: f.y0 }, { time: toTime(f.dt1), value: f.y1 }] : []);

    c.zsOverlay.setBoxes(data.structure.zs.map(function (z) {
      return { t0: toTime(z.dt0), t1: toTime(z.dt1), zg: z.zg, zd: z.zd };
    }));
    c.markers.setMarkers(buildMarkers(data.signals, data.forming_signal));

    computeMAs();
    refreshMaSeries();
    showInd(curInd);

    // 通道线：最近一条已完成线段的上下轨，金色虚线（主图）
    c.channelSeries.forEach(function (s) { c.main.removeSeries(s); });
    c.channelSeries = (data.channels || []).reduce(function (acc, ch) {
      ['upper', 'lower'].forEach(function (k) {
        var r = ch[k];
        var s = c.main.addSeries(LightweightCharts.LineSeries, {
          color: P.goldA,
          lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
          crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
        });
        s.setData([
          { time: toTime(r.t0), value: r.y0 },
          { time: toTime(r.t1), value: r.y1 },
        ]);
        acc.push(s);
      });
      return acc;
    }, []);

    if (opts.resetRange !== false) {
      // 展示最近 DISPLAY_BARS 根（计算仍为全量）
      var n = kline.length;
      var range = { from: Math.max(0, n - DISPLAY_BARS), to: n - 1 + 3 };
      c.main.timeScale().setVisibleLogicalRange(range);
      c.macdChart.timeScale().setVisibleLogicalRange(range);
    }

    legendLatest();
    renderResonance(data.resonance);
    updateAxisAnchor();
  }

  function load() {
    ensureCharts();
    if (chartAbort) chartAbort.abort();
    chartAbort = new AbortController();
    el('aiPanel').innerHTML = '<div class="ai-note">加载中…</div>';  // 切换即清上一只结论
    updateAiTitle();
    setStatus('加载中… ' + state.code + ' ' + state.freq);
    loadF10(state.code);  // F10/资金流与 /api/chart 并行，互不阻塞（内部有 300s TTL）
    fetch('/api/chart?code=' + encodeURIComponent(state.code) + '&freq=' + state.freq, { signal: chartAbort.signal })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.detail || r.status); });
        return r.json();
      })
      .then(function (data) {
        renderChart(data, { resetRange: true });
        renderEvidence(data.evidence);
        renderMeta(data.meta);
        loadAnalysis();  // 完全分类面板跟随 code/freq 切换刷新
        setStatus('');
      })
      .catch(function (e) {
        if (e && e.name === 'AbortError') return;  // 被新请求中止，静默
        setStatus('加载失败：' + e.message);
      });
  }

  // ---------- 交易时段自动刷新（口径同 engine/session.py：cn 09:25–15:05，hk 09:30–16:05，周一至五） ----------

  function marketOf(code) { return code && code.indexOf('hk') === 0 ? 'hk' : 'cn'; }

  function isSessionOpen(now, market) {
    var day = now.getDay();
    if (day === 0 || day === 6) return false;
    var mins = now.getHours() * 60 + now.getMinutes();
    var win = market === 'hk' ? [9 * 60 + 30, 16 * 60 + 5] : [9 * 60 + 25, 15 * 60 + 5];
    return mins >= win[0] && mins <= win[1];
  }

  setInterval(function () {
    renderStatus();  // 交易时段点/分钟级状态保鲜
    if (!state.code) return;
    if (!isSessionOpen(new Date(), marketOf(state.code))) return;
    load();
    loadQuotes();
  }, 60000);

  // ---------- 初始化 ----------

  el('themeBtn').addEventListener('click', function () {
    applyTheme(theme === 'light' ? 'dark' : 'light');
  });
  el('aiRefresh').onclick = function () { loadAnalysis(); };
  wireMaSeg();
  wireSubSeg();

  renderTabs();
  renderStatus();
  loadWatchlist();
  loadQuotes();
})();
