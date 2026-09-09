"""Map raw bars to the fixed native single-level calculation core."""
from datetime import datetime
import math

from .chanpy_profiles import make_config, profile_identity
from .chanpy_vendor.Common.CEnum import DATA_FIELD, FX_TYPE, KL_TYPE
from .chanpy_vendor.Common.CTime import CTime
from .chanpy_vendor.KLine.KLine_List import CKLine_List
from .chanpy_vendor.KLine.KLine_Unit import CKLine_Unit

FREQUENCIES = {'day': KL_TYPE.K_DAY, 'm60': KL_TYPE.K_60M, 'm30': KL_TYPE.K_30M,
               'm15': KL_TYPE.K_15M, 'm5': KL_TYPE.K_5M}


def build_native(bars, freq='day', rule_profile='strict', signal_scope='expanded'):
    if freq not in FREQUENCIES:
        raise ValueError(f'unsupported freq: {freq}')
    native = CKLine_List(FREQUENCIES[freq], make_config(rule_profile, signal_scope))
    previous = None
    previous_dt = None
    for idx, bar in enumerate(bars):
        try:
            dt = datetime.strptime(bar['dt'], '%Y-%m-%d %H:%M' if ' ' in bar['dt'] else '%Y-%m-%d')
            values = {key: float(bar[key]) for key in ('open', 'high', 'low', 'close')}
            volume = float(bar.get('volume', 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f'invalid bar at index {idx}') from exc
        if previous_dt is not None and dt <= previous_dt:
            raise ValueError(f'bar times must be strictly increasing at index {idx}')
        if not all(math.isfinite(v) and v > 0 for v in values.values()) or not math.isfinite(volume) or volume < 0:
            raise ValueError(f'invalid numeric bar at index {idx}')
        if values['low'] > min(values.values()) or values['high'] < max(values.values()):
            raise ValueError(f'invalid OHLC at index {idx}')
        unit = CKLine_Unit({DATA_FIELD.FIELD_TIME: CTime(dt.year, dt.month, dt.day, dt.hour, dt.minute, auto=False),
                           **values, DATA_FIELD.FIELD_VOLUME: volume})
        unit.set_idx(idx)
        unit.kl_type = FREQUENCIES[freq]
        unit.set_pre_klu(previous)
        native.add_single_klu(unit)
        previous, previous_dt = unit, dt
    return native


def normalize_line(line, bars):
    x0, x1 = line.get_begin_klu().idx, line.get_end_klu().idx
    return dict(index=line.idx, x0=x0, x1=x1, y0=float(line.get_begin_val()), y1=float(line.get_end_val()),
                dt0=bars[x0]['dt'], dt1=bars[x1]['dt'], direction='up' if line.is_up() else 'down',
                forming=not line.is_sure, status='confirmed' if line.is_sure else 'provisional')


def _parent_context(point, parents):
    parent = getattr(point.bi, 'parent_seg', None)
    if parent is None:
        index = getattr(point.bi, 'seg_idx', None)
        parent = next((segment for segment in parents if segment.idx == index), None)
    return dict(parent_segment_index=None if parent is None else parent.idx,
                zs_count=None if parent is None else len(parent.zs_lst))


def normalize_points(point_list, bars, level, config, parents=()):
    result = []
    for point in point_list.getSortedBspList():
        x = point.klu.idx
        confirmed = bool(point.bi.is_sure and x <= point_list.last_sure_pos)
        types = list(dict.fromkeys(t.value for t in point.type))
        side = 'buy' if point.is_buy else 'sell'
        prefix = 'B' if point.is_buy else 'S'
        relation = point.relate_bsp1
        relation_data = None if relation is None else dict(index=relation.bi.idx, x=relation.klu.idx,
            dt=bars[relation.klu.idx]['dt'], price=float(relation.bi.get_end_val()), types=[t.value for t in relation.type])
        point_config = config.b_conf if point.is_buy else config.s_conf
        context = _parent_context(point, parents)
        related_context = None if relation is None else _parent_context(relation, parents)
        is_first = any(t in ('1', '1p') for t in types)
        origin_context = context if is_first else related_context
        count = None if origin_context is None else origin_context['zs_count']
        context.update(origin='unavailable' if count is None else 'zero_center' if count == 0 else 'centered',
                       origin_source='self' if is_first else 'related_bsp1' if relation is not None else 'unavailable',
                       related_bsp1_context=related_context)
        ratio = dict(point.features.items()).get('divergence_rate')
        finite = isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and math.isfinite(ratio)
        strength = dict(metric=point_config.macd_algo.name.lower(), value=float(ratio) if finite else None,
                        state='unavailable' if not finite else 'weaker' if ratio < 1 else 'stronger' if ratio > 1 else 'equal')
        result.append(dict(x=x, dt=bars[x]['dt'], price=float(point.bi.get_end_val()), side=side,
            level=level, types=types, label=('段:' if level == 'seg' else '') + '/'.join(prefix+t for t in types),
            status='confirmed' if confirmed else 'provisional', forming=not confirmed,
            structure_ref=dict(level=level, index=point.bi.idx), related_bsp1=relation_data,
            context=context, strength=strength,
            features={k: v if not isinstance(v, float) or math.isfinite(v) else None for k,v in point.features.items()},
            macd_algo=point_config.macd_algo.name.lower(), last_sure_pos=point_list.last_sure_pos))
    return result


def extract_structure(native, bars, rule_profile='strict', signal_scope='expanded'):
    def zones(items):
        return [dict(x0=z.begin.idx, x1=z.end.idx, dt0=bars[z.begin.idx]['dt'], dt1=bars[z.end.idx]['dt'],
                     zg=float(z.high), zd=float(z.low), gg=float(z.peak_high), dd=float(z.peak_low),
                     forming=not z.is_sure, status='confirmed' if z.is_sure else 'provisional') for z in items]
    bi = [normalize_line(line, bars) for line in native.bi_list]
    xd = [normalize_line(line, bars) for line in native.seg_list]
    zs, zs_xd = zones(native.zs_list), zones(native.segzs_list)
    points = normalize_points(native.bs_point_lst, bars, 'bi', native.config.bs_point_conf, native.seg_list)
    points += normalize_points(native.seg_bs_point_lst, bars, 'seg', native.config.seg_bs_point_conf, native.segseg_list)
    points.sort(key=lambda s: (s['x'], s['level'], s['side']))
    units = list(native.klu_iter())
    return dict(bi=bi, xd=xd, zs=zs, zs_xd=zs_xd,
        counts=dict(cl_kline=len(native), fx=sum(k.fx != FX_TYPE.UNKNOWN for k in native),
                    bi=len(bi), xd=len(xd), bi_zs=len(zs), xd_zs=len(zs_xd)),
        **profile_identity(rule_profile, signal_scope),
        _native_signals=points,
        _native_macd=dict(dif=[k.macd.DIF for k in units], dea=[k.macd.DEA for k in units], hist=[k.macd.macd for k in units]))
