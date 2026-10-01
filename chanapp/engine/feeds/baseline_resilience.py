"""Baseline display merging; source age is informational, never cache expiry."""
import math
import time

SOURCE_AGE_SECONDS = 900
BASIC_FIELDS = ('price', 'amount', 'outer', 'inner', 'volume_ratio',
                'limit_up_price', 'limit_down_price', 'total_mv', 'float_mv',
                'pe_ttm', 'pb', 'turnover', 'amplitude')


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def price(value):
    value = number(value)
    return value if value is not None and value > 0 else None


def field_value(key, value):
    value = number(value)
    if value is None:
        return None
    if key in ("price", "limit_up_price", "limit_down_price", "total_mv", "float_mv"):
        return value if value > 0 else None
    if key not in ("pe_ttm", "pb") and value < 0:
        return None
    return value


def stamp(row, source):
    row = dict(row)
    row['source'] = row.get('source') or source
    ts = number(row.get('source_ts'))
    row['source_ts'] = ts if ts is not None and 0 < ts <= time.time() + 300 else None
    row['source_stale'] = bool(row['source_ts'] and time.time() - row['source_ts'] > SOURCE_AGE_SECONDS)
    row['source_time_unknown'] = row['source_ts'] is None
    return row


def quote_rows(rows, codes, source):
    result = {}
    for code in codes:
        row = rows.get(code)
        if not isinstance(row, dict) or price(row.get('price')) is None:
            continue
        prev = price(row.get('prev_close'))
        pct = number(row.get('pct'))
        if prev is None or pct is None or abs((row['price'] / prev - 1) * 100 - pct) > 0.1:
            continue
        row = stamp(row, source)
        row['price'] = price(row['price'])
        row['prev_close'] = price(row.get('prev_close'))
        row['pct'] = number(row.get('pct'))
        result[code] = row
    return result


def metadata(rows, unavailable=()):
    rows = [stamp(row, 'baseline_display') for row in rows]
    times = [r.get('source_ts') for r in rows if number(r.get('source_ts')) is not None]
    return {'sources': sorted({r.get('source', 'baseline_display') for r in rows}),
            'source_ts': min(times) if times else None,
            'source_stale': any(stamp(r, 'baseline_display')['source_stale'] for r in rows),
            'source_time_unknown': not rows or any(not r.get('source_ts') for r in rows),
            'unavailable': list(unavailable)}


def merge_f10(primary, backup):
    primary = primary or {}
    backup = backup or {}
    f10 = dict(primary.get('f10') or {})
    field_sources = {}
    for key in BASIC_FIELDS:
        f10[key] = field_value(key, f10.get(key))
        replacement = field_value(key, (backup.get('f10') or {}).get(key))
        if f10[key] is None and replacement is not None:
            f10[key] = replacement
            field_sources[key] = 'baseline_backup'
    # Sector, concepts and money flow belong to this fetch only. Never reuse old data.
    f10.setdefault('industry', '')
    f10.setdefault('concepts', [])
    f10.setdefault('board_code', '')
    flow = {key: number((primary.get('flow') or {}).get(key))
            for key in ('main', 'super', 'large', 'medium', 'small')}
    unavailable = [key for key in ('industry', 'concepts') if not f10.get(key)]
    if not all(value is not None for value in flow.values()):
        unavailable.append('flow')
    industry_pct = number(primary.get('industry_pct'))
    if industry_pct is None:
        unavailable.append('industry_pct')
    rows = ([stamp(primary, 'baseline_display')] if primary else [])
    if field_sources:
        rows.append(stamp(backup, 'baseline_backup'))
    meta = metadata(rows, unavailable)
    meta['flow_note'] = '资金流暂不可用' if 'flow' in unavailable else ''
    meta['field_sources'] = field_sources
    meta['source_times'] = {row['source']: row['source_ts'] for row in rows}
    return {'f10': f10, 'flow': flow, 'industry_pct': industry_pct, 'meta': meta,
            'flow_note': '资金流暂不可用' if 'flow' in unavailable else '',
            'source_ts': meta['source_ts']}


def refresh_f10_metadata(result):
    result = dict(result)
    meta = dict(result.get('meta') or {})
    if not meta:
        result['meta'] = merge_f10(result, {})['meta']
        return result
    if meta:
        source_times = meta.get('source_times') or {source: meta.get('source_ts') for source in meta.get('sources', [])}
        meta.update(metadata(({'source': source, 'source_ts': ts} for source, ts in source_times.items()), meta.get('unavailable', [])))
        result['meta'] = meta
    return result
