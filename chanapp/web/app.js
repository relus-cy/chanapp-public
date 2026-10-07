/* chanapp 前端：无框架无构建，lightweight-charts v5 standalone。
   视觉口径：双主题 token（默认日间）、金=结构色、红涨青跌。 */
(function () {
  'use strict';

  // ---------- 主题调色板（图表内无法走 CSS 变量，共享色读 index.html tokens） ----------

  var PAL = {
    light: {
      bg: '#ffffff', grid: 'rgba(0,0,12,.05)', border: 'rgba(0,0,12,.14)',
      upD: '#b23228', downD: '#226b78',
      bi: 'rgba(90,98,110,.6)', biForming: 'rgba(90,98,110,.45)',
      zsFillA: .07, zsLineA: .45
    },
    dark: {
      bg: '#0b0c0e', grid: 'rgba(255,255,255,.04)', border: 'rgba(255,255,255,.1)',
      upD: '#b23c33', downD: '#2e8494',
      bi: 'rgba(150,158,170,.7)', biForming: 'rgba(150,158,170,.5)',
      zsFillA: .09, zsLineA: .5
    }
  };

  function hexA(hex, a) {  // '#rrggbb' → 'rgba(r,g,b,a)'
    return 'rgba(' + parseInt(hex.slice(1, 3), 16) + ',' + parseInt(hex.slice(3, 5), 16) +
      ',' + parseInt(hex.slice(5, 7), 16) + ',' + a + ')';
  }

  function palette(mode) {
    var css = getComputedStyle(document.documentElement);
    function v(name) { return css.getPropertyValue(name).trim(); }
    var chart = PAL[mode], p = {};
    for (var k in chart) p[k] = chart[k];
    var up = v('--up'), down = v('--down'), gold = v('--gold');
    p.text = v('--dim');
    p.up = up; p.down = down; p.gold = gold;
    p.upA = hexA(up, .45); p.downA = hexA(down, .45); p.goldA = hexA(gold, .55);
    p.zsFill = hexA(gold, chart.zsFillA); p.zsLine = hexA(gold, chart.zsLineA);
    [5, 13, 20, 60, 144, 250].forEach(function (n) { p['ma' + n] = v('--ma' + n); });
    return p;
  }

  var theme = document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
  var P = palette(theme);

  var DISPLAY_BARS = 120;  // 14-16 寸屏：约 9-12px/根，密度适中；日线约半年，够看中枢/线段结构

  // 分钟级时间：lightweight-charts 字符串时间只支持 'yyyy-mm-dd'；
  // 带时分的 dt 转 UTC 秒（按 UTC 展示墙钟时间，K线/信号/中枢坐标口径一致）。
  function toTime(dt) {
    if (dt.indexOf(' ') < 0) return dt;
    return Date.parse(dt.replace(' ', 'T') + 'Z') / 1000;
  }

  // sideTab：侧栏显示自选（watch）还是最近查看（recent）；recent 来自 /api/views（查看记录，不代表关注状态）
  var state = { code: null, freq: 'day', ruleProfile: 'strict', signalScope: 'expanded', watchlist: [], quotes: {},
    sideTab: 'watch', recent: [] };
  try { if (localStorage.getItem('chanapp-side-tab') === 'recent') state.sideTab = 'recent'; } catch (_) {}
  try { if (localStorage.getItem('chanapp-rule-profile') === 'relaxed') state.ruleProfile = 'relaxed'; } catch (_) {}
  try { if (localStorage.getItem('chanapp-signal-scope') === 'standard') state.signalScope = 'standard'; } catch (_) {}
  (function () {
    var qs = new URLSearchParams(location.search);
    if (qs.get('code')) state.code = qs.get('code');
    if (qs.get('freq')) state.freq = normalizeFreq(qs.get('freq'));  // 旧链接的 5分/15分归到 30 分
  })();
  var periodPrefs = null; // 服务端实例偏好；确认完成前不加载图表
  var periodSaving = false;
  var periodLoad = null;
  var charts = null; // {main, macdChart, candleSeries, ...}
  var chartAbort = null;
  var CHART_TIMEOUT_MS = 25000;     // 冷取数正常 ~8s；25s 在病理性兜底链前切断
  var REFETCH_TIMEOUT_MS = 120000;  // 手动重拉整段重取分析窗口（按月分片多次请求），比常规读取慢得多
  var ANALYSIS_TIMEOUT_MS = 150000; // 覆盖可配置的 LLM 最长 120s 及联合数据准备
  var klineData = [];    // 当前 K 线原始数据（time 为原始字符串，供图例/指标计算）
  var klineByTime = {};  // toTime(time) → {bar, prev}，十字光标定位用
  var macdRowsRaw = [];  // 后端 MACD 行（原始 dt 字符串）
  // 当前视图的数据身份（与 state 的 code/freq 一起决定响应是否仍可接纳）：
  //   adjust        请求的复权模式（qfq/raw），主图、分页、分析都带上；
  //   token         主图视图令牌，左拉分页必带，服务端判不符返回 409 → 整窗重载；
  //   analysisTokens 主图同次读取给出的 {day, m60, m30} 令牌，是 AI 请求令牌的唯一来源。
  var viewState = { adjust: 'qfq', token: null, analysisTokens: null };
  // 复权模式是用户偏好（指数由服务端恒按不复权返回，偏好不因浏览指数而改变）
  try { if (localStorage.getItem('chanapp-adjust') === 'raw') viewState.adjust = 'raw'; } catch (_) {}
  var historyState = { loading: false, hasMore: false, reqId: 0 };
  var lastChartData = null;  // 最近一次 /api/chart 响应，主题切换时原路径重渲染
  var loadedTarget = null;   // 当前图表内容归属 {code, freq, adjust}，后台刷新据此判定是否保留可视区间
  var restoreRange = null;   // {code, freq, range}：规则切换重载前的可视时间区间，新图提交后恢复
  var chartEtag = null;  // {code, freq, adjust, profile, scope, etag}：最近一次 200 的 /api/chart ETag，60s 自动刷新带 If-None-Match
  function rememberEtag(code, freq, adjust, profile, scope, etag) {
    chartEtag = etag ? { code: code, freq: freq, adjust: adjust, profile: profile, scope: scope, etag: etag } : null;
  }
  function conditionalHeaders(code, freq, adjust, profile, scope) {
    if (!chartEtag || chartEtag.code !== code || chartEtag.freq !== freq || chartEtag.adjust !== adjust ||
        chartEtag.profile !== profile || chartEtag.scope !== scope) return {};
    return { 'If-None-Match': chartEtag.etag };
  }
  var legendEl = null;

  // ---------- UI 骨架 ----------

  var SVG_STAR = '<svg class="ic-star" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.5l2.6 5.3 5.9.9-4.2 4.1 1 5.8-5.3-2.8-5.3 2.8 1-5.8L3.5 9.7l5.9-.9z"/></svg>';
  var SVG_X = '<svg class="ic-x" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';

  function el(id) { return document.getElementById(id); }

  // 行情行的展示口径（自选行与右栏卡头共用，保证两处一致）：
  // A 股行取自 K 线事实（price_label 最新/昨收；事实缺失 price_unavailable），港股行为显示层快照。
  // 昨收时涨跌为空；不回退 F10 价格。
  // 港股报价只带源时间戳（秒）：按北京时间（A 股、港股同为 UTC+8，无夏令时）写成与 price_time 同样的格式
  function sourceTime(sec) {
    if (typeof sec !== 'number' || !isFinite(sec) || sec <= 0) return '';
    return new Date((sec + 8 * 3600) * 1000).toISOString().slice(0, 16).replace('T', ' ');
  }

  function quoteView(q) {
    q = q || {};
    var price = q.price == null ? null : q.price;
    var pct = price == null || q.price_label === '昨收' ? null : (q.pct == null ? null : q.pct);
    return { price: price, pct: pct, prevClose: price != null && q.price_label === '昨收',
             historical: price != null && q.price_label === '历史',
             unavailable: q.price_unavailable === true, time: q.price_time || sourceTime(q.source_ts),
             stale: price != null && q.stale === true };
  }

  var SVG_PLUS = '<svg class="ic-plus" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';

  // 关注状态只认自选列表（唯一事实）；最近查看里服务端给的 watched 只是快照，渲染时不用它
  function isWatched(code) {
    return state.watchlist.some(function (w) { return w.code === code; });
  }

  function nameOf(code) {
    var hit = null;
    state.watchlist.concat(state.recent || []).some(function (w) { return w.code === code && (hit = w); });
    return hit ? hit.name : code;
  }

  // 最近查看时刻：服务端 ISO 本地时间取「MM-DD HH:mm」；本页刚打开、尚未回读的条目显示「刚刚」
  function viewedAt(ts) {
    return ts ? String(ts).slice(5, 16).replace('T', ' ') : '刚刚';
  }

  function renderWatchlist() {
    var box = el('wlItems');
    var focused = document.activeElement;
    var focusedRow = focused && focused.closest('.item');
    var focusedCode = focusedRow && focusedRow.dataset.code;
    var focusedAction = focusedRow && (['star', 'del', 'add'].filter(function (c) { return focused.classList.contains(c); })[0] || null);
    var restoreRow = null;
    var recentMode = state.sideTab === 'recent';
    var list = recentMode ? (state.recent || []) : state.watchlist;
    box.innerHTML = '';
    list.forEach(function (w) {
      var div = document.createElement('div');
      div.dataset.code = w.code;
      div.className = 'item' + (w.code === state.code ? ' active' : '');
      if (recentMode) {
        var watched = isWatched(w.code);
        div.innerHTML = '<div class="row"><span>' + esc(w.name) +
          (watched ? '<span class="tag-watched">已自选</span>' : '') + '</span>' +
          '<span class="btns">' +
          (watched ? '' : '<button class="add" title="加入自选" aria-label="加入自选">' + SVG_PLUS + '</button>') +
          '</span></div>' +
          '<div class="row2"><span class="code">' + esc(w.code) + '</span>' +
          '<span class="px" title="最近查看">' + esc(viewedAt(w.last_viewed_at)) + '</span></div>';
        if (!watched) {
          div.querySelector('.add').onclick = function (ev) {
            ev.stopPropagation();
            addWatch(w.code, w.name, null);
          };
        }
      } else {
        var q = (state.quotes || {})[w.code] || {};
        var v = quoteView(q);  // 与右栏卡头同一口径；缺价格时价格与涨跌一并显空
        var pctCls = v.pct > 0 ? 'up' : (v.pct < 0 ? 'down' : 'flat');
        div.innerHTML = '<div class="row"><span>' + esc(w.name) +
          (q.limit_up ? '<span class="tag-limit">涨停</span>' : '') + '</span>' +
          '<span class="chg ' + pctCls + '">' +
          (v.pct == null ? '' : (v.pct > 0 ? '+' : '') + v.pct.toFixed(2) + '%') + '</span>' +
          '<span class="btns">' +
          '<button class="star' + (w.starred ? ' on' : '') + '" title="置顶" aria-label="置顶">' + SVG_STAR + '</button>' +
          '<button class="del" title="移出自选" aria-label="移出自选">' + SVG_X + '</button></span></div>' +
          '<div class="row2"><span class="code">' + esc(w.code) + '</span>' +
          '<span class="px ' + pctCls + '"' + (v.time ? ' title="价格时刻 ' + esc(v.time) + '"' : '') + '>' +
          (v.unavailable ? '暂无可信价格' : v.price == null ? '' : v.price.toFixed(2) + (v.historical ? ' 历史' : v.prevClose ? ' 昨收' : '') + (v.stale ? ' 延迟' : '')) +
          '</span></div>';
        div.querySelector('.star').onclick = function (ev) {
          ev.stopPropagation();
          toggleStar(w.code);
        };
        div.querySelector('.del').onclick = function (ev) {
          ev.stopPropagation();
          removeWatch(w.code);
        };
      }
      div.onclick = function () { openCode(w.code, w.name); };
      div.tabIndex = 0;
      div.onkeydown = function (ev) {
        if (ev.target !== div) return;
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); div.onclick(); }
      };
      box.appendChild(div);
      if (w.code === focusedCode) restoreRow = div;
    });
    if (recentMode && !list.length) {
      var empty = document.createElement('div');
      empty.className = 'wl-empty';
      empty.textContent = '暂无查看记录';
      box.appendChild(empty);
    }
    if (restoreRow) {
      restoreRow.focus({preventScroll: true});
      var action = focusedAction && restoreRow.querySelector('.' + focusedAction);
      if (action) action.focus({preventScroll: true});
    }

    [['wlTabWatch', 'watch'], ['wlTabRecent', 'recent']].forEach(function (t) {
      var tab = el(t[0]);
      if (!tab) return;
      tab.setAttribute('aria-selected', state.sideTab === t[1] ? 'true' : 'false');
      tab.classList.toggle('on', state.sideTab === t[1]);
      tab.onclick = function () { setSideTab(t[1]); };
    });
    // 当前看的不是自选：页头给显式「加入自选」入口（查看只保证近期分析窗口）
    var track = el('trackBtn');
    if (track) {
      track.hidden = !state.code || isWatched(state.code);
      track.title = sessionDemo ? '加入自选（仅查看本地样本）' : '加入自选以补完整历史，并在离开页面后持续跟踪';
      track.onclick = function () { if (state.code) addWatch(state.code, nameOf(state.code), null); };
    }

    var formBox = el('wlForm');
    /* 表单只建一次：行情刷新重建会清空输入、夺走焦点。rail 版按钮是常驻首子，
       不能拿 firstChild 判空——按类名在子级里找已建表单 */
    var hasForm = Array.prototype.some.call(formBox.children, function (c) {
      return c.classList && c.classList.contains('wl-add');
    });
    if (!hasForm) {
      var form = document.createElement('form');
      form.className = 'wl-add';
      form.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></svg>' +
        '<input name="q" placeholder="代码 / 名称，回车查看" autocomplete="off" required>' +
        '<div class="wl-drop" hidden></div>';
      wireSearch(form);
      formBox.appendChild(form);
    }
    var wlCount = el('wlCount');
    if (wlCount) wlCount.textContent = String(list.length).padStart(2, '0');
    var wlCountRail = el('wlCountRail');
    if (wlCountRail) wlCountRail.textContent = String(list.length).padStart(2, '0');
    updateAiTitle();  // watchlist 晚于首次 load() 返回时修正标题
    if (lastF10 && lastF10.code === state.code) {
      renderF10Header();  // 同上，修正卡头名称
      renderTags();
    }
  }

  // ---------- 搜索（防抖 300ms → /api/search；选中候选即查看，每条候选另有「加自选」） ----------

  var searchTimer = null, searchRequest = 0;

  function wireSearch(form) {
    var input = form.q;
    var drop = form.querySelector('.wl-drop');
    var cands = [], activeIdx = -1;
    input.id = 'watchSearch';
    drop.id = 'watchSearchListbox';
    drop.setAttribute('role', 'listbox');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', drop.id);
    input.setAttribute('aria-label', '搜索股票');

    function hideDrop() {
      drop.hidden = true; drop.innerHTML = ''; cands = []; activeIdx = -1;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
    }

    function cancelSearch() {
      searchRequest += 1;
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = null;
      hideDrop();
    }

    function markActive() {
      Array.prototype.forEach.call(drop.children, function (div, i) {
        div.classList.toggle('active', i === activeIdx);
        div.setAttribute('aria-selected', i === activeIdx ? 'true' : 'false');
      });
      if (activeIdx >= 0 && drop.children[activeIdx]) {
        input.setAttribute('aria-activedescendant', drop.children[activeIdx].id);
      } else {
        input.removeAttribute('aria-activedescendant');
      }
    }

    function pick(i) {
      var c = cands[i];
      if (!c) return;
      cancelSearch();
      input.value = '';
      openCode(c.code, c.name);
    }

    function showCandidates(list) {
      drop.innerHTML = '';
      cands = list; activeIdx = -1;
      if (!list.length) {
        drop.innerHTML = '<div class="cand none">无匹配结果</div>';
        drop.hidden = false;
        input.setAttribute('aria-expanded', 'true');
        return;
      }
      list.forEach(function (c, i) {
        var div = document.createElement('div');
        div.className = 'cand';
        div.id = 'wl-cand-' + i;
        div.setAttribute('role', 'option');
        div.setAttribute('aria-selected', 'false');
        div.innerHTML = '<span class="c-name">' + esc(c.name) + '</span>' +
          '<span class="c-code">' + esc(c.code) + '</span>';
        div.onmousedown = function (ev) {  // mousedown 先于 input blur
          ev.preventDefault();
          pick(i);
        };
        // 显式加入自选：不打开图表；已在自选的只标注，不重复添加
        var watched = state.watchlist.some(function (w) { return w.code === c.code; });
        var add = document.createElement('button');
        add.type = 'button';
        add.className = 'c-add';
        add.tabIndex = -1;
        add.disabled = watched;
        add.textContent = watched ? '已自选' : '加自选';
        add.setAttribute('aria-label', watched ? c.name + ' 已在自选' : '加入自选 ' + c.name);
        add.onmousedown = function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          if (watched) return;
          cancelSearch();
          input.value = '';
          addWatch(c.code, c.name, null);
        };
        div.appendChild(add);
        drop.appendChild(div);
      });
      drop.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }

    input.oninput = function () {
      var q = input.value.trim();
      cancelSearch();
      if (!q) return;
      var request = searchRequest;
      searchTimer = setTimeout(function () {
        searchTimer = null;
        fetch('/api/search?q=' + encodeURIComponent(q))
          .then(function (r) {
            if (!r.ok) throw new Error('搜索失败 ' + r.status);
            return r.json();
          })
          .then(function (list) {
            if (request === searchRequest && input.value.trim() === q) showCandidates(list);
          })
          .catch(function (e) { if (request === searchRequest) setStatus(e.message); });
      }, 300);
    };

    input.onkeydown = function (ev) {
      if (ev.key === 'Escape') { cancelSearch(); input.blur(); return; }
      if (ev.key === 'Enter' && (ev.isComposing || ev.keyCode === 229)) return;
      if (drop.hidden || !cands.length) return;
      if (ev.key === 'ArrowDown') {
        ev.preventDefault();
        activeIdx = Math.min(activeIdx + 1, cands.length - 1);
        markActive();
      } else if (ev.key === 'ArrowUp') {
        ev.preventDefault();
        activeIdx = Math.max(activeIdx - 1, 0);
        markActive();
      } else if (ev.key === 'Enter') {
        ev.preventDefault();
        pick(activeIdx >= 0 ? activeIdx : 0);
      }
    };
    input.onblur = cancelSearch;
    form.onsubmit = function (ev) { ev.preventDefault(); };  // 查看/添加只走候选（点击/回车/加自选），不整表提交
  }

  // 点搜索区外部收起下拉
  document.addEventListener('mousedown', function (ev) {
    if (!ev.target.closest || !ev.target.closest('.wl-add')) {
      var drop = document.querySelector('.wl-drop');
      if (drop) { drop.hidden = true; drop.innerHTML = ''; }
    }
  });

  var watchlistQueue = Promise.resolve();
  function queueWatchlist(operation) {
    var result = watchlistQueue.then(operation);
    watchlistQueue = result.catch(function () {});
    return result;
  }

  function loadWatchlist(options) {
    var skipLoad = !!(options && options.skipLoad);
    return queueWatchlist(function () {
      return fetch('/api/watchlist')
        .then(function (r) { if (!r.ok) throw new Error('加载失败 ' + r.status); return r.json(); })
        .then(function (items) {
          state.watchlist = items;
          if (state.code) renderF10Header();   // 价格卡按是否自选选报价通道：列表一到就重绘，不等 F10
          var corrected = false;
          // 不在自选里的代码是合法的「查看」，保持不动；只有尚未选中任何代码时才落到自选首项
          if (!state.code && items.length) {
            state.code = items[0].code;
            corrected = true;
          }
          renderWatchlist();
          // 首屏若 URL 已预选 code，chart 已与本请求并发发出（skipLoad）；只有 code 被补选时才需重新 load
          // 首屏自动落到自选首项不是用户的选择，不记查看（否则每次打开页面都把它顶到最近查看最前）
          if (state.code && (!skipLoad || corrected)) load();
        });
    })
      .catch(function (e) { setStatus('自选股加载失败：' + e.message); });
  }

  // 左栏行情快照：静默失败（价格只是增强，失败保留旧值）
  function displayDataNote(j) {
    var notes = [], meta = j.meta || {};
    if (j.degraded && j.fetch_time) notes.push(hhmmss(j.fetch_time) + ' 缓存');
    if ((meta.sources || []).indexOf('baseline_backup') >= 0) notes.push('备用行情');
    if (meta.source_stale || meta.source_time_unknown) {
      notes.push(meta.source_stale && meta.source_ts ? '源时间较早：' + new Date(meta.source_ts * 1000).toLocaleString('zh-CN', {hour12:false}) : '源时间未确认');
    }
    if ((j.missing_codes || []).length || (meta.unavailable || []).length) notes.push('部分数据缺失');
    return notes.join(' · ');
  }

  // 行情按自选代码成表，与当前 code/freq/adjust 无关；只接纳最新一次请求的响应（逆序返回时较早的晚到不覆盖）
  var quotesSeq = 0;
  function loadQuotes() {
    var seq = ++quotesSeq;
    fetch('/api/quotes')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || seq !== quotesSeq) return;
        state.quotes = j.quotes || {};
        renderWatchlist();
        if (state.code) renderF10Header();  // 价格卡独立于 F10：F10 未到时报价也要显示
      })
      .catch(function () {});
  }

  // 正在看的非自选代码：单代码报价（自选由 loadQuotes 的整表覆盖）；只接纳最新一次请求的响应（逆序返回时
  // 较早的请求晚到不覆盖），且代码仍是当前代码
  var viewQuoteSeq = 0;
  function loadViewQuote(code) {
    if (!code || isWatched(code)) return;
    var seq = ++viewQuoteSeq;
    fetch('/api/quote?code=' + encodeURIComponent(code))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || seq !== viewQuoteSeq || code !== state.code) return;
        state.viewQuote = { code: code, quote: j.quote || null };
        renderF10Header();
      })
      .catch(function () {});
  }

  function addWatch(code, name, form) {
    return queueWatchlist(function () {
      return fetch('/api/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code, name: name }),
      }).then(function (r) {
        if (r.status === 409) throw new Error(code + ' 已在自选中');
        if (!r.ok) throw new Error('添加失败 ' + r.status);
        return r.json();
      }).then(function (items) {
        var selectFirst = !state.code && items.length;
        state.watchlist = items;
        if (selectFirst) state.code = items[0].code;
        if (form) form.reset();
        renderWatchlist();
        // 报价通道切到报价表：上次成员期间残留的这一项不可信，作废并立即刷新；表到之前价格卡沿用单代码报价
        if (state.quotes) delete state.quotes[code];
        if (state.code) renderF10Header();
        loadQuotes();
        if (selectFirst) { load(); recordView(items[0].code, items[0].name); }  // 无当前代码时加入即打开：记一次查看
        setStatus('');
      });
    }).catch(function (e) { setStatus(e.message); });
  }

  function removeWatch(code) {
    return queueWatchlist(function () {
      return fetch('/api/watchlist/' + encodeURIComponent(code), { method: 'DELETE' })
        .then(function (r) {
          if (!r.ok) throw new Error('删除失败 ' + r.status);
          return r.json();
        })
        .then(function (items) {
          // 移出自选退回搜索查看：当前图表保持不动（数据与查看记录都保留），只停止后台持续跟踪
          state.watchlist = items;
          renderWatchlist();
          // 报价通道切到单代码报价：报价表里的这一项不再刷新，作废；立即重绘并取单代码报价
          if (state.quotes) delete state.quotes[code];
          if (state.code) renderF10Header();
          if (state.code === code) loadViewQuote(code);
        });
    }).catch(function (e) { setStatus(e.message); });
  }

  // 用户显式打开一个代码（搜索选中、最近查看、自选行）：切图并记一次查看。
  // 自动刷新、整窗重载、周期/复权切换都走 load()，不经这里，不记查看。
  function openCode(code, name) {
    state.code = code;
    rememberRecent(code, name);
    renderWatchlist();
    load();
    recordView(code, name);
    if (sbMode() === 'open') setSidebar('rail');  /* 浮动展开选中后收回窄栏，露出图表 */
  }

  // 本页先把刚打开的代码放到最近查看首位，服务端回读后以服务端为准
  function rememberRecent(code, name) {
    var prev = null;
    state.recent = (state.recent || []).filter(function (r) {
      if (r.code === code) prev = r;
      return r.code !== code;
    });
    state.recent.unshift({ code: code, name: name || (prev && prev.name) || code,
      freq: state.freq, adjust: viewState.adjust, last_viewed_at: null });
  }

  // 查看记录是日志不是门槛：写入失败静默，不影响图表
  function recordView(code, name) {
    return fetch('/api/views', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: code, name: name || code, freq: state.freq, adjust: viewState.adjust }),
    }).then(function (r) { if (r.ok) return loadRecent(); })
      .catch(function () {});
  }

  function loadRecent() {
    return fetch('/api/views')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j) return;
        state.recent = j.recent || [];
        renderWatchlist();
      })
      .catch(function () {});
  }

  function setSideTab(tab) {
    if (tab !== 'watch' && tab !== 'recent') return;
    state.sideTab = tab;
    try { localStorage.setItem('chanapp-side-tab', tab); } catch (_) {}
    renderWatchlist();
    if (tab === 'recent') loadRecent();
  }

  function toggleStar(code) {
    return queueWatchlist(function () {
      return fetch('/api/watchlist/' + encodeURIComponent(code) + '/star', { method: 'POST' })
        .then(function (r) {
          if (!r.ok) throw new Error('置顶失败 ' + r.status);
          return r.json();
        })
        .then(function (items) {
          state.watchlist = items;
          renderWatchlist();
        });
    }).catch(function (e) { setStatus(e.message); });
  }

  // ---------- 展示周期偏好 ----------
  function periodAvailable(freq) {
    var market = /^hk/.test(state.code || '') ? 'hk' : 'cn';
    return !!periodPrefs && !!periodPrefs.markets[market] &&
      periodPrefs.markets[market].available.indexOf(freq) >= 0;
  }

  function periodEnabled(freq) {
    return !!periodPrefs && !periodPrefs.notice && periodPrefs.selected.indexOf(freq) >= 0 && periodAvailable(freq);
  }

  function clearPeriodView(message) {
    if (chartAbort) chartAbort.abort();
    chartAbort = null;
    Object.keys(analysisSlots).forEach(function (key) {
      if (analysisSlots[key].abort) analysisSlots[key].abort.abort();
      analysisSlots[key].abort = null;
    });
    analysisSlots = {};
    analysisIdentity = null;
    pendingAnalysis = null;
    closeAiPopup();
    el('aiPanel').innerHTML = '';
    historyState.loading = false;
    historyState.reqId++;
    historyState.hasMore = false;
    viewState.token = null;
    viewState.analysisTokens = null;
    chartEtag = null;
    loadedTarget = null;
    lastChartData = null;
    activeChartVersion = null;
    el('center').classList.add('period-empty');
    el('rail').classList.add('period-empty');
    el('resonance').innerHTML = '';
    el('metaBar').textContent = '';
    el('chartError').innerHTML = '';
    el('chartError').appendChild(document.createTextNode(message + ' '));
    var button = document.createElement('button');
    button.type = 'button';
    button.textContent = periodPrefs ? '选择展示周期' : '重试';
    button.onclick = function () { if (periodPrefs) openPeriodDialog(); else loadPeriodPrefs(); };
    el('chartError').appendChild(button);
    el('chartError').hidden = false;
    setStatus(message);
  }

  function openPeriodDialog() {
    if (!periodPrefs) { loadPeriodPrefs(); return; }
    var dialog = el('periodDialog'), market = /^hk/.test(state.code || '') ? 'hk' : 'cn';
    var capability = periodPrefs.markets[market];
    el('periodTitle').textContent = !periodPrefs.notice ? '展示周期' :
      periodPrefs.notice.kind === 'first_use' ? '选择展示周期' : '可用周期已变化';
    el('periodDescription').textContent = '勾选保存在此实例。当前查看市场：' + (market === 'hk' ? '港股' : 'A 股') + '。不可用项保留原勾选，恢复能力后自动启用。';
    var changes = (periodPrefs.notice && periodPrefs.notice.changes || []).map(function (change) {
      return (change.market === 'hk' ? '港股' : 'A 股') + ' ' + FREQ_NAME[change.freq] + (change.available ? '现已可用' : '现已不可用');
    });
    el('periodChanges').textContent = changes.join('；');
    el('periodOptions').innerHTML = '';
    periodPrefs.catalog.forEach(function (period) {
      var freq = period.freq;
      var label = document.createElement('label'), input = document.createElement('input');
      input.type = 'checkbox';
      input.value = freq;
      input.checked = periodPrefs.selected.indexOf(freq) >= 0;
      input.disabled = capability.available.indexOf(freq) < 0;
      label.className = 'period-option';
      label.appendChild(input);
      label.appendChild(document.createTextNode(period.label));
      if (input.disabled) {
        var reason = document.createElement('small');
        reason.textContent = capability.reasons[freq] || '当前市场不支持此周期';
        label.appendChild(reason);
      }
      el('periodOptions').appendChild(label);
    });
    el('periodError').textContent = '';
    el('periodCancel').hidden = !!periodPrefs.notice;
    if (!dialog.open) dialog.showModal();
  }

  function loadPeriodPrefs(options) {
    if (options && options.sync && periodSaving) return Promise.resolve(null);
    if (periodLoad) return periodLoad;
    var requestedRevision = periodPrefs && periodPrefs.revision;
    periodLoad = fetch('/api/periods', {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw new Error('周期偏好读取失败，请重试');
      return r.json();
    }).then(function (data) {
      // 保存前已发出的读请求不可在保存期间/之后覆盖新选择。
      if (options && options.sync && (periodSaving || (periodPrefs && periodPrefs.revision) !== requestedRevision)) return null;
      var previous = periodPrefs;
      var editing = el('periodDialog').open;
      var changed = !previous || previous.revision !== data.revision;
      if (!changed && options && options.sync) return false;
      periodPrefs = data;
      FREQ_NAME = Object.fromEntries(data.catalog.map(function (p) { return [p.freq, p.label]; }));
      if (previous && changed) clearPeriodView('展示周期已更新');
      renderTabs();
      if (data.notice || editing) {
        openPeriodDialog();
        if (changed && editing && !data.notice) el('periodError').textContent = '周期设置已更新，请重新确认。';
      }
      load();
      return changed;
    }).catch(function (error) {
      if (options && options.sync && (periodSaving || (periodPrefs && periodPrefs.revision) !== requestedRevision)) return null;
      periodPrefs = null;
      el('periodDialog').close();
      renderTabs();
      clearPeriodView('周期偏好读取失败，请重试');
      return null;
    })
      .finally(function () { periodLoad = null; });
    return periodLoad;
  }

  function savePeriodPrefs() {
    if (periodSaving) return;
    if (!periodPrefs) { loadPeriodPrefs(); return; }
    var selected = Array.from(el('periodOptions').querySelectorAll('input')).filter(function (input) {
      return input.checked;
    }).map(function (input) { return input.value; });
    periodSaving = true;
    el('periodSave').disabled = true;
    el('periodCancel').disabled = true;
    el('periodError').textContent = '';
    fetch('/api/periods', {method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({selected: selected, revision: periodPrefs.revision})
    }).then(function (r) {
      if (r.status === 409) {
        return loadPeriodPrefs().then(function (changed) {
          if (changed === null) return null;
          openPeriodDialog();
          el('periodError').textContent = '周期设置已变化，已读取最新设置，请重新确认。';
          return null;
        });
      }
      if (!r.ok) throw new Error('保存失败，请重试');
      return r.json();
    }).then(function (data) {
      if (!data) return;
      periodPrefs = data;
      FREQ_NAME = Object.fromEntries(data.catalog.map(function (p) { return [p.freq, p.label]; }));
      // 废弃正在返回的图表/分页/分析结果，取消的周期不可保留在旧画面上。
      clearPeriodView('正在应用展示周期');
      el('periodDialog').close();
      renderTabs();
      load();
    }).catch(function (error) { el('periodError').textContent = error.message; })
      .finally(function () {
        periodSaving = false;
        el('periodSave').disabled = false;
        el('periodCancel').disabled = false;
      });
  }

  function renderTabs() {
    var box = el('freqTabs');
    var catalog = (periodPrefs && periodPrefs.catalog) || [];
    var signature = JSON.stringify(catalog);
    if (box.dataset.catalog !== signature) {
      box.innerHTML = '';
      catalog.forEach(function (period) {
        var button = document.createElement('button');
        button.dataset.freq = period.freq;
        button.textContent = period.label;
        if (period.freq === 'week') button.title = '周线不参与共振/AI';
        box.appendChild(button);
      });
      box.dataset.catalog = signature;
    }
    var btns = box.querySelectorAll('button');
    btns.forEach(function (b) {
      b.hidden = !periodEnabled(b.dataset.freq);
      b.classList.toggle('active', b.dataset.freq === state.freq);
      b.onclick = function () { if (!periodEnabled(b.dataset.freq)) return; state.freq = b.dataset.freq; renderTabs(); load(); };
    });
    renderAdjust();
  }

  function isIndexCode(code) { return /^(sh000|sz399)/.test(code || ''); }

  // 前复权/不复权切换：指数恒为不复权，切换禁用并显示「不复权」
  function renderAdjust() {
    var index = isIndexCode(state.code);
    var box = el('adjustTabs');
    if (!box) return;                    // 旧版页面缓存没有该控件时不阻断启动
    box.title = index ? '指数恒为不复权' : '';
    box.querySelectorAll('button').forEach(function (b) {
      b.disabled = index;
      b.classList.toggle('active', b.dataset.adjust === (index ? 'raw' : viewState.adjust));
      b.onclick = function () { setAdjust(b.dataset.adjust); };
    });
  }

  function setAdjust(adjust) {
    if ((adjust !== 'qfq' && adjust !== 'raw') || adjust === viewState.adjust || isIndexCode(state.code)) return;
    viewState.adjust = adjust;
    try { localStorage.setItem('chanapp-adjust', adjust); } catch (_) {}
    renderAdjust();
    if (!state.code) return;
    // 同一时间轴只换价格口径：保留可视时间区间，便于对照
    restoreRange = charts ? {code: state.code, freq: state.freq, range: charts.main.timeScale().getVisibleRange()} : null;
    load();
  }

  // ---------- header 状态（常驻：交易时段 + 抓取时间；临时消息优先） ----------

  var statusMsg = null;   // setStatus 的临时消息，空串/null 回落到常驻状态
  var lastMeta = null;    // 最近一次 /api/chart 的 meta（抓取时间/新鲜度）

  // 新鲜度徽标（K 线数据契约「新鲜度」）：只看 meta.stale。stale → 金色「（缓存·HH:MM:SS）」，时刻取 fetch_time
  // （该数据集最近一次成功提交；取不到 HH:MM:SS 则「缓存·旧」）；否则无徽标。from_cache 只表示本次不是同步首取，不参与
  function cacheBadge(meta) {
    if (!meta || !meta.stale) return '';
    var t = /(\d{2}:\d{2}:\d{2})/.test(String(meta.fetch_time || '')) ? hhmmss(meta.fetch_time) : '旧';
    return '<span class="warn">（缓存·' + t + '）</span>';
  }

  function hhmmss(ts) {
    var m = String(ts || '').match(/(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : (ts || '');
  }

  function setStatus(msg) { statusMsg = msg || null; renderStatus(); }

  function showChartError(msg) {
    var e = el('chartError');
    e.innerHTML = '';
    e.appendChild(document.createTextNode('图表数据加载失败：' + msg + ' '));
    var retry = document.createElement('button');
    retry.type = 'button';
    retry.textContent = '重试';
    retry.onclick = function () { load(); };
    e.appendChild(retry);
    e.hidden = false;
  }

  function hideChartError() {
    el('chartError').hidden = true;
  }

  // 状态栏（目标 2026-09-29 第三阶段）：服务端在同一次读取里给出 coverage.data_status {phase, day, at}，只按本次
  // 图表快照渲染——「交易中 · 实时抓取」「已收盘 · 待定稿」「已收盘 · 历史抓取」加最后成功接纳时刻（MM-DD HH:MM），
  // 从未成功为「暂无数据」。没有该字段（旧门面）时沿用交易时段 + 抓取时间。
  var DATA_PHASE = { live: '交易中 · 实时抓取', awaiting_final: '已收盘 · 待定稿', final: '已收盘 · 历史抓取' };
  function renderStatus() {
    var s = el('status');
    if (statusMsg) { s.textContent = statusMsg; return; }
    if (!state.code) { s.textContent = ''; return; }
    var ds = lastMeta && lastMeta.coverage && lastMeta.coverage.data_status;
    var open = ds ? (ds.phase === 'live' || (ds.phase === 'none' && isSessionOpen(marketOf(state.code))))
      : isSessionOpen(marketOf(state.code));
    var html = '<span class="dot" style="color:' +
      (open ? 'var(--down)' : 'var(--faint)') + '">●</span> ';
    if (ds && ds.phase === 'historical') {
      html += '历史截止于 ' + esc(ds.day || '未知日期');
    } else if (ds) {
      html += ds.phase === 'none' || !ds.at ? (open ? '交易中' : '已收盘') + ' · 暂无数据'
        : esc(DATA_PHASE[ds.phase] + ' ' + String(ds.at).slice(5, 16));
    } else {
      html += open ? '交易中' : '已收盘';
      if (lastMeta && lastMeta.fetch_time) html += ' · 抓取 ' + hhmmss(lastMeta.fetch_time) + cacheBadge(lastMeta);
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

  // 成交量单位是股（所有市场、所有周期统一），图例换算为万股/亿股
  function fmtVol(v) {
    if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿股';
    if (v >= 1e4) return (v / 1e4).toFixed(2) + '万股';
    return Math.round(v) + '股';
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

  function renderLegend(bar, prev, idx) {
    if (!legendEl) return;
    if (!bar) { legendEl.textContent = ''; return; }
    var pct = '', pctCls = 'dim';
    if (prev && prev.close) {
      var chg = (bar.close - prev.close) / prev.close * 100;
      pct = (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%';
      pctCls = chg >= 0 ? 'up' : 'down';  // 涨红跌青
    }
    // 十字线 bar 至今：间隔周期数（最新一根为 0）+ 收盘→最新收盘涨跌幅
    var since = '';
    if (idx != null && klineData.length && bar.close) {
      var last = klineData[klineData.length - 1];
      var sinceChg = (last.close - bar.close) / bar.close * 100;
      since = ' <span class="dim">至今' + (klineData.length - 1 - idx) + '期</span>' +
        ' <span class="' + (sinceChg >= 0 ? 'up' : 'down') + '">' +
        (sinceChg >= 0 ? '+' : '') + sinceChg.toFixed(2) + '%</span>';
    }
    legendEl.innerHTML =
      '<span class="dim">' + fmtBarTime(bar.time) + '</span>' +
      ' 开<b>' + bar.open.toFixed(2) + '</b>' +
      ' 高<b>' + bar.high.toFixed(2) + '</b>' +
      ' 低<b>' + bar.low.toFixed(2) + '</b>' +
      ' 收<b>' + bar.close.toFixed(2) + '</b>' +
      ' <span class="' + pctCls + '">' + pct + '</span>' +
      ' <span class="dim">量' + fmtVol(bar.volume) + '</span>' + since;
  }

  function legendLatest() {
    var n = klineData.length;
    renderLegend(n ? klineData[n - 1] : null, n > 1 ? klineData[n - 2] : null, n - 1);
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
      rightPriceScale: { borderColor: P.border, minimumWidth: 72 },
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
    if (typeof onMainRangeChanged === 'function') {
      main.timeScale().subscribeVisibleLogicalRangeChange(onMainRangeChanged);
    }

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
    // forming 笔/段互不相交，逐条独立 series（renderChart 里按数据重建），
    // 不能像确认结构那样拼成单条折线——否则不相交的段之间会被直线连接。

    var zsOverlay = makeZsOverlay(el('chart'), main, candleSeries);

    // 十字线主副图联动：指针所在图（源）的十字线停在某根 bar，另一图在同一时间画竖线，图例与副图读数跟随该 bar；
    // 移出回落到最新一根。跟随一侧关掉横线与价格标签（那里的纵轴不是同一量纲）。
    // 库的程序化 setCrosshairPosition/clearCrosshairPosition 与 applyOptions 都会异步回发 crosshairMove，
    // 只认指针所在图的事件，跟随图的回声一律忽略，否则两图互相设置无限往返。
    // charts 只建一次，订阅一次即可；数据经 klineData/klineByTime 按 load 更新。
    // 指针在哪张图由 wireChartPointer 跟踪（crossPointer）。
    legendEl = el('ohlc');
    function followCrosshair(src, dst, param, place) {
      if (crossPointer !== src) return;
      var hit = param.time != null ? klineByTime[param.time] : null;
      if (!hit) { crossRest(dst); return; }
      place(hit);
      renderLegend(hit.bar, hit.prev, hit.i);
      renderSubReadout(hit.i);
    }
    main.subscribeCrosshairMove(function (param) {
      followCrosshair(main, macdChart, param, function (hit) {
        if (!subSeries.length) { macdChart.clearCrosshairPosition(); return; }
        // 竖线只看时间；价格取该根第一条线的值，无值（whitespace）时给 0，横线已关
        var v = subRead && subRead.rows.length && subRead.rows[0].values[hit.i];
        macdChart.setCrosshairPosition(v == null ? 0 : v, param.time, subSeries[0]);
      });
    });
    macdChart.subscribeCrosshairMove(function (param) {
      followCrosshair(macdChart, main, param, function (hit) {
        main.setCrosshairPosition(hit.bar.close, param.time, candleSeries);
      });
    });

    var markers = LW.createSeriesMarkers(candleSeries, []);

    // 同步时间轴（双向，防回环）；showInd 重建副图 series 期间（subRebuild）库会
    // 触发 时间轴 null→自动适配 瞬变，必须抑制同步，否则主图可视区间被拖走。
    // 宽度变化（窗口缩放、侧栏/布局过渡）引起的区间变化不外传：两图同宽，各自保持 bar 间距
    // 重排后区间自然一致；若互相回灌，先缩完的一侧会把对侧还没跟上的旧区间抄回来，
    // 间距越推越大，最后只剩几根巨大 K 线。
    var syncing = false;
    function sync(src, dst) {
      var ts = src.timeScale(), width = ts.width();
      ts.subscribeVisibleLogicalRangeChange(function (range) {
        var resized = ts.width() !== width;
        width = ts.width();
        if (syncing || subRebuild || !range || resized) return;
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
      formingBiSegs: [], formingXdSegs: [],
      zsOverlay: zsOverlay, markers: markers, channelSeries: [],
    };
    return charts;
  }

  // 十字线联动的源图：指针所在的那张图（charts.main / charts.macdChart），不在图上为 null
  var crossPointer = null;

  function crossHorz(chart, on) {
    chart.applyOptions({ crosshair: { horzLine: { visible: on, labelVisible: on } } });
  }

  // 源图十字线离开 bar 或指针移出：跟随图清十字线，图例与副图读数回到最新一根
  function crossRest(dst) {
    dst.clearCrosshairPosition();
    legendLatest();
    renderSubReadout(null);
  }

  // 指针进入哪张图，哪张图就是联动源：源图显示横线与价格标签，跟随图关掉
  function wireChartPointer() {
    [['chart', 'main', 'macdChart'], ['sub', 'macdChart', 'main']].forEach(function (pair) {
      var node = el(pair[0]);
      node.addEventListener('pointerenter', function () {
        if (!charts || crossPointer === charts[pair[1]]) return;
        crossPointer = charts[pair[1]];
        crossHorz(charts[pair[1]], true);
        crossHorz(charts[pair[2]], false);
      });
      node.addEventListener('pointerleave', function () {
        if (!charts || crossPointer !== charts[pair[1]]) return;
        crossPointer = null;
        crossRest(charts[pair[2]]);
      });
    });
  }

  // ---------- 日间/夜间主题 ----------

  function applyTheme(mode) {
    theme = mode;
    document.documentElement.dataset.theme = mode;
    P = palette(mode);
    localStorage.setItem('chanapp-theme', mode);
    if (!charts) return;
    charts.main.applyOptions(chartOpts());
    charts.macdChart.applyOptions(chartOpts());
    charts.candleSeries.applyOptions({ upColor: P.up, downColor: P.down, wickUpColor: P.up, wickDownColor: P.down });
    charts.biSeries.applyOptions({ color: P.bi });
    charts.xdSeries.applyOptions({ color: P.gold });
    charts.formingBiSegs.forEach(function (s) { s.applyOptions({ color: P.biForming }); });
    charts.formingXdSegs.forEach(function (s) { s.applyOptions({ color: P.goldA }); });
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
        s += closes[i];
        if (i >= n) s -= closes[i - n];
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
  var subRebuild = false;  // showInd 重建副图期间抑制主副图时间轴同步（ensureCharts 的 sync 检查）
  // 副图读数：指标名 · 各线数值 · 说明。数值随十字线所在 bar，移出回到最新一根
  var SUB_HEAD = { macd: 'MACD', kdj: 'KDJ(9,3,3)', rsi: 'RSI(14)', boll: 'BOLL(20,2)' };
  var SUB_TAIL = { macd: 'hist=2×(DIF−DEA)', kdj: '显示用', rsi: '显示用', boll: '显示用' };
  var subRead = null;  // { name, rows: [{label, color, values(与 klineData 逐根对齐), digits}], short }

  function clearSub() {
    if (!charts) return;
    subSeries.forEach(function (s) { charts.macdChart.removeSeries(s); });
    subSeries = [];
  }

  function subLine(color, w) {
    return { color: color, lineWidth: w || 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
  }

  function lastVal(arr) { for (var i = arr.length - 1; i >= 0; i--) if (arr[i] != null) return arr[i]; }

  // i 为 klineData 下标；null/越界表示「未悬停」，取最新一根。
  // 某根无值（预热期、翻页进来而后端未给 MACD 的历史段）显示 —；BOLL 全段不足 20 根时整体标样本不足、缺值记 --
  function renderSubReadout(i) {
    var note = el('subNote');
    if (!note || !subRead) return;
    var n = klineData.length;
    if (i == null || i < 0 || i >= n) i = n - 1;
    var miss = subRead.short ? '--' : '—';
    var parts = [SUB_HEAD[subRead.name] + (subRead.short ? ' · 样本不足（至少 20 根）' : '')];
    subRead.rows.forEach(function (row) {
      var v = i >= 0 ? row.values[i] : null;
      var color = typeof row.color === 'function' ? row.color(v) : row.color;
      // 去掉运算末位的二进制噪声，再按显示精度舍入；如 884.9549999999999 应显示 884.96。
      var text = v == null ? miss : Number(v.toPrecision(15)).toFixed(row.digits);
      parts.push(row.label + ' <b style="color:' + color + '">' + text + '</b>');
    });
    parts.push(SUB_TAIL[subRead.name]);
    note.innerHTML = parts.join(' · ');
  }

  function subReadRows(name) {
    var col = function (key) { return klineData.map(function (b) { return b[key]; }); };
    if (name === 'macd') {
      var macdAt = {};
      macdRowsRaw.forEach(function (m) { macdAt[toTime(m.time)] = m; });
      var pick = function (key) {
        return klineData.map(function (b) { var m = macdAt[toTime(b.time)]; return m && m[key] != null ? m[key] : null; });
      };
      return [
        { label: 'DIF', color: P.gold, values: pick('dif'), digits: 3 },
        { label: 'DEA', color: P.bi, values: pick('dea'), digits: 3 },
        { label: 'MACD', color: function (v) { return v != null && v < 0 ? P.down : P.up; }, values: pick('hist'), digits: 3 },
      ];
    }
    if (name === 'kdj') {
      var kdj = chanIndicators.kdj(klineData);
      return [
        { label: 'K', color: P.gold, values: kdj.k, digits: 2 },
        { label: 'D', color: P.bi, values: kdj.d, digits: 2 },
        { label: 'J', color: P.up, values: kdj.j, digits: 2 },
      ];
    }
    if (name === 'rsi') return [{ label: 'RSI', color: P.gold, values: chanIndicators.rsi(col('close'), 14), digits: 2 }];
    var boll = chanIndicators.boll(col('close'), 20, 2);
    return [
      { label: 'MID', color: P.gold, values: boll.mid, digits: 2 },
      { label: 'UP', color: P.bi, values: boll.up, digits: 2 },
      { label: 'LOW', color: P.bi, values: boll.lo, digits: 2 },
      { label: 'C', color: P.up, values: col('close'), digits: 2 },
    ];
  }

  function showInd(name) {
    curInd = name;
    document.querySelectorAll('#subSeg [data-ind]').forEach(function (b) {
      b.classList.toggle('active', b.dataset.ind === name);
    });
    var cur = el('subSegCur');
    if (cur) cur.textContent = name.toUpperCase();
    if (!charts) return;
    var LW = LightweightCharts, sub = charts.macdChart;
    // 清空副图会让库的时间轴经历 null→自动适配 瞬变：记录原区间，重建后恢复，期间抑制同步
    subRebuild = true;
    var keepRange = null;
    try {
      keepRange = sub.timeScale().getVisibleLogicalRange();
      clearSub();
      // 读数与副图 series 共用同一份逐根数值
      var rows = subReadRows(name);
      subRead = { name: name, rows: rows, short: name === 'boll' && lastVal(rows[0].values) == null };
      renderSubReadout(null);
      if (!klineData.length) return;
      // 主副图按逻辑索引同步，副图每条 series 必须逐根铺满主图时间轴：
      // 无值处（指标预热期、左拉得到而后端未给 MACD 的历史段）放只有 time 的 whitespace 点
      var toPt = function (arr, round, color) {
        return klineData.map(function (b, i) {
          var t = toTime(b.time), v = arr[i];
          if (v == null) return { time: t };
          var pt = { time: t, value: round ? +v.toFixed(4) : v };
          if (color) pt.color = color(v);
          return pt;
        });
      };
      var line = function (color, values, extra) {
        var s = sub.addSeries(LW.LineSeries, Object.assign(subLine(color), extra || {}));
        s.setData(toPt(values, name !== 'macd'));
        return s;
      };
      if (name === 'macd') {
        var h = sub.addSeries(LW.HistogramSeries, { priceLineVisible: false, lastValueVisible: false });
        h.setData(toPt(rows[2].values, false, function (v) { return v >= 0 ? P.upA : P.downA; }));
        subSeries = [h, line(P.gold, rows[0].values), line(P.bi, rows[1].values)];
      } else if (name === 'kdj') {
        subSeries = rows.map(function (row) { return line(row.color, row.values); });
      } else if (name === 'rsi') {
        subSeries = [line(P.gold, rows[0].values)];
      } else if (name === 'boll') {
        // 收盘价线让轨道有参照；线序沿用 UP、MID（虚线）、LOW、C
        subSeries = [line(P.bi, rows[1].values), line(P.gold, rows[0].values, { lineStyle: LW.LineStyle.Dashed }),
          line(P.bi, rows[2].values), line(P.up, rows[3].values)];
      }
    } finally {
      try {
        if (keepRange) sub.timeScale().setVisibleLogicalRange(keepRange);
      } finally {
        subRebuild = false;
      }
    }
  }

  function wireSubSeg() {
    var wrap = el('subSeg'), btn = el('subSegBtn'), pop = el('subSegPop');
    function setOpen(open) {
      pop.hidden = !open;
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    btn.addEventListener('click', function () { setOpen(pop.hidden); });
    pop.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      showInd(b.dataset.ind);
      setOpen(false);
    });
    document.addEventListener('click', function (e) {
      if (!pop.hidden && !wrap.contains(e.target)) setOpen(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !pop.hidden) {
        setOpen(false); btn.focus();
        e.stopImmediatePropagation();  // 已消费 Esc：下层浮层（aiPop/rulesPop）不再同键连关
      }
    });
  }

  // ---------- 多级别共振角标 ----------

  var FREQ_NAME = Object.fromEntries(((periodPrefs && periodPrefs.catalog) || []).map(function (p) { return [p.freq, p.label]; }));
  // 力度算法上游口径的中文名（详情卡多处展示，统一走这一份）
  var MACD_ALGO_NAME = { peak: 'MACD同向柱峰值', slope: '价格变化斜率' };

  function renderResonance(list, freqs) {
    var box = el('resonance');
    box.innerHTML = '';
    (freqs || (list || []).map(function (item) { return item.freq; })).forEach(function (freq) {
      var found = (list || []).find(function (item) { return item.freq === freq; });
      var lv = found || { freq: freq };
      var signals = lv.signals || [], details = [], forming = false;
      var html = signals.map(function (signal) {
        var label = (signal.level === 'seg' ? '' : '笔 ') + signalLabel(signal);
        var time = fmtBarTime(signal.dt);
        details.push(label + ' ' + time);
        forming = forming || signal.status === 'provisional';
        // 时间单独成块：空间不够时整块让位（CSS 换行裁掉），说明文字再省略
        return '<span class="res-sig sig-' + (signal.side === 'buy' ? 'b' : 's') +
          (signal.status === 'provisional' ? ' prov' : '') + '">' +
          esc(label.replace(' · 形成中', '')) + '</span><span class="res-time">' + esc(time) + '</span>';
      }).join('<span class="res-sep">/</span>');
      if (!signals.length) {
        var text = lv.zs ? (lv.zs.inside ? '中枢内 ' : '中枢外 ') + lv.zs.zd + '–' + lv.zs.zg : (found ? '暂无点位' : '摘要不可用');
        details.push(text);
        html = '<span class="res-sig ' + (lv.zs ? 'zs' : 'res-quiet') + '">' + esc(text) + '</span>';
      }
      var chip = document.createElement('span');
      chip.className = 'res-chip' + (signals.length ? ' has-signal' : '');
      chip.dataset.freq = freq;
      chip.title = FREQ_NAME[freq] + '：' + details.join(' / ') + (periodEnabled(freq) ? '（点击切换周期）' : '（分析必需；展示未勾选）');
      chip.innerHTML = '<span class="lv">' + FREQ_NAME[freq] + '</span><span class="res-detail">' + html + '</span>' +
        (forming ? '<span class="res-state">形成中</span>' : '');
      if (signals.length) {
        // 尾部详情钮：吃住在 chip 内但点击不冒泡（不触发切周期），开该周期最新信号详情
        var detailBtn = document.createElement('button');
        detailBtn.className = 'res-detail-btn';
        detailBtn.type = 'button';
        detailBtn.textContent = '›';
        detailBtn.setAttribute('aria-label', FREQ_NAME[freq] + '信号详情');
        detailBtn.title = '查看该周期信号详情';
        detailBtn.onclick = function (ev) {
          ev.stopPropagation();
          openDetail('bs', lv, detailBtn);
        };
        chip.appendChild(detailBtn);
      }
      chip.onclick = function () {
        if (!periodEnabled(freq) || state.freq === freq) return;
        state.freq = freq;
        renderTabs();
        load();
      };
      box.appendChild(chip);
    });
    // 周线只作查看，不参与共振与 AI。
    if (state.freq === 'week') {
      var note = document.createElement('span');
      note.className = 'res-note';
      note.textContent = '周线不参与共振/AI';
      box.appendChild(note);
    }
  }

  // ---------- 原生信号标注 ----------
  function signalLabel(s) {
    var prefix = s.side === 'buy' ? 'B' : 'S';
    return (s.level === 'seg' ? '段 ' : '') +
      (s.types || []).map(function (t) { return prefix + t; }).join('/') +
      (s.status === 'provisional' ? ' · 形成中' : '');
  }

  // 图上标记用短文本（~ 前缀表形成中），长文案留给共振条和依据卡
  function signalMarkerText(s) {
    var prefix = s.side === 'buy' ? 'B' : 'S';
    return (s.level === 'seg' ? '段 ' : '') + (s.status === 'provisional' ? '~' : '') +
      (s.types || []).map(function (t) { return prefix + t; }).join('/');
  }

  function signalColor(s) {
    var color = s.level === 'seg' ? P.gold : (s.side === 'buy' ? P.up : P.down);
    return s.status === 'provisional' ? hexA(color, 0.45) : color;
  }

  function buildMarkers(signals) {
    return (signals || []).map(function (s) {
      var buy = s.side === 'buy';
      return {
        time: toTime(s.dt),
        position: buy ? 'belowBar' : 'aboveBar',
        shape: buy ? 'arrowUp' : 'arrowDown',
        color: signalColor(s),
        text: signalMarkerText(s),
        size: s.level === 'seg' ? 2 : 1,
      };
    }).sort(function (a, b) { return a.time < b.time ? -1 : a.time > b.time ? 1 : 0; });
  }

  // ---------- 依据卡 ----------

  // 依据卡点击定位：目标 bar 在当前跨度内居中，越界收敛（+3 与默认右留白一致）
  function locateEvidenceRange(i, len, span) {
    var from = Math.max(0, Math.min(i - Math.floor(span / 2), len - 1 + 3 - span));
    return { from: from, to: from + span };
  }

  function locateBar(dt) {
    if (!charts || !charts.main || !klineData.length) return;
    var rec = klineByTime[toTime(dt)];
    if (!rec) return;
    var lr = charts.main.timeScale().getVisibleLogicalRange();
    var span = lr ? Math.max(20, lr.to - lr.from) : 60;
    var range = locateEvidenceRange(rec.i, klineData.length, span);
    charts.main.timeScale().setVisibleLogicalRange(range);
    charts.macdChart.timeScale().setVisibleLogicalRange(range);
    charts.main.setCrosshairPosition(rec.bar.close, toTime(dt), charts.candleSeries);
    renderLegend(rec.bar, rec.prev, rec.i);  // 程序化十字线的回声事件被联动忽略（只认指针所在图），图例手动同步
    if (locateBar.timer) clearTimeout(locateBar.timer);
    locateBar.timer = setTimeout(function () {
      charts.main.clearCrosshairPosition();
      legendLatest();
    }, 1600);
  }

  function renderEvidence(evidence, options) {
    var selected = options && options.preserve && evidenceList[evidenceIdx];
    evidenceList = evidence.slice().sort(function (a, b) { return a.dt < b.dt ? 1 : -1; });
    evidenceIdx = 0;
    if (selected) {
      var index = evidenceList.findIndex(function (c) {
        return c.dt === selected.dt && c.side === selected.side && c.level === selected.level &&
          JSON.stringify(c.types) === JSON.stringify(selected.types);
      });
      if (index >= 0) evidenceIdx = index;
    }
    if (!evidenceList.length) {
      el('cardsDock').hidden = true;
      el('cards').innerHTML = '';
      el('cardsCount').textContent = '';
      el('cardsPrev').disabled = true;
      el('cardsNext').disabled = true;
      return;
    }
    el('cardsDock').hidden = false;
    showEvidence(evidenceIdx);
  }

  // 依据卡浮层状态：dt 降序全量与当前展示条序号
  var evidenceList = [], evidenceIdx = 0;

  // 渲染当前条并同步计数与左右切换键；越界序号忽略
  function showEvidence(i) {
    if (i < 0 || i >= evidenceList.length) return;
    evidenceIdx = i;
    var c = evidenceList[i];
    var box = el('cards');
    var hadFocus = box.contains(document.activeElement);
    box.innerHTML = '';
    var div = document.createElement('div');
    div.className = 'card';
    div.dataset.dt = c.dt;
    div.tabIndex = 0;
    div.setAttribute('role', 'button');
    div.title = '点击定位到 K 线';
    div.onclick = function () { locateBar(c.dt); };
    div.onkeydown = function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); locateBar(c.dt); }
    };
    var sideCls = c.side === 'buy' ? 'lbl-buy' : 'lbl-sell';
    div.innerHTML =
      '<div class="head"><span class="' + sideCls + '">' + esc(signalLabel(c)) + '</span>' +
      '<span class="price">' + c.price.toFixed(2) + '</span></div>' +
      '<div class="text">' + fmtBarTime(c.dt) + '</div>' +
      '<div class="text">' + c.text + '</div>';
    box.appendChild(div);
    if (hadFocus) div.focus({preventScroll: true});
    el('cardsCount').textContent = (i + 1) + '/' + evidenceList.length;
    el('cardsPrev').disabled = i <= 0;
    el('cardsNext').disabled = i >= evidenceList.length - 1;
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

  // noteMode: null（真实结果）| 'unconfigured'（未配置 LLM，渲染静态样例）
  function renderAnalysis(j, noteMode) {
    var box = el('aiPanel');
    if (noteMode) pendingAnalysis = null;
    if (typeof closeAiPopup === 'function') closeAiPopup();  // 任何重渲染都关掉情景弹层，避免悬留旧内容
    el('aiDataStatus').hidden = true;
    el('aiSampleBadge').hidden = !noteMode;  // 静态样例身份常驻面板头部，不随滚动离开
    var html = '';
    if (!noteMode && j.analysis_scope === 'multi_timeframe') {
      html += '<div class="ai-note">分析级别 · ' + (j.freqs || []).map(function (f) { return esc(FREQ_NAME[f] || f); }).join(' / ') + '</div>';
    }
    if (noteMode === 'unconfigured') {
      html += '<div class="ai-note">未配置 LLM，仅显示依据卡（以下为静态样例）</div>';
    } else if (j.cached) {
      html += '<div class="ai-note">缓存结果（' +
        (j.analysis_scope === 'multi_timeframe' ? '分析输入未变化' : (j.ref_bar_dt ? fmtRefDt(j.ref_bar_dt) + '后结构未变化' : '结构未变化')) +
        '）</div>';
    }
    if (j.current_state) {
      html += '<div class="ai-state">' + esc(j.current_state) + '</div>';
    }
    if (j.raw) {  // LLM 输出解析失败：展示原文
      html += '<div class="card"><div class="kv">' + esc(j.raw) + '</div></div>';
    }
    // 后验·低 情景不展示（噪声档位，口径同 posteriorBadge 的首字提取）
    var scenarios = (j.scenarios || []).filter(function (s) {
      return String(s.posterior || '').charAt(0) !== '低';
    }).slice(0, 3);
    aiScenarios = scenarios;
    // 情景折叠为标题行：点击（初始化区事件委托）弹玻璃浮层，全文仍由 renderScenario 渲染
    scenarios.forEach(function (s, i) {
      html += '<div class="ai-row" data-i="' + i + '" role="button" tabindex="0"><span>' +
        esc(s.name) + '</span>' + posteriorBadge(s.posterior) + '</div>';
    });
    if (!j.current_state && !scenarios.length && !j.raw) {
      html += '<div class="card"><span class="text">暂无完全分类结果</span></div>';
    }
    box.innerHTML = html;
  }

  // 当前面板的情景数据：标题行 data-i 索引对应，弹层据此渲染全文
  var aiScenarios = [];

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

  // 详情副题：标的名 + 行内说明（AI 情景/信号详情共用）
  function detailSubtitle(extra) {
    var w = null;
    state.watchlist.forEach(function (x) { if (x.code === state.code) w = x; });
    return '<p class="detail-subtitle">' + esc(w ? w.name : state.code) + ' · ' + extra + '<br>' +
      esc(ruleLabelText()) + '</p>';
  }

  // 情景详情渲染为覆盖层分块排版：h2 标题 + 副题 + detail-block 逐块；新字段缺失（旧格式缓存）时不渲染
  function renderScenario(s) {
    function block(label, value, cls) {
      var inner = kv(label, value, cls);
      return inner ? '<div class="detail-block">' + inner + '</div>' : '';
    }
    function blockList(label, items, cls) {
      var inner = kvList(label, items, cls);
      return inner ? '<div class="detail-block">' + inner + '</div>' : '';
    }
    return '<h2>' + esc(s.name) + ' ' + posteriorBadge(s.posterior) + '</h2>' +
      detailSubtitle('AI 完全分类') +
      block('先验：', s.prior) +
      blockList('支持：', s.evidence_for, 'ev-for') +
      blockList('削弱：', s.evidence_against, 'ev-against') +
      block('后验：', s.posterior) +
      block('触发：', s.trigger) +
      block('边界：', s.boundary) +
      block('应对：', s.action) +
      block('依据：', s.basis) +
      block('更新观察：', s.update_watch);
  }

  // 共振 chip 信号详情（lv = 该周期共振项）：label/时间/形成中/识别依据（字段齐全时逐条）+ 中枢信息
  function renderSignalDetail(lv) {
    var freq = lv.freq, signals = lv.signals || [];
    /* signals 按上游枚举序排列（level: bi 在前 seg 在后），不按时间排序：
       尾钮语义是「该周期最新信号详情」，这里显式按 dt 取最新，不依赖 signals[0] 恰好最新 */
    var s = null;
    signals.forEach(function (x) { if (!s || x.dt > s.dt) s = x; });
    if (!s) {
      var text = lv.zs ? (lv.zs.inside ? '中枢内 ' : '中枢外 ') + lv.zs.zd + '–' + lv.zs.zg : '暂无点位';
      return '<h2>' + FREQ_NAME[freq] + '</h2>' + detailSubtitle(FREQ_NAME[freq] + '信号') +
        '<div class="detail-block"><div class="kv"><b>当前状态：</b>' + esc(text) + '</div></div>';
    }
    var forming = s.forming || s.status === 'provisional';
    var dt = s.detail || {};
    var lsp = (freq === state.freq) ? (dt.last_sure_pos != null ? dt.last_sure_pos : s.last_sure_pos) : null;
    var sure = (lsp != null && klineData[lsp]) ? klineData[lsp].time : null;
    var str = dt.strength || s.strength;
    var metricName = MACD_ALGO_NAME[s.macd_algo || dt.macd_algo];
    var feats = dt.features || s.features;
    var featText = feats ? Object.keys(feats).map(function (k) { return k + '=' + (typeof feats[k] === 'number' ? +feats[k].toFixed(4) : feats[k]); }).join('，') : null;
    var rel = dt.related_bsp1 || s.related_bsp1;
    var relText = rel ? fmtBarTime(rel.dt) + ' @ ' + (rel.price != null ? rel.price.toFixed(2) : rel.price) : null;
    var ctx = dt.context || s.context;
    var ref = dt.structure_ref || s.structure_ref;
    var refText = ref ? (ref.level === 'seg' ? '段' : '笔') + ' #' + ref.index : null;
    var zsText = lv.zs ? (lv.zs.inside ? '中枢内 ' : '中枢外 ') + lv.zs.zd + '–' + lv.zs.zg : null;
    return '<h2>' + esc(signalLabel(s).replace(' · 形成中', '')) + ' ' +
      (forming ? '<b class="badge mid">形成中</b>' : '<b class="badge hi">已确认</b>') + '</h2>' +
      detailSubtitle(FREQ_NAME[freq] + '信号') +
      '<div class="detail-block"><div class="kv"><b>当前状态：</b>' +
      esc(s.text || (forming ? '候选点位仍可能随新 K 线移动或消失。' : '已确认信号。')) + '</div></div>' +
      '<div class="detail-block">' +
      kv('信号时间：', fmtBarTime(s.dt)) +
      kv('点位：', s.price != null ? s.price.toFixed(2) : null) +
      kv('最近确认 K 线：', sure ? fmtBarTime(sure) : null) +
      kv('力度算法：', metricName || s.macd_algo || dt.macd_algo) +
      kv('关联一类点：', relText) +
      kv('结构引用：', refText) +
      (feats ? kv('特征：', featText) : '') +
      (ctx && ctx.zs_count != null ? kv('父段中枢数：', String(ctx.zs_count)) : '') +
      (ctx && ctx.origin === 'zero_center' ? kv('扩展来源：', ctx.origin_source === 'self' ? '自身为无中枢一类点' : '关联无中枢一类点') : '') +
      '</div>' +
      (str && str.value != null ? '<div class="detail-block"><h3>力度</h3>' +
        kv('指标：', MACD_ALGO_NAME[str.metric] || str.metric) +
        kv('数值：', +str.value.toFixed(4) + '') +
        kv('状态：', {weaker: '减弱', equal: '持平', stronger: '增强'}[str.state] || str.state) +
        '</div>' : '') +
      (zsText ? '<div class="detail-block"><h3>中枢</h3><div class="kv"><b>位置：</b>' + esc(zsText) + '</div></div>' : '');
  }

  // 依据卡详情：当前卡 evidence 的完整字段
  function renderEvidenceDetail(c) {
    if (!c) return '';
    var forming = c.forming || c.status === 'provisional';
    var dt = c.detail || {};
    var rel = dt.related_bsp1;
    var relText = rel ? fmtBarTime(rel.dt) + ' @ ' + (rel.price != null ? rel.price.toFixed(2) : rel.price) : null;
    var ctx = dt.context;
    var ref = dt.structure_ref;
    var refText = ref ? (ref.level === 'seg' ? '段' : '笔') + ' #' + ref.index : null;
    var str = dt.strength;
    var feats = dt.features;
    var featText = feats ? Object.keys(feats).map(function (k) { return k + '=' + (typeof feats[k] === 'number' ? +feats[k].toFixed(4) : feats[k]); }).join('，') : null;
    return '<h2>' + esc(signalLabel(c).replace(' · 形成中', '')) + ' ' +
      (forming ? '<b class="badge mid">形成中</b>' : '<b class="badge hi">已确认</b>') + '</h2>' +
      detailSubtitle('信号依据卡') +
      '<div class="detail-block"><h3>当前状态</h3><div class="kv">' + esc(c.text || '') + '</div></div>' +
      '<div class="detail-block"><h3>时间口径</h3>' +
      kv('信号时间：', fmtBarTime(c.dt)) +
      kv('点位：', c.price != null ? c.price.toFixed(2) : null) +
      kv('力度算法：', MACD_ALGO_NAME[dt.macd_algo] || dt.macd_algo) +
      kv('最近确认 K 线位置：', dt.last_sure_pos != null ? String(dt.last_sure_pos) : null) +
      '</div>' +
      '<div class="detail-block"><h3>结构字段</h3>' +
      kv('结构引用：', refText) +
      kv('关联一类点：', relText) +
      (feats ? kv('特征：', featText) : '') +
      (ctx && ctx.zs_count != null ? kv('父段中枢数：', String(ctx.zs_count)) : '') +
      (ctx && ctx.origin === 'zero_center' ? kv('扩展来源：', ctx.origin_source === 'self' ? '自身为无中枢一类点' : '关联无中枢一类点') : '') +
      (str ? kv('力度：', (str.value != null ? +str.value.toFixed(4) : '不可用') +
        (str.state && str.state !== 'unavailable' ? '（' + ({weaker: '减弱', equal: '持平', stronger: '增强'}[str.state] || str.state) + '）' : '') +
        (str.metric ? ' · ' + (MACD_ALGO_NAME[str.metric] || str.metric) : '')) : '') +
      '</div>';
  }

  // 非模态详情为右栏滑入覆盖层：覆盖右栏期间将下层兄弟设为 inert，关闭后焦点归还触发控件
  var aiPopupTrigger = null, railSavedScroll = 0, detailKind = null;
  function openDetail(kind, payload, trigger) {
    /* 切换/刷新请求期间仍保留旧图表供对照，但不可用新选择解释旧信号。
       activeChartVersion 只在当前请求成功提交后存在，同时保证位置索引对应已加载 K 线。 */
    if ((kind === 'card' || kind === 'bs') && (!activeChartVersion ||
        activeChartVersion.code !== state.code || activeChartVersion.freq !== state.freq)) return;
    var html;
    if (kind === 'ai') html = renderScenario(payload);
    else if (kind === 'card') html = renderEvidenceDetail(payload);
    else if (kind === 'bs') html = renderSignalDetail(payload);
    else return;
    if (!html) return;  // 渲染落空（如 payload 缺失）不开空覆盖层
    el('aiPopBody').innerHTML = html;
    detailKind = kind;
    aiPopupTrigger = trigger || document.activeElement;
    var pop = el('aiPop');
    railSavedScroll = el('railScroll').scrollTop;
    pop.scrollTop = 0;
    pop.classList.add('open');
    pop.setAttribute('aria-hidden', 'false');
    pop.inert = false;
    for (var i = 0; i < pop.parentElement.children.length; i++) {
      var sib = pop.parentElement.children[i];
      if (sib !== pop) sib.inert = true;
    }
    el('aiPopBack').focus({preventScroll: true});
    /* ≤760px 时 main 是滚动容器、覆盖层随 #rail 流式排布，inset:0 只贴 rail 顶部：
       从 rail 底部打开会把覆盖层留在视口外，scrollIntoView 把它的顶边滚进视野
       （focus 仍 preventScroll，由这一行统一决定去向，桌面端 rail 固定不占滚动）。 */
    if (window.matchMedia('(max-width: 760px)').matches) pop.scrollIntoView({ block: 'start' });
  }
  function closeDetail() {
    var pop = el('aiPop');
    if (!pop.classList.contains('open')) return;  // 幂等兜底调用不触碰滚动/inert
    var restore = pop.contains(document.activeElement);
    pop.classList.remove('open');
    pop.setAttribute('aria-hidden', 'true');
    for (var i = 0; i < pop.parentElement.children.length; i++) {
      var sib = pop.parentElement.children[i];
      sib.inert = (sib === pop);
    }
    if (restore && aiPopupTrigger && aiPopupTrigger.isConnected) aiPopupTrigger.focus({preventScroll: true});
    // 焦点归还仍可能触发滚动对齐，滚动恢复放在其后覆盖
    el('railScroll').scrollTop = railSavedScroll;
    aiPopupTrigger = null;
    detailKind = null;
  }
  function openAiPopup(i, trigger) {
    var s = aiScenarios[i];
    if (!s) return;
    openDetail('ai', s, trigger);
  }
  function closeAiPopup() {
    closeDetail();
  }

  var pendingAnalysis = null, activeChartVersion = null;
  var analysisIdentity = null;
  /* AI 结果与在飞请求按身份槽位保留（身份 = 股票 + 成笔标准 + 提示范围 + 复权模式 + 分析周期组合）：
     切走不 abort、不丢结果，切回即恢复；超出上限按最近发起逐出并中止其请求 */
  var AI_SLOT_MAX = 10;
  var analysisSlots = {};
  function analysisSlot(identity) {
    var slot = analysisSlots[identity];
    if (slot) {  /* 触到即移到最新位 */
      delete analysisSlots[identity];
      analysisSlots[identity] = slot;
      return slot;
    }
    var keys = Object.keys(analysisSlots);
    if (keys.length >= AI_SLOT_MAX) {
      var old = analysisSlots[keys[0]];
      if (old.abort) old.abort.abort();
      delete analysisSlots[keys[0]];
    }
    slot = analysisSlots[identity] = {abort: null, inFlight: false, body: null};
    return slot;
  }
  var ruleSwitchNote = null;  // 成笔标准刚切换时的面板提示，由下一次 loadAnalysis 消费
  function acceptsRule(body, profile, scope) {
    return profile === state.ruleProfile && body.rule_profile === profile &&
      scope === state.signalScope && body.signal_scope === scope &&
      body.schema_version === 'chanpy_v2' && typeof body.calculation_id === 'string' && !!body.calculation_id;
  }
  function currentAnalysisIdentity() {
    return JSON.stringify([state.code, state.ruleProfile, state.signalScope, viewState.adjust, Object.keys(viewState.analysisTokens || {}).sort()]);
  }
  function updateAnalysisFreshness() {
    var note = el('aiDataStatus'), chart = activeChartVersion;
    var versions = pendingAnalysis && pendingAnalysis.body.data_versions;
    var mismatch = pendingAnalysis && pendingAnalysis.identity === currentAnalysisIdentity() &&
      chart && ((chart.calculation_id && pendingAnalysis.body.calculation_id !== chart.calculation_id) ||
      (versions && versions[chart.freq] && versions[chart.freq] !== chart.data_version));
    note.textContent = mismatch ? '图表与联合分析数据不同步，可刷新' : '';
    note.hidden = !mismatch;
  }
  function queueAnalysis(body) {
    pendingAnalysis = {body: body, identity: currentAnalysisIdentity()};
    showMatchingAnalysis();
    updateAnalysisFreshness();
  }
  function showMatchingAnalysis() {
    // 联合版本属于本次周期组合的数据集，不与当前单张图表的版本比较。
    if (pendingAnalysis && pendingAnalysis.identity === currentAnalysisIdentity()) {
      renderAnalysis(pendingAnalysis.body, null);
    }
  }

  function fetchAnalysisSample() {
    return fetch('/analysis.sample.json').then(function (r) {
      if (!r.ok) throw new Error('sample ' + r.status);
      return r.json();
    });
  }

  // 仅未配置 LLM 时渲染静态样例（无 key 也能验收 UI）；接口失败不回落样例，以免被误读为该标的的结论
  var AI_SPIN_MIN = 500;  // 旋转反馈最短时长：缓存秒回时也要让用户感知「已刷新」

  // 自动刷新与切换仅同步面板归属；AI 请求只由刷新按钮触发。
  // 身份切换不再丢弃在飞请求与已完成结果：按身份落槽，切回时恢复。
  function syncManualAnalysis() {
    var identity = currentAnalysisIdentity();
    if (identity === analysisIdentity) return;
    // 409 后的整窗重载换了组合（同一标的与规则）时沿用重新分析提示
    var keepStale = !!staleNoteIdentity &&
      JSON.stringify(JSON.parse(staleNoteIdentity).slice(0, 4)) === JSON.stringify(JSON.parse(identity).slice(0, 4));
    staleNoteIdentity = null;
    analysisIdentity = identity;
    ruleSwitchNote = null;
    var slot = analysisSlots[identity];
    var busy = !!(slot && slot.inFlight);
    pendingAnalysis = slot && slot.body ? {body: slot.body, identity: identity} : null;
    if (pendingAnalysis) {
      showMatchingAnalysis();
    } else {
      el('aiPanel').innerHTML = '<div class="ai-note">' + (busy ? '加载中…' : keepStale ? '数据已更新，请重新分析' : '点击刷新生成分析') + '</div>';
      el('aiDataStatus').hidden = true;
      el('aiSampleBadge').hidden = true;
    }
    if (typeof closeAiPopup === 'function') closeAiPopup();
    var btn = el('aiRefresh');
    btn.classList[busy ? 'add' : 'remove']('spin');
    btn.disabled = busy;
    updateAnalysisFreshness();
  }

  // 服务端判定分析令牌与当前数据不符（分析期间有定稿、修订或隔离，或旧页面缺令牌）时返回 409：
  // 不调用模型；前端整窗重载主图以取得新令牌，提示用户重新分析，不自动重发（避免重复调用模型）。
  var ANALYSIS_STALE = {};
  var staleNoteIdentity = null;  // 提示只延续到这次整窗重载落地
  function onAnalysisStale(identity) {
    if (identity !== currentAnalysisIdentity()) return;  // 已切走：不动当前面板，也不重载别的标的
    pendingAnalysis = null;
    staleNoteIdentity = identity;
    el('aiPanel').innerHTML = '<div class="ai-note">数据已更新，请重新分析</div>';
    el('aiDataStatus').hidden = true;
    el('aiSampleBadge').hidden = true;
    reloadWholeWindow();
  }

  function loadAnalysis(options) {
    if (!options || options.manual !== true) return;
    closeAiPopup();
    var profile = state.ruleProfile, scope = state.signalScope, adjust = viewState.adjust;
    // 分析令牌只属于给出它的那次主图（同一代码与复权模式）；切走后新图未到时不借用旧代码的令牌
    var tokens = loadedTarget && loadedTarget.code === state.code && loadedTarget.adjust === adjust
      ? viewState.analysisTokens : null;
    if (!state.code) { el('aiPanel').innerHTML = ''; return; }
    var identity = currentAnalysisIdentity();
    var slot = analysisSlot(identity);
    if (identity === analysisIdentity && (slot.inFlight || !(options && options.refresh))) return;
    if (identity !== analysisIdentity || !pendingAnalysis) {
      pendingAnalysis = null;
      el('aiPanel').innerHTML = '<div class="ai-note">' + (ruleSwitchNote || '加载中…') + '</div>';
      ruleSwitchNote = null;
    }
    analysisIdentity = identity;
    slot.inFlight = true;
    if (slot.abort) slot.abort.abort();
    var ctl = slot.abort = new AbortController();
    var btn = el('aiRefresh');
    btn.classList.add('spin');
    btn.disabled = true;
    var t0 = Date.now();
    var timer = setTimeout(function () {
      ctl.abort(new Error('analysis timeout'));
    }, ANALYSIS_TIMEOUT_MS);
    // 没有分析令牌（主图尚未给出）时不带 tokens 参数：服务端按缺令牌返回 409，走同一重载路径
    fetch('/api/analysis?code=' + encodeURIComponent(state.code) + '&freq=day&rule_profile=' + encodeURIComponent(profile) +
          '&signal_scope=' + encodeURIComponent(scope) + '&adjust=' + encodeURIComponent(adjust) +
          (tokens ? '&tokens=' + encodeURIComponent(JSON.stringify(tokens)) : ''), { signal: ctl.signal })
      .then(function (r) {
        if (r.status === 409) return ANALYSIS_STALE;
        if (!r.ok) throw new Error('analysis ' + r.status);
        return r.json();
      })
      .then(function (j) {
        if (ctl !== slot.abort) return;
        if (j === ANALYSIS_STALE) { onAnalysisStale(identity); return; }
        if (!acceptsRule(j, profile, scope)) {  // 规则身份不匹配：显式提示，不静默停在加载态
          if (identity === currentAnalysisIdentity()) {
            analysisIdentity = null;
            el('aiPanel').innerHTML = '<div class="ai-note">分析响应与当前成笔标准或提示范围不一致，请刷新</div>';
          }
          return;
        }
        if (j.status === 'disabled') {
          if (identity === currentAnalysisIdentity()) {
            el('aiPanel').innerHTML = '<div class="ai-note">demo 模式不调用 AI</div>';
            el('aiDataStatus').hidden = true;
            el('aiSampleBadge').hidden = true;
          }
          return;
        }
        if (j.status === 'ok') {
          slot.body = j;   /* 结果落槽：切走后才完成也不丢，切回即恢复 */
          if (identity === currentAnalysisIdentity()) queueAnalysis(j);
          return;
        }
        return fetchAnalysisSample().then(function (s) { if (ctl === slot.abort && identity === currentAnalysisIdentity()) renderAnalysis(s, 'unconfigured'); });
      })
      .catch(function (e) {
        if (ctl !== slot.abort || identity !== currentAnalysisIdentity()) return;
        if (e && e.name === 'AbortError') return;  // 被新请求中止，静默
        pendingAnalysis = null;
        el('aiPanel').innerHTML = '<div class="ai-note">完全分类不可用</div>';
        el('aiDataStatus').hidden = true;
        el('aiSampleBadge').hidden = true;
      })
      .then(function () {  // 按钮复位仅由最新一次请求执行，且只在身份仍当前时触碰 UI
        if (ctl !== slot.abort) return;
        var wait = Math.max(0, AI_SPIN_MIN - (Date.now() - t0));
        setTimeout(function () {
          if (ctl !== slot.abort || identity !== currentAnalysisIdentity()) return;
          btn.classList.remove('spin');
          btn.disabled = false;
        }, wait);
      })
      .finally(function () {
        clearTimeout(timer);
        if (ctl === slot.abort) slot.inFlight = false;
      });
  }

  // ---------- 数据口径条 ----------

  // 覆盖提示：复权段带前复权覆盖起点（coverage.qfq_from，原始价/指数/港股供应商口径为空），
  // 视图提示（notices：前复权显示至/起可用、分钟历史加载中、结构输入尚未补齐、该市场暂不提供）逐条醒目显示
  function renderMeta(meta) {
    lastMeta = meta;
    renderStatus();
    var bar = el('metaBar');
    var qfqFrom = meta.coverage && meta.coverage.qfq_from;
    var parts = [
      {text: '数据源：' + meta.source},
      {text: '复权：' + meta.fqf + (qfqFrom ? '（自 ' + qfqFrom + '）' : '')},
      {text: meta.coverage && meta.coverage.data_status && meta.coverage.data_status.phase === 'historical'
        ? '历史截止于 ' + (meta.coverage.data_status.day || '未知日期')
        : '抓取：' + (meta.fetch_time || '暂无记录'), badge: cacheBadge(meta)},
      {text: 'K线：' + meta.bars + ' 根', title: meta.first_dt + ' ~ ' + meta.last_dt},
    ];
    (meta.notices || []).forEach(function (notice) {
      if (notice && notice.text) parts.push({text: notice.text, cls: 'warn'});
    });
    bar.innerHTML = parts.map(function (part) {
      return '<span' + (part.cls ? ' class="' + part.cls + '"' : '') + ' title="' + esc(part.title || part.text) + '">' +
        esc(part.text) + (part.badge || '') + '</span>';
    }).join('');
    bar.title = meta.degraded && meta.degraded_note ? meta.degraded_note : '';
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
    state.watchlist.concat(state.recent || []).some(function (x) { return x.code === code && (w = x); });
    return w ? w.name : code;
  }

  function hideF10Cards() {
    el('f10Sec').style.display = 'none';
    el('f10Stale').hidden = true;         // F10 的缓存注记随 F10 收起；价格卡不动
    el('flowSec').style.display = 'none';
    el('flowNote').textContent = '';
    el('flowNote').hidden = true;
  }

  // 卡头：独立的价格卡，F10 慢或失败不挡。名称取自选股（查不到回落 code）；价/涨幅只取行情——按当前是否自选
  // 选通道：自选取 state.quotes（报价表还没有时退回单代码报价），非自选只取单代码报价 state.viewQuote（按代码
  // 核对；移出自选后报价表里的残留旧价不再刷新，不能遮住它）——不回退 F10 价格
  function renderF10Header() {
    var vq = state.viewQuote;
    var view = vq && vq.code === state.code ? vq.quote : null;
    var v = quoteView(isWatched(state.code) ? (state.quotes || {})[state.code] || view : view);
    el('quoteSec').style.display = state.code ? '' : 'none';
    el('f10Name').textContent = watchName(state.code);
    el('f10Code').textContent = state.code;
    var px = el('f10Px');
    px.textContent = v.price == null ? '--' : v.price.toFixed(2);
    px.className = 'px' + (v.pct == null ? '' : ' ' + signCls(v.pct)) + (v.stale ? ' stale' : '');
    px.title = (v.stale ? '数据延迟（采集未按时更新） · ' : '') + (v.time ? '价格时刻 ' + v.time : (v.prevClose ? '昨日收盘价' : ''));
    var chg = el('f10Chg');
    chg.textContent = v.unavailable ? '暂无可信价格' : v.prevClose ? '昨收'
      : v.pct == null ? '' : (v.pct > 0 ? '+' : '') + v.pct.toFixed(2) + '%';
    if (v.historical) chg.textContent = '历史' + (chg.textContent ? ' · ' + chg.textContent : '');
    chg.className = 'chg' + (v.pct == null ? ' flat' : ' ' + signCls(v.pct));
  }

  function renderF10(j) {
    var f = j.f10 || {};
    el('f10Sec').style.display = '';
    var stale = el('f10Stale');
    var dataNote = displayDataNote(j);
    stale.textContent = dataNote ? (j.degraded ? '缓存' : '数据') : '';
    stale.title = dataNote;
    stale.setAttribute('aria-label', dataNote);
    stale.hidden = !dataNote;
    renderF10Header();
    renderTags();
    if (j.meta && j.meta.mode === 'demo' && !Object.keys(f).length) {
      el('f10Grid').innerHTML = '<div class="ai-note">样本未提供基本资料</div>';
      return;
    }
    var cells = [
      ['总市值', fmtYi(f.total_mv)], ['PE(TTM)', numOr(f.pe_ttm)], ['PB', numOr(f.pb)],
      ['成交额', fmtYi(f.amount)], ['换手', pctOr(f.turnover)], ['量比', numOr(f.volume_ratio)],
      ['外盘', fmtYi(f.outer)], ['内盘', fmtYi(f.inner)], ['振幅', pctOr(f.amplitude)],
    ];
    el('f10Grid').innerHTML = cells.map(function (c) {
      return '<div class="kv"><div class="k">' + c[0] + '</div><div class="v">' + c[1] + '</div></div>';
    }).join('');
  }

  // 自定义标签行：取自 state.watchlist；末尾编辑 chip（无标签时占位「+ 标签」）进编辑态。非自选股只读，不渲染编辑 chip（保存必然 404）
  function renderTags(force) {
    var box = el('f10Tags');
    var currentInput = box.querySelector('.tag-input');
    if (!force && currentInput && currentInput.getAttribute('data-code') === state.code) return;
    var w = null;
    state.watchlist.forEach(function (x) { if (x.code === state.code) w = x; });
    var tags = (w && w.tags) || [];
    box.innerHTML = '';
    tags.forEach(function (t) {
      var chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = t;
      box.appendChild(chip);
    });
    if (w) {
      var edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'chip tag-edit';
      edit.id = 'f10TagEdit';
      edit.textContent = tags.length ? '✎ 编辑' : '+ 标签';
      edit.onclick = function () { editTags(tags); };
      box.appendChild(edit);
    }
  }

  // 编辑态：标签行换成单个 input（现有标签、连接）；Enter/失焦保存，Esc 取消（同搜索框键盘习惯）
  function editTags(tags) {
    var box = el('f10Tags');
    var code = state.code;
    box.innerHTML = '';
    var label = document.createElement('label');
    label.setAttribute('for', 'f10TagInput');
    label.textContent = '自选股标签';
    label.style.cssText = 'position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap';
    var input = document.createElement('input');
    input.id = 'f10TagInput';
    input.className = 'tag-input';
    input.setAttribute('data-code', code);
    input.value = tags.join('、');
    input.placeholder = '多个标签用、分隔';
    box.appendChild(label);
    box.appendChild(input);
    input.focus();
    var settled = false;  // Enter 后 blur 会再触发一次保存，只放行第一次
    function save() {
      if (settled) return;
      settled = true;
      input.readOnly = true;
      input.setAttribute('aria-busy', 'true');
      var next = input.value.split(/[、,，;；\s]+/).filter(function (t) { return t; });
      return queueWatchlist(function () {
        return fetch('/api/watchlist/' + encodeURIComponent(code) + '/tags', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tags: next }),
        }).then(function (r) {
          if (!r.ok) throw new Error('标签保存失败 ' + r.status);
          return r.json();
        }).then(function (items) {
          state.watchlist = items;
          if (state.code === code && input.parentNode === box) renderTags(true);
        });
      }).catch(function (e) {
        setStatus(e.message);
        if (state.code === code && input.parentNode === box) renderTags(true);
      });
    }
    input.onkeydown = function (ev) {
      if (settled) return;
      if (ev.key === 'Escape') { ev.preventDefault(); settled = true; renderTags(true); return; }
      if (ev.key === 'Enter' && (ev.isComposing || ev.keyCode === 229)) return;
      if (ev.key === 'Enter') { ev.preventDefault(); save(); }
    };
    input.onblur = save;
  }

  function renderFlow(j) {
    if (j.meta && j.meta.mode === 'demo' && !Object.keys(j.flow || {}).length) {
      el('flowSec').style.display = 'none';
      return;
    }
    var note = j.meta && j.meta.flow_note;
    el('flowNote').textContent = note || '';
    el('flowNote').hidden = !note;
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

  // 与 /api/chart 并行调用；失败（含 502/退避）→ 两卡整卡隐藏，不显示报错。
  // F10 只随代码变化（与周期、复权无关）：响应只在请求发出时的代码仍是当前代码时接纳。
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

  // 同标的/同周期的后台刷新保留可视区间：历史区按时间锚点恢复，最新端仅随新 bar 前移
  function refreshRangePlan(keep, newBars) {
    if (!keep || !newBars || !newBars.length) return null;
    var newLen = newBars.length, span = keep.to - keep.from;
    var newLastTime = toTime(newBars[newLen - 1].time);
    if (keep.to >= keep.len - 1.5 && newLastTime !== keep.lastTime) {
      var latestTo = newLen - 1 + 3;
      return { from: Math.max(0, latestTo - span), to: latestTo };
    }
    var anchorIndex = -1;
    for (var i = 0; i < newLen; i++) {
      if (toTime(newBars[i].time) === keep.anchorTime) { anchorIndex = i; break; }
    }
    if (anchorIndex >= 0) {
      var anchoredFrom = anchorIndex + keep.anchorOffset;
      return { from: anchoredFrom, to: anchoredFrom + span };
    }
    var fallbackFrom = keep.from + newLen - keep.len;
    var maxFrom = Math.max(0, newLen - 1 - span);
    fallbackFrom = Math.max(0, Math.min(fallbackFrom, maxFrom));
    return { from: fallbackFrom, to: fallbackFrom + span };
  }

  function captureRefreshRange(range, bars) {
    if (!range || !bars || !bars.length) return null;
    var anchorIndex = Math.max(0, Math.min(bars.length - 1, Math.floor(range.from)));
    return {
      from: range.from, to: range.to, len: bars.length,
      anchorTime: toTime(bars[anchorIndex].time), anchorOffset: range.from - anchorIndex,
      lastTime: toTime(bars[bars.length - 1].time),
    };
  }

  // 同一视图令牌下的刷新保留左拉得到的旧段（严格早于新窗首行）；令牌不同或旧令牌已作废时整窗替换，
  // 不混接新旧数据。
  function mergeWithHistory(incoming, token, previousToken, currentBars) {
    currentBars = currentBars || klineData;
    if (!token || token !== previousToken || !currentBars.length || !incoming.length ||
        toTime(currentBars[0].time) >= toTime(incoming[0].time)) return incoming;
    var incomingFirst = toTime(incoming[0].time);
    var older = currentBars.filter(function (b) { return toTime(b.time) < incomingFirst; });
    return older.length ? older.concat(incoming) : incoming;
  }

  // 响应接纳：请求发出时的 code、freq、adjust 仍是当前值（切走后迟到的响应不渲染）
  function isCurrentView(request) {
    return periodEnabled(request.freq) && request.code === state.code && request.freq === state.freq && request.adjust === viewState.adjust;
  }

  // 整窗重载：作废旧令牌（重载结果不与旧段拼接）、使在飞分页失效、去掉条件请求以保证拿到新令牌。
  // notice 在新窗口提交后显示。
  function reloadWholeWindow(notice) {
    viewState.token = null;
    historyState.loading = false;
    historyState.reqId++;
    chartEtag = null;
    load({ refresh: true, notice: notice });
  }

  // 临时提示（整窗重载后「数据已更新」），8s 后若未被其他状态覆盖则回落常驻状态。
  // 提示期内的左拉分页不覆盖它（重载落在左沿时会立刻续拉下一页）。
  var NOTICE_MS = 8000, activeNotice = '';
  function showNotice(notice) {
    activeNotice = notice;
    setStatus(notice);
    setTimeout(function () {
      if (activeNotice === notice) activeNotice = '';
      if (statusMsg === notice) setStatus('');
    }, NOTICE_MS);
  }

  function onMainRangeChanged(range) {
    if (!range || historyState.loading) return;
    if (!periodAvailable(state.freq)) return;
    if (!viewState.token || !klineData.length) return;
    if (range.from > 8) return;
    if (historyState.hasMore) loadHistoryPage();
    else hintFullHistory();
  }

  // 非自选代码左拉到本地最早数据：只提示，不联网扩大范围（查看只保证近期分析窗口）；同一视图只提示一次
  var edgeHinted = null;
  function hintFullHistory() {
    var view = state.code + '|' + state.freq + '|' + viewState.adjust;
    var watched = state.watchlist.some(function (w) { return w.code === state.code; });
    if (!state.code || watched || edgeHinted === view) return;
    edgeHinted = view;
    showNotice(sessionDemo ? '已到样本最早数据' : '已到本地最早数据，加入自选以补完整历史');
  }

  var HISTORY_STALE = {};
  function loadHistoryPage() {
    if (!periodEnabled(state.freq)) return;
    var request = {
      code: state.code, freq: state.freq, adjust: viewState.adjust, token: viewState.token,
      firstTime: klineData[0].time, reqId: ++historyState.reqId,
    };
    function current() {
      return historyState.reqId === request.reqId && isCurrentView(request) &&
        klineData.length > 0 && klineData[0].time === request.firstTime;
    }
    historyState.loading = true;
    var failed = false;
    if (!activeNotice) setStatus('加载历史…');
    fetch('/api/chart?code=' + encodeURIComponent(request.code) + '&freq=' + request.freq +
          '&adjust=' + encodeURIComponent(request.adjust) +
          '&before=' + encodeURIComponent(request.firstTime) + '&limit=520' +
          '&token=' + encodeURIComponent(request.token))
      .then(function (r) {
        if (r.status === 409) return HISTORY_STALE;  // 分页期间数据已更新：令牌不符
        if (!r.ok) throw new Error('history ' + r.status);
        return r.json();
      })
      .then(function (page) {
        if (!current()) return;
        if (page !== HISTORY_STALE && (!page || !page.meta || !page.meta.history)) return;
        if (page === HISTORY_STALE || page.meta.token !== request.token) {
          reloadWholeWindow('数据已更新，已重新加载');
          return;
        }
        prependHistory(page);
      })
      .catch(function () { failed = true; })
      .finally(function () {
        if (historyState.reqId !== request.reqId || !isCurrentView(request)) return;
        historyState.loading = false;
        setStatus(failed ? '历史加载失败' : activeNotice);
      });
  }

  function prependHistory(page) {
    var first = toTime(klineData[0].time);
    var older = page.kline.filter(function (b) { return toTime(b.time) < first; });
    historyState.hasMore = !!page.meta.has_more;
    if (lastChartData && lastChartData.meta) {
      lastChartData.meta = Object.assign({}, lastChartData.meta, { has_more: historyState.hasMore });
    }
    if (!older.length) return;
    var range = charts.main.timeScale().getVisibleLogicalRange();
    klineData = older.concat(klineData);
    klineByTime = {};
    klineData.forEach(function (b, i) {
      klineByTime[toTime(b.time)] = { bar: b, prev: i > 0 ? klineData[i - 1] : null, i: i };
    });
    charts.candleSeries.setData(klineData.map(function (b) {
      return { time: toTime(b.time), open: b.open, high: b.high, low: b.low, close: b.close };
    }));
    charts.volumeSeries.setData(klineData.map(function (b) {
      return { time: toTime(b.time), value: b.volume, color: b.close >= b.open ? P.upA : P.downA };
    }));
    computeMAs();
    refreshMaSeries();
    showInd(curInd);
    if (range) {
      var shifted = { from: range.from + older.length, to: range.to + older.length };
      charts.main.timeScale().setVisibleLogicalRange(shifted);
      charts.macdChart.timeScale().setVisibleLogicalRange(shifted);
    }
    charts.markers.setMarkers(buildMarkers(lastChartData.signals));
    lastChartData = Object.assign({}, lastChartData, { kline: klineData });
  }

  // 图表渲染主路径：fetch 后与主题切换共用（opts.resetRange=false 时保留可视区间）
  function renderChart(data, opts) {
    var c = ensureCharts();
    opts = opts || {};
    var meta = data.meta || {};
    var token = meta.token || null;
    var kline = opts.mergedKline || (opts.resetRange === false
      ? mergeWithHistory(data.kline, token, viewState.token, klineData)
      : data.kline);
    // 视图令牌随图表内容一起登记；每次重建都递增 reqId，使在飞的旧分页失效
    viewState.token = token;
    historyState = { loading: false, hasMore: !!(token && periodAvailable(state.freq) && meta.has_more),
      reqId: historyState.reqId + 1 };
    klineData = kline;
    klineByTime = {};
    kline.forEach(function (b, i) {
      klineByTime[toTime(b.time)] = { bar: b, prev: i > 0 ? kline[i - 1] : null, i: i };
    });
    macdRowsRaw = data.macd.rows;
    lastChartData = Object.assign({}, data, { kline: kline });

    c.candleSeries.setData(kline.map(function (b) {
      return { time: toTime(b.time), open: b.open, high: b.high, low: b.low, close: b.close };
    }));
    c.volumeSeries.setData(kline.map(function (b) {
      return { time: toTime(b.time), value: b.volume, color: b.close >= b.open ? P.upA : P.downA };
    }));

    // 笔：端点连续（zigzag），单线即可；forming 笔/段逐条独立虚线 series
    c.biSeries.setData(segsToPoints(data.structure.bi.filter(function (s) { return !s.forming; })));
    var xdConfirmed = data.structure.xd.filter(function (s) { return !s.forming; });
    c.xdSeries.setData(segsToPoints(xdConfirmed));
    function setFormingSegs(key, segs, color, width) {
      c[key].forEach(function (s) { c.main.removeSeries(s); });
      c[key] = segs.map(function (b) {
        var s = c.main.addSeries(LightweightCharts.LineSeries, {
          color: color, lineWidth: width, lineStyle: LightweightCharts.LineStyle.Dashed,
          crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
        });
        s.setData([{ time: toTime(b.dt0), value: b.y0 }, { time: toTime(b.dt1), value: b.y1 }]);
        return s;
      });
    }
    setFormingSegs('formingXdSegs', data.structure.xd.filter(function (s) { return s.forming; }), P.goldA, 2);
    setFormingSegs('formingBiSegs', data.structure.bi.filter(function (s) { return s.forming; }), P.biForming, 1);

    c.zsOverlay.setBoxes(data.structure.zs.map(function (z) {
      return { t0: toTime(z.dt0), t1: toTime(z.dt1), zg: z.zg, zd: z.zd };
    }));

    computeMAs();
    refreshMaSeries();
    showInd(curInd);

    // 通道线：最近一条已完成线段的上下轨，金色虚线（主图）
    c.channelSeries.forEach(function (s) { c.main.removeSeries(s); });
    c.channelSeries = (data.channels || []).reduce(function (acc, ch) {
      ['upper', 'lower'].forEach(function (k) {
        var r = ch[k];
        var s = c.main.addSeries(LightweightCharts.LineSeries, {
          // 延长轨只作几何参考，不让投影端点挤压实际价格范围。
          autoscaleInfoProvider: function () { return null; },
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

    // 信号箭头必须在同一任务内所有 series 更新（K线/MA/通道/可视区间）之后设置：
    // lightweight-charts 5.0.9 markers 插件对其后的 series setData 敏感（上游 issue #1990），
    // 否则新 bar 到达时箭头按旧索引落位并持续错位，直到下一次重绘才跳回
    c.markers.setMarkers(buildMarkers(data.signals));

    legendLatest();
    renderResonance(data.resonance, (data.meta || {}).analysis_freqs);
    updateAxisAnchor();
  }

  function load(options) {
    // 选择已经变更；旧图表详情须在早退前收回。
    // 联合 AI 的身份不含当前周期，仍由其原生命周期管理。
    if (detailKind === 'card' || detailKind === 'bs') closeDetail();
    if (!state.code) return;
    // 已下线的 5分/15分（旧链接、旧状态）归到 30 分，无法识别的回日线；每次加载同步周期与复权控件
    state.freq = normalizeFreq(state.freq);
    if (!periodPrefs || periodPrefs.notice) {
      clearPeriodView(periodPrefs ? '请确认展示周期' : '正在读取周期偏好');
      return;
    }
    if (!periodEnabled(state.freq)) state.freq = periodPrefs.selected.find(periodEnabled) || null;
    renderTabs();
    if (!state.freq) { clearPeriodView('当前市场没有已勾选的可用周期'); return; }
    var profile = state.ruleProfile, scope = state.signalScope;
    var notice = options && options.notice || '';
    var refetch = !!(options && options.refetch);
    var busyRetry = options && options.busyRetry || 0;  // 503 自动重试的第几次（0 为用户或定时器发起）  // 手动重拉：服务端先整段重取分析窗口；不带条件头，避免 304
    // 请求发出时的视图身份：响应只在它仍是当前值时接纳
    var request = { code: state.code, freq: state.freq, adjust: viewState.adjust };
    activeChartVersion = null;
    updateAnalysisFreshness();
    ensureCharts();
    if (chartAbort) chartAbort.abort();
    var ctl = chartAbort = new AbortController();
    // 进度细线只跟用户发起的加载（切换、重拉）；定时刷新只在接替一个在途的用户加载时接力
    if (!(options && options.refresh) || refetch || progressOwner !== null) progressStart(ctl);
    updateAiTitle();
    setStatus((refetch ? '重拉中… ' : '加载中… ') + state.code + ' ' + state.freq);
    hideChartError();
    if (!options || !options.refresh) {  // 用户切换：新数据到达前旧内容降为「旧数据」标记，失败时保留
      if (!busyRetry) state.openSeq = (state.openSeq || 0) + 1;  // 这次打开：收盘后跟进按打开计，重开同一代码是新的一次；自动重试不算
      el('center').classList.add('ctx-old');
      el('rail').classList.add('ctx-old');
    }
    // 只记录刷新身份；可视区间必须在响应提交前再取，避免覆盖请求期间的用户平移。
    var sameTarget = function () {
      return !!loadedTarget && loadedTarget.code === request.code && loadedTarget.freq === request.freq &&
        loadedTarget.adjust === request.adjust;
    };
    var refreshTarget = options && options.refresh && sameTarget();
    renderF10Header();    // 价格卡先按已有行情显示（F10 慢或失败不挡价格）
    if (!(options && options.refresh) && !busyRetry) loadViewQuote(state.code);  // 非自选：打开时取一次单代码报价，之后随报价轮询
    loadF10(state.code);  // F10/资金流与 /api/chart 并行，互不阻塞（内部有 300s TTL）
    syncManualAnalysis(); // 切换与行情自动刷新不请求 AI
    var timer = setTimeout(function () {
      ctl.abort(new Error(refetch ? '重拉超时（120s）' : '加载超时（25s）'));
    }, refetch ? REFETCH_TIMEOUT_MS : CHART_TIMEOUT_MS);
    fetch('/api/chart?code=' + encodeURIComponent(request.code) + '&freq=' + request.freq +
          '&adjust=' + encodeURIComponent(request.adjust) +
          '&rule_profile=' + encodeURIComponent(profile) + '&signal_scope=' + encodeURIComponent(scope) +
          (refetch ? '&refetch=1' : ''),
          { signal: ctl.signal, headers: refetch ? {} : conditionalHeaders(request.code, request.freq, request.adjust, profile, scope) })
      .then(function (r) {
        if (r.status === 304) return null;  // 内容未变：保留现有图表，只清状态
        if (refetch) notice = refetchNotice(r.headers.get('X-Refetch-Status'));
        if (!r.ok) return r.json().then(function (j) { var err = new Error(j.detail || r.status); err.status = r.status; throw err; });
        if (ctl === chartAbort && isCurrentView(request)) {
          rememberEtag(request.code, request.freq, request.adjust, profile, scope, r.headers.get('ETag'));
        }
        return r.json();
      })
      .then(function (data) {
        if (ctl !== chartAbort || !isCurrentView(request)) return;  // 已切走：迟到的旧响应不渲染
        if (data === null) {
          el('center').classList.remove('chart-reloading');
          el('center').classList.remove('ctx-old');
          el('rail').classList.remove('ctx-old');
          renderStatus();
          if (notice) showNotice(notice); else setStatus('');
          return;
        }
        if (!acceptsRule(data, profile, scope)) {  // 规则身份不匹配：显式报错并解除遮罩，不静默停在加载态
          el('center').classList.remove('chart-reloading');
          setStatus('加载失败');
          showChartError('响应与当前成笔标准或提示范围不一致，请刷新');
          return;
        }
        var meta = data.meta || {};
        var saved = restoreRange && restoreRange.code === request.code && restoreRange.freq === request.freq && restoreRange.range;
        var keep = null;
        if (!saved && refreshTarget && charts && charts.main && sameTarget() && klineData.length) {
          keep = captureRefreshRange(charts.main.timeScale().getVisibleLogicalRange(), klineData);
        }
        var incomingKline = data.kline || [];
        var allowHistoryMerge = !!saved || !!(refreshTarget && sameTarget());
        var mergedKline = allowHistoryMerge
          ? mergeWithHistory(incomingKline, meta.token, viewState.token, klineData)
          : incomingKline;
        var plan = keep ? refreshRangePlan(keep, mergedKline) : null;
        renderChart(data, { resetRange: !saved && !plan, mergedKline: mergedKline });
        if (saved) {
          charts.main.timeScale().setVisibleRange(saved);
          charts.macdChart.timeScale().setVisibleRange(saved);
        } else if (plan) {
          charts.main.timeScale().setVisibleLogicalRange(plan);
          charts.macdChart.timeScale().setVisibleLogicalRange(plan);
        }
        restoreRange = null;
        el('center').classList.remove('period-empty');
        el('rail').classList.remove('period-empty');
        loadedTarget = { code: request.code, freq: request.freq, adjust: request.adjust };
        var previousAnalysisIdentity = currentAnalysisIdentity();
        viewState.analysisTokens = meta.analysis_tokens || null;
        if (previousAnalysisIdentity !== currentAnalysisIdentity()) syncManualAnalysis();
        staleNoteIdentity = null;
        el('center').classList.remove('chart-reloading');
        el('center').classList.remove('ctx-old');
        el('rail').classList.remove('ctx-old');
        renderEvidence(data.evidence, {preserve: !!plan});
        renderMeta(meta);
        // 首开非自选：单代码报价与首取并行，事实还没写入时报价为空；首取完成后补取一次（刷新与已有价格不补）
        var vq = state.viewQuote;
        if (!(options && options.refresh) && !(vq && vq.code === request.code && vq.quote && vq.quote.price != null)) {
          loadViewQuote(request.code);
        }
        activeChartVersion = {code: request.code, freq: request.freq, adjust: request.adjust, calculation_id: data.calculation_id, data_version: data.data_version || meta.data_version};
        updateAnalysisFreshness();
        if (notice) showNotice(notice); else setStatus('');
      })
      .catch(function (e) {
        if (e && e.name === 'AbortError') return;  // 被新请求中止，静默
        if (ctl !== chartAbort || !isCurrentView(request)) return;  // 旧请求（超时等）或已切走：已有新请求接管
        if (e && e.status === 400) {
          // 另一页面取消周期或服务重启：先收起旧图；仅 revision 变化才自动重载，避免 400 循环。
          clearPeriodView('正在核对展示周期');
          loadPeriodPrefs({sync: true}).then(function (changed) {
            if (changed === false) { setStatus('加载失败：' + e.message); showChartError(e.message); }
          });
          return;
        }
        // 503：同一代码的历史规划、缓存刷新或重拉在途，服务端等锁超过预算且没有可服务快照——隔几秒自动重试，
        // 次数有上限（收盘后没有定时刷新兜底）；手动重拉不自动重试（结果由 X-Refetch-Status 说明）
        if (e && e.status === 503 && !refetch && busyRetry < BUSY_RETRY_MAX) {
          setStatus(e.message + '，' + BUSY_RETRY_MS / 1000 + ' 秒后自动重试（' + (busyRetry + 1) + '/' + BUSY_RETRY_MAX + '）');
          setTimeout(function () {
            if (ctl !== chartAbort || !isCurrentView(request)) return;  // 已切走或已有新请求：不重试
            load({ refresh: !!(options && options.refresh), notice: notice, busyRetry: busyRetry + 1 });
          }, BUSY_RETRY_MS);
          return;
        }
        el('center').classList.remove('chart-reloading');  // 失败也要解除遮罩，露出错误条
        setStatus('加载失败：' + e.message);
        showChartError(e.message);
      })
      .finally(function () { clearTimeout(timer); progressDone(ctl); });
  }

  var BUSY_RETRY_MS = 5000, BUSY_RETRY_MAX = 6;  // 503 自动重试：约 30 秒内至多 6 次

  // 顶部加载细线：请求超过 PROGRESS_DELAY_MS 仍未完成才出现，快响应不闪。
  // 新请求取代旧请求时接过所有权并沿用首次计时，旧请求迟到的完成不再收起或重新拉起。
  var PROGRESS_DELAY_MS = 150;
  var progressOwner = null, progressTimer = null;
  function progressStart(owner) {
    var relay = progressOwner !== null;
    progressOwner = owner;
    el('center').setAttribute('aria-busy', 'true');
    if (relay) return;
    progressTimer = setTimeout(function () { progressTimer = null; showProgress(true); }, PROGRESS_DELAY_MS);
  }
  function progressDone(owner) {
    if (owner !== progressOwner) return;
    progressOwner = null;
    clearTimeout(progressTimer);
    progressTimer = null;
    showProgress(false);
    el('center').setAttribute('aria-busy', 'false');
  }
  function showProgress(on) {
    var bar = el('loadBar');
    bar.classList.toggle('on', on);
    bar.setAttribute('aria-hidden', on ? 'false' : 'true');
  }

  // 手动重拉：强制重新请求当前分析窗口（不改变关注状态）；失败时保留现有图表
  // 服务端经 X-Refetch-Status 报告重拉结果；只有 ok 才说「已重拉」，其余如实说明（图表都照常显示已有数据）
  var REFETCH_NOTICES = {
    ok: '已重拉当前窗口',
    partial: '重拉部分完成，未取到的部分沿用已有数据',
    failed: '重拉失败，显示的是已有数据',
    busy: '正在更新这只标的，本次没有重拉，请稍后再试',
    disabled: '当前环境不支持重拉，显示的是已有数据',
    unavailable: '当前环境不支持重拉，显示的是已有数据',
  };
  function refetchNotice(status) { return REFETCH_NOTICES[status] || '重拉结果未知，显示的是已有数据'; }

  function refetchCurrent() {
    if (!state.code) return;
    load({ refresh: true, refetch: true });
  }

  // 页面周期（目标 2026-09-29 第三阶段）：5分/15分已下线，服务端对它们回 400；旧链接与旧状态归到 30 分。
  // 启动时读 URL 就要调用（函数声明会提升，外部 var 表那时还没赋值），所以不依赖外部变量。
  function normalizeFreq(freq) {
    if (freq === 'day' || freq === 'week' || freq === 'm60' || freq === 'm30') return freq;
    return freq === 'm5' || freq === 'm15' ? 'm30' : 'day';
  }

  // ---------- 交易时段自动刷新（开市与否只认后端 /api/session：交易日历 + engine/session.py 时段） ----------

  function marketOf(code) { return code && code.indexOf('hk') === 0 ? 'hk' : 'cn'; }

  var sessionOpen = { cn: false, hk: false };  // 从未取到按未开市；取数失败保留上次结果
  var sessionDemo = false;
  var sessionReq = null;                       // 在途请求：未返回时不叠加

  function refreshSession(verify) {
    if (sessionDemo && !verify) return Promise.resolve();  // demo 不轮询；verify 只在回到前台时核对一次模式
    if (sessionReq) return sessionReq;
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 10000);
    sessionReq = fetch('/api/session', { cache: 'no-store', signal: ctrl.signal })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (body) {
        sessionDemo = body && body.mode === 'demo';
        var m = (body && body.markets) || {};
        ['cn', 'hk'].forEach(function (k) {
          if (m[k] && typeof m[k].open === 'boolean') sessionOpen[k] = m[k].open;
        });
      })
      .catch(function () {})
      .then(function () {
        clearTimeout(timer);
        sessionReq = null;
        renderStatus();  // 交易时段点/分钟级状态保鲜
      });
    return sessionReq;
  }

  function isSessionOpen(market) { return !!sessionOpen[market]; }

  var closeFollowed = null;  // 已做过收盘后跟进的（代码|周期|打开序号|交易日）：每次打开每个收盘日至多一次，补读失败也不重试

  setInterval(function () {
    if (sessionDemo || document.hidden) return;  // demo 不轮询；后台标签不轮询：恢复前台单独同步偏好
    if (sessionReq || periodLoad || periodSaving) return; // 上一轮还没返回：由它收尾，不重复刷新图表
    refreshSession().then(function () { return loadPeriodPrefs({sync: true}); }).then(function (changed) {
      if (changed !== false || !state.code) return; // 有变化时同步函数已处理图表；失败时也不借用旧偏好刷新
      if (!isSessionOpen(marketOf(state.code))) {
        // 收盘后有限跟进：快照还停在盘中（live）时补读一次收盘后的状态，之后（待定稿/已定稿）不再轮询；
        // 次数独立计：补读失败时快照仍是 live，也不每分钟重试
        var ds = lastMeta && lastMeta.coverage && lastMeta.coverage.data_status;
        var key = [state.code, state.freq, state.openSeq, ds && ds.day].join('|');
        if (ds && ds.phase === 'live' && closeFollowed !== key) {
          closeFollowed = key;
          load({refresh:true});
        }
        return;
      }
      load({refresh:true});
    });
  }, 60000);

  // 标签页恢复前台即核对一次；不新增定时器，休市也可看到其他页面的勾选与重启后的能力变化。
  // demo 页面没有轮询，期间服务若按真实模式重启，靠这里核对模式后恢复轮询。
  // demo 下两个同时可见的窗口之间不互相同步勾选（无轮询的代价），回到前台或图表被拒时同步。
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    if (sessionDemo) refreshSession(true);
    if (!periodSaving) loadPeriodPrefs({sync: true});
  });

  // 自选股快照 60s 一轮（与图表、采集器盘中增量、显示层 QUOTE_TTL 同为 60s）。按自选各标的的市场判断：
  // 所选标的休市不停其他市场自选的报价，没有选中标的也照常。
  setInterval(function () {
    if (sessionDemo || document.hidden) return;
    if (state.code && !isWatched(state.code) && isSessionOpen(marketOf(state.code))) loadViewQuote(state.code);
    var open = (state.watchlist || []).some(function (it) { return isSessionOpen(marketOf(it.code)); });
    if (!open) return;
    loadQuotes();
  }, 60000);

  // ---------- 侧边栏（Codex 客户端式：pinned 固定展开 / rail 窄栏；窄栏悬停或聚焦自动浮动展开，移开收回） ----------

  var hoverOpen = false;      /* 当前 open 是否由悬停触发（决定移开时是否自动收回） */
  var suppressHover = false;  /* 主动收回后鼠标仍在栏内：先移出一次才允许再次悬停展开 */
  var focusMute = false;      /* 程序化焦点迁移期间抑制 focusin 重开（迁移目标在栏内，focus 会冒泡 focusin） */

  /* 展开文字编排：窄栏与展开逐行等高（CSS 不变量），进入展开态时给文字交错淡入。
     级联延迟逐行 12ms 封顶 60ms；row2 由 CSS 再延迟 90ms；reduced-motion 整体跳过。 */
  var sbAnimTimer = 0;
  function playSidebarEnter() {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    var items = el('sidebar').querySelectorAll('.item');
    for (var i = 0; i < items.length; i++) {
      var d = Math.min(i * 12, 60) + 'ms';
      var name = items[i].querySelector('.row > span:first-child');
      var chg = items[i].querySelector('.chg');
      var row2 = items[i].querySelector('.row2');
      if (name) name.style.animationDelay = d;
      if (chg) chg.style.animationDelay = d;
      if (row2) row2.style.animationDelay = 'calc(.09s + ' + d + ')';
    }
    document.body.classList.add('sb-anim-in');
    clearTimeout(sbAnimTimer);
    sbAnimTimer = setTimeout(function () {
      document.body.classList.remove('sb-anim-in');
      for (var j = 0; j < items.length; j++) {
        var s = items[j].querySelectorAll('.row > span:first-child, .chg, .row2');
        for (var k = 0; k < s.length; k++) s[k].style.animationDelay = '';
      }
    }, 620);
  }

  function sbMode() {
    var b = document.body.classList;
    return b.contains('sb-rail') ? 'rail' : (b.contains('sb-open') ? 'open' : 'pinned');
  }

  /* 窄窗（≤1100px）侧栏是抽屉，只有收起（rail）与打开（open）两态，没有固定展开。
     宽窗偏好 sbPref（pinned / rail）只在宽窗写入；窄窗里的开合不改写它，回到宽窗时恢复。 */
  var sbNarrow = null;   /* matchMedia('(max-width: 1100px)')，initSidebar 建立 */
  var sbPref = null;     /* 已保存的宽窗偏好；未保存时为 null（宽窗默认固定展开） */
  function narrowViewport() { return !!(sbNarrow && sbNarrow.matches); }

  function setSidebar(mode) {
    var prev = sbMode(), narrow = narrowViewport();
    /* 窄窗没有固定展开：按打开的抽屉处理，仍可点外部/Esc 关闭 */
    if (narrow && mode === 'pinned') mode = 'open';
    if (mode !== 'open') hoverOpen = false;
    if (mode === 'rail' && prev !== 'rail') suppressHover = el('sidebar').matches(':hover');
    document.body.classList.toggle('sb-rail', mode === 'rail');
    document.body.classList.toggle('sb-open', mode === 'open');
    /* 窄栏 → 展开（悬停/图钉/快捷键同路）：文字交错淡入；几何等高保证无任何纵向位移 */
    if (prev === 'rail' && mode !== 'rail') playSidebarEnter();
    /* open 是临时浮层，宽窗只持久化 pinned / rail；兼容旧键值 expanded / collapsed */
    if (!narrow) savePref(mode === 'pinned' ? 'pinned' : 'rail');
    var pinned = narrow ? sbPref === 'pinned' : mode === 'pinned';
    var pin = el('sidebarPin');
    pin.classList.toggle('on', pinned);
    pin.setAttribute('aria-pressed', pinned ? 'true' : 'false');
    pin.title = pinned ? '取消固定（收为窄栏）' : '固定展开侧边栏';
    /* 收回窄栏时迁移焦点：窄窗窄栏整条隐藏，焦点迁到顶栏展开钮；宽窗迁到钉按钮 */
    if (mode === 'rail' && prev !== 'rail' && el('sidebar').contains(document.activeElement)) {
      focusMute = true;
      (narrow ? el('sidebarExpand') : pin).focus({preventScroll: true});
      focusMute = false;
    }
  }

  function savePref(pref) {
    sbPref = pref;
    try { localStorage.setItem('chanapp-sidebar', pref); } catch (_) {}
  }

  /* 按当前窗口宽度落实侧栏：窄窗收起抽屉，宽窗恢复偏好（未保存过默认固定展开） */
  function applySidebarForViewport() {
    setSidebar(narrowViewport() ? 'rail' : (sbPref || 'pinned'));
  }

  function toggleSidebar() { setSidebar(sbMode() === 'rail' ? 'open' : 'rail'); }

  function initSidebar() {
    sbNarrow = window.matchMedia('(max-width: 1100px)');
    try {
      var saved = localStorage.getItem('chanapp-sidebar');
      if (saved === 'pinned' || saved === 'expanded') sbPref = 'pinned';
      else if (saved === 'rail' || saved === 'collapsed') sbPref = 'rail';
    } catch (_) {}
    applySidebarForViewport();
    sbNarrow.addEventListener('change', applySidebarForViewport);
    el('sidebarPin').addEventListener('click', function () {
      /* 窄窗的图钉切换宽窗偏好，抽屉保持当前开合 */
      if (narrowViewport()) {
        savePref(sbPref === 'pinned' ? 'rail' : 'pinned');
        setSidebar(sbMode());
        return;
      }
      setSidebar(sbMode() === 'pinned' ? 'rail' : 'pinned');
    });
    el('sidebarExpand').addEventListener('click', toggleSidebar);
    /* 窄栏悬停自动浮动展开，移开自动收回（仅悬停触发的 open 才随鼠标收回） */
    el('sidebar').addEventListener('mousemove', function () {
      if (sbMode() !== 'rail' || suppressHover) return;
      setSidebar('open');
      hoverOpen = true;
    });
    el('sidebar').addEventListener('mouseleave', function () {
      suppressHover = false;
      if (hoverOpen && sbMode() === 'open') setSidebar('rail');
    });
    /* 键盘可达性：焦点进入窄栏同样展开；非悬停展开时焦点离开即收回 */
    el('sidebar').addEventListener('focusin', function () {
      if (focusMute) return;
      if (sbMode() === 'rail') setSidebar('open');
    });
    el('sidebar').addEventListener('focusout', function (ev) {
      if (sbMode() !== 'open' || hoverOpen) return;
      if (ev.relatedTarget && el('sidebar').contains(ev.relatedTarget)) return;
      setSidebar('rail');
    });
    document.addEventListener('keydown', function (ev) {
      var tag = ev.target && ev.target.tagName || '';
      if (ev.key === '[' && !/^(INPUT|TEXTAREA|SELECT)$/.test(tag) && !(ev.target && ev.target.isContentEditable)) {
        toggleSidebar();
      }
      if (ev.key === 'Escape' && sbMode() === 'open') { setSidebar('rail'); ev.stopImmediatePropagation(); }
    });
    /* 浮动展开时点外部收回窄栏；固定展开不自动收回 */
    document.addEventListener('mousedown', function (ev) {
      if (sbMode() !== 'open') return;
      if (ev.target.closest && (ev.target.closest('#sidebar') || ev.target.closest('#sidebarExpand'))) return;
      setSidebar('rail');
    });
    /* 窄栏空档填充钮：展开浮动层并把焦点交给搜索输入框（focusin 会冒泡，迁移期间抑制重开判断） */
    el('wlRailBtn').addEventListener('click', function () {
      suppressHover = false;
      setSidebar('open');
      var input = el('wlForm').querySelector('input');
      if (input) { focusMute = true; input.focus({preventScroll: true}); focusMute = false; }
    });
    /* 标签页切换/窗口失焦不派发鼠标事件，悬停展开态会悬置：显式收回窄栏并复位悬停抑制，
       指针仍在栏上时回来移动鼠标即可再次展开（新交互回合） */
    function reclaimHoverOpen() {
      if (sbMode() !== 'open') return;
      setSidebar('rail');
      suppressHover = false;
    }
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) reclaimHoverOpen();
    });
    window.addEventListener('blur', reclaimHoverOpen);
  }

  // ---------- 初始化 ----------

  initSidebar();

  /* 右栏依据卡与副图共享底部分带：cardsDock 高度跟随 subWrap，两条分隔线始终同高（桌面；窄屏块布局不同步） */
  (function syncCardsDockHeight() {
    var dock = el('cardsDock'), sub = el('subWrap');
    var mq = window.matchMedia('(min-width: 761px)');
    function sync() {
      dock.style.height = mq.matches ? Math.round(sub.getBoundingClientRect().height) + 'px' : '';
    }
    new ResizeObserver(sync).observe(sub);
    mq.addEventListener('change', sync);
    sync();
  })();
  el('refetchBtn').addEventListener('click', function () { refetchCurrent(); });
  el('themeBtn').addEventListener('click', function () {
    applyTheme(theme === 'light' ? 'dark' : 'light');
  });
  function syncRuleControl() {
    Array.prototype.forEach.call(el('ruleProfile').querySelectorAll('button'), function (b) {
      b.classList.toggle('active', b.dataset.profile === state.ruleProfile);
    });
    Array.prototype.forEach.call(el('signalScope').querySelectorAll('button'), function (b) {
      b.classList.toggle('active', b.dataset.scope === state.signalScope);
    });
    /* res-bar 只留一行只读摘要，两组 seg 收进「图层与规则」浮层；
       摘要随 seg 同函数刷新，任何 state 变更路径（含绕过 click 的）都不会脱节 */
    el('ruleSummary').textContent = ruleLabelText();
  }
  /* 成笔标准/提示范围的展示文案只有这一份映射：res-bar 摘要与详情副题共用 */
  function ruleLabelText() {
    var profile = state.ruleProfile === 'relaxed' ? '宽松' : '严格';
    var scope = state.signalScope === 'standard' ? '标准' : '扩展';
    return profile + '成笔 · ' + scope + '提示';
  }
  syncRuleControl();
  el('ruleProfile').addEventListener('click', function (e) {
    var b = e.target.closest('button');
    if (!b) return;
    var profile = b.dataset.profile;
    if (profile !== 'strict' && profile !== 'relaxed' || profile === state.ruleProfile) return;
    state.ruleProfile = profile;
    try { localStorage.setItem('chanapp-rule-profile', profile); } catch (_) {}
    reloadRuleSelection('成笔标准');
  });
  el('signalScope').addEventListener('click', function (e) {
    var b = e.target.closest('button');
    if (!b) return;
    var scope = b.dataset.scope;
    if (scope !== 'standard' && scope !== 'expanded' || scope === state.signalScope) return;
    state.signalScope = scope;
    try { localStorage.setItem('chanapp-signal-scope', scope); } catch (_) {}
    reloadRuleSelection('提示范围');
  });
  function reloadRuleSelection(label) {
    if (!state.code) { syncRuleControl(); return; }
    restoreRange = charts ? {code: state.code, freq: state.freq, range: charts.main.timeScale().getVisibleRange()} : null;
    analysisIdentity = null;
    pendingAnalysis = null; activeChartVersion = null; lastChartData = null;
    syncRuleControl();
    renderEvidence([]);
    el('resonance').innerHTML = '';
    ruleSwitchNote = label + '已切换，分析待更新…';
    el('aiPanel').innerHTML = '<div class="ai-note">' + ruleSwitchNote + '</div>';
    if (typeof closeAiPopup === 'function') closeAiPopup();
    el('center').classList.add('chart-reloading');
    load();
  }
  el('aiRefresh').onclick = function () { loadAnalysis({manual:true, refresh:true}); };
  // 依据卡浮层左右切换：按钮与 ArrowLeft/ArrowRight 键逐条翻看（越界由 showEvidence 忽略）
  function wireCardsNav() {
    el('cardsPrev').onclick = function () { showEvidence(evidenceIdx - 1); };
    el('cardsNext').onclick = function () { showEvidence(evidenceIdx + 1); };
    el('evidenceBtn').onclick = function () { openDetail('card', evidenceList[evidenceIdx], this); };
    el('cardsDock').addEventListener('keydown', function (ev) {
      if (ev.key === 'ArrowLeft') { ev.preventDefault(); showEvidence(evidenceIdx - 1); }
      else if (ev.key === 'ArrowRight') { ev.preventDefault(); showEvidence(evidenceIdx + 1); }
    });
  }
  wireMaSeg();
  wireSubSeg();
  wireChartPointer();
  wireCardsNav();

  // AI 完全分类标题行：点击（或聚焦后 Enter/Space）在右栏滑出详情层看全文；返回钮/Esc 关闭
  function wireAiPopup() {
    el('aiPanel').addEventListener('click', function (ev) {
      var row = ev.target.closest('.ai-row');
      if (row) openAiPopup(+row.dataset.i, row);
    });
    el('aiPanel').addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      var row = ev.target.closest('.ai-row');
      if (!row) return;
      ev.preventDefault();
      openAiPopup(+row.dataset.i, row);
    });
    var aiPop = el('aiPop');
    el('aiPopBack').onclick = closeAiPopup;
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && aiPop.classList.contains('open')) {
        closeAiPopup();
        ev.stopImmediatePropagation();  // 上层先消费 Esc：rulesPop 等下层浮层不同键连关
      }
    });
  }
  wireAiPopup();

  // 「图层与规则」浮层：摘要与软边框按钮同为入口，Esc/外点关闭，关闭后焦点归还按钮
  function wireRulesPopover() {
    var pop = el('rulesPop'), btn = el('rulesBtn'), summary = el('ruleSummary');
    function setOpen(open, refocus) {
      var restore = pop.contains(document.activeElement);
      pop.classList.toggle('open', open);
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      summary.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (!open && refocus && restore) btn.focus({ preventScroll: true });
    }
    function toggle() {
      var open = !pop.classList.contains('open');
      setOpen(open);
      if (open) pop.querySelector('button').focus({ preventScroll: true });
    }
    btn.addEventListener('click', toggle);
    summary.addEventListener('click', toggle);
    summary.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      ev.preventDefault();
      toggle();
    });
    /* 非模态：点浮层以外关闭，不吞这次点击（图表、共振条等下层控件照常响应）。
       焦点归还延后一拍：图表库自身的 mousedown 处理器与浏览器默认焦点迁移都在本
       处理之后执行；落点没交给别的控件（body/空/刚隐藏的浮层内）时交还按钮。 */
    document.addEventListener('mousedown', function (ev) {
      if (!pop.classList.contains('open')) return;
      var t = ev.target;
      if (t.closest && (t.closest('#rulesPop') || t.closest('#rulesBtn') || t.closest('#ruleSummary'))) return;
      setOpen(false);
      setTimeout(function () {
        var a = document.activeElement;
        if (!a || a === document.body || pop.contains(a)) btn.focus({ preventScroll: true });
      }, 0);
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Escape' || !pop.classList.contains('open')) return;
      if (el('aiPop').classList.contains('open') || !el('subSegPop').hidden) return;  // 上层浮层先消费 Esc（其 handler 已 stopImmediatePropagation，此处仅兜底）
      setOpen(false, true);
    });
  }
  wireRulesPopover();

  el('periodBtn').onclick = openPeriodDialog;
  el('periodSave').onclick = savePeriodPrefs;
  el('periodCancel').onclick = function () { el('periodDialog').close(); };
  el('periodDialog').addEventListener('cancel', function (event) {
    if (periodSaving || (periodPrefs && periodPrefs.notice)) event.preventDefault();
  });

  // ---------- 启动 ----------

  renderTabs();
  renderStatus();
  refreshSession();
  loadPeriodPrefs();
  var preselected = !!state.code;
  if (preselected) { load(); recordView(state.code, state.code); }  // URL ?code= 已给：先记录查看；图表由周期偏好守卫放行，watchlist 并发读取
  loadWatchlist({ skipLoad: preselected });
  loadQuotes();
  loadRecent();
})();
