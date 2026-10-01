"""实例展示周期偏好：与部署配置分离，保存勾选和确认过的市场能力。

不依赖数据适配层。生命周期注入各市场的事实粒度，浏览器与采集器共享同一选择。
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import logging
import os
from pathlib import Path
import threading
import uuid

from chanapp.engine import atomic_file

PERIOD_LABELS = {'day': '日线', 'week': '周线', 'm60': '60分', 'm30': '30分'}
PERIODS = tuple(PERIOD_LABELS)
_MINUTES = {'m5': 5, 'm15': 15, 'm30': 30, 'm60': 60}
_LABELS = {'cn': 'A 股', 'hk': '港股'}
log = logging.getLogger(__name__)


def capabilities(minute_frequencies):
    result = {}
    for market, fact in minute_frequencies.items():
        market = market.lower()
        available, reasons = [], {}
        for freq in PERIODS:
            target, base = _MINUTES.get(freq), _MINUTES.get(fact)
            if target is None or (base and target >= base and target % base == 0):
                available.append(freq)
            else:
                source = f'粒度为 {fact}' if fact else '仅提供日线'
                reasons[freq] = f'当前{_LABELS.get(market, market)}{source}，无法显示 {target} 分'
        result[market] = {'fact_freq': fact, 'available': available, 'reasons': reasons}
    return result


def _write(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_file.replace(path, json.dumps(state, ensure_ascii=False).encode('utf-8'))


class Conflict(ValueError):
    """旧页面的偏好版本已过期。"""


class Preferences:
    def __init__(self, path, minute_frequencies, *, new_instance=False):
        self.path = Path(path)
        self.markets = capabilities(minute_frequencies)
        self._lock = threading.RLock()
        available = {f for m in self.markets.values() for f in m['available']}
        self._state = {'selected': [f for f in PERIODS if not new_instance or f in available],
                       'capabilities': {m: v['available'] for m, v in self.markets.items()},
                       'notice': {'kind': 'first_use', 'changes': []} if new_instance else None,
                       'revision': uuid.uuid4().hex}
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(saved, dict) or not isinstance(saved.get('selected'), list)
                    or any(f not in PERIODS for f in saved['selected'])
                    or not isinstance(saved.get('capabilities'), dict)
                    or not isinstance(saved.get('revision'), str)
                    or any(not isinstance(v, list) or any(f not in PERIODS for f in v)
                           for v in saved['capabilities'].values())
                    or 'notice' not in saved
                    or (saved['notice'] is not None and (not isinstance(saved['notice'], dict)
                        or saved['notice'].get('kind') not in ('first_use', 'capabilities_changed')
                        or not isinstance(saved['notice'].get('changes'), list)
                        or any(not isinstance(c, dict) or c.get('market') not in _LABELS
                               or c.get('freq') not in PERIODS or not isinstance(c.get('available'), bool)
                               for c in saved['notice']['changes'])))):
                raise ValueError('invalid preferences')
            self._state = saved
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeError):
            # 已有选择读不出：原文件留作证据，分钟暂停到页面重新确认，不替用户恢复耗额度的采集。
            log.warning('周期偏好不可读，原文件已保留；分钟周期暂停，待页面重新确认')
            try:
                os.replace(self.path, self.path.with_name(f'{self.path.name}.unreadable-{uuid.uuid4().hex[:8]}'))
            except OSError:
                pass
            self._state = {'selected': [f for f in PERIODS if f not in _MINUTES],
                           'capabilities': {m: v['available'] for m, v in self.markets.items()},
                           'notice': {'kind': 'first_use', 'changes': []}, 'revision': uuid.uuid4().hex}
        # 能力比较只认可合成周期：m5→m15 不应触发无意义提示。
        changes = []
        for market, value in self.markets.items():
            before = self._state['capabilities'].get(market, [])
            for freq in PERIODS:
                if (freq in before) != (freq in value['available']):
                    changes.append({'market': market, 'freq': freq, 'available': freq in value['available']})
        if changes:
            self._state['notice'] = {'kind': 'capabilities_changed', 'changes': changes}
            self._state['revision'] = uuid.uuid4().hex
        elif (self._state['notice'] or {}).get('kind') == 'capabilities_changed':
            self._state['notice'] = None
            self._state['revision'] = uuid.uuid4().hex
        try:
            _write(self.path, self._state)
        except OSError:
            log.warning('周期偏好暂不能保存；启动继续，页面保存会报告错误')

    @property
    def revision(self):
        with self._lock:
            return self._state['revision']

    def snapshot(self):
        with self._lock:
            return copy.deepcopy({k: self._state[k] for k in ('selected', 'notice', 'revision')} | {
                'catalog': [{'freq': f, 'label': PERIOD_LABELS[f]} for f in PERIODS],
                'markets': copy.deepcopy(self.markets)})

    def save(self, selected, revision):
        with self._lock:
            if revision != self._state['revision']:
                raise Conflict('周期偏好已更新，请重新确认')
            if any(f not in PERIODS for f in selected):
                raise ValueError('仅支持 30 分、60 分、日、周')
            available = {f for m in self.markets.values() for f in m['available']}
            if any(f not in available and f not in self._state['selected'] for f in selected):
                raise ValueError('不能新增当前所有市场都无法合成的周期')
            state = {'selected': [f for f in PERIODS if f in selected],
                     'capabilities': {m: v['available'] for m, v in self.markets.items()},
                     'notice': None, 'revision': uuid.uuid4().hex}
            _write(self.path, state)
            self._state = state
            return self.snapshot()

    def allows(self, code, freq):
        return freq in self.allowed(code, (freq,))

    def allowed(self, code, freqs):
        """同一偏好版本下 freqs 中可用的周期，保持输入顺序。"""
        market = 'hk' if code.lower().startswith('hk') else 'cn'
        with self._lock:
            return tuple(f for f in freqs
                         if f in self._state['selected'] and f in self.markets[market]['available'])

    def minute_enabled(self, code):
        return bool(self.allowed(code, ('m60', 'm30')))


_current = None


def current():
    return _current


@contextmanager
def activate(paths, minute_frequencies, *, new_instance=False):
    global _current
    previous = _current
    _current = Preferences(paths.period_prefs, minute_frequencies, new_instance=new_instance)
    try:
        yield _current
    finally:
        _current = previous


def preset(paths, minute_frequencies):
    """demo 初始化时在 activate 之前调用；只在文件缺席时预置四个勾选且无弹窗。

    minute_frequencies 例：{"CN": "m5", "HK": "m30"}；paths 由 instance_paths.resolve/current 返回。
    已有偏好不覆盖，后续改粒度仍由正常启动重检。
    """
    if not paths.period_prefs.exists():
        Preferences(paths.period_prefs, minute_frequencies, new_instance=False)
