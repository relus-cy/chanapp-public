"""Backup batch quote source. Prices/amounts retain each market's currency.

Recorded 2026-09-09: mainland amount[37] is ten-thousand currency units;
HK amount[37] is currency units. Market caps[44:46] use 100 million units.
HK has a distinct layout (notably [46] is an English name, not PB).
Unverified fundamental fields are deliberately unavailable.
"""
import math
import re
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from .throttle import throttle

SOURCE = 'baseline_backup'
_CODE = re.compile(r'(?:sh|sz)\d{6}|hk\d{5}')
_ROW = re.compile(r'v_((?:sh|sz)\d{6}|hk\d{5})="([^"\r\n]*)";')


def _validate(code):
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise ValueError('备用显示代码无效')
    return code


def _number(value, *, positive=False, nonnegative=False, scale=1):
    try:
        number = float(value) * scale
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (positive and number <= 0) or (nonnegative and number < 0):
        return None
    return number


def _timestamp(value, hk):
    try:
        parsed = datetime.strptime(value, '%Y/%m/%d %H:%M:%S' if hk else '%Y%m%d%H%M%S')
        stamp = parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        return stamp if 0 < stamp <= time.time() + 300 else None
    except (ValueError, OverflowError):
        return None


def _rows(text, codes):
    wanted = set(codes)
    rows, duplicates = {}, set()
    for code, raw in _ROW.findall(text):
        if code not in wanted:
            continue
        if code in rows:
            duplicates.add(code)
        rows[code] = raw.split('~')
    return {code: fields for code, fields in rows.items()
            if code not in duplicates and len(fields) >= 46 and fields[2] == code[2:]}


def _quote(code, fields):
    price = _number(fields[3], positive=True)
    prev = _number(fields[4], positive=True)
    stamp = _timestamp(fields[30], code.startswith('hk'))
    if price is None or prev is None or stamp is None or not fields[1].strip():
        return None
    pct = _number(fields[32])
    if pct is None or abs(pct - (price / prev - 1) * 100) > 0.06:
        return None
    ratio = None if code.startswith(('hk', 'sh000', 'sz399')) else (0.2 if code.startswith(('sh68', 'sz30')) else 0.1)
    return {'price': price, 'prev_close': prev, 'pct': pct, 'name': fields[1],
            'limit_up': bool(ratio and price >= round(prev * (1 + ratio), 2)),
            'source': SOURCE, 'source_ts': stamp}


def parse_quotes(text, codes):
    """Return only requested, valid rows; absent/ambiguous rows stay absent."""
    codes = [_validate(code) for code in codes]
    return {code: quote for code, fields in _rows(text, codes).items()
            if (quote := _quote(code, fields)) is not None}


def _fetch_text(codes):
    # Source retry/backoff belongs to the display coordinator, once per source.
    throttle(0.4)
    request = urllib.request.Request('https://qt.gtimg.cn/q=' + ','.join(codes),
                                     headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(request, timeout=10) as response:
        text = response.read().decode('gb18030')
    if not parse_quotes(text, codes):
        raise RuntimeError('备用显示批次无有效行情')
    return text


def fetch_quotes(codes):
    codes = list(dict.fromkeys(_validate(code) for code in codes))
    out, error = {}, None
    for start in range(0, len(codes), 80):
        batch = codes[start:start + 80]
        try:
            out.update(parse_quotes(_fetch_text(batch), batch))
        except Exception as exc:
            error = exc
    if not out and error is not None:
        raise error
    return out


def fetch_f10(code):
    code = _validate(code)
    fields = _rows(_fetch_text([code]), [code]).get(code)
    quote = _quote(code, fields) if fields else None
    if quote is None:
        raise RuntimeError('备用显示资料无有效行情')
    hk = code.startswith('hk')
    f10 = dict.fromkeys(('price', 'amount', 'outer', 'inner', 'volume_ratio',
                        'limit_up_price', 'limit_down_price', 'total_mv', 'float_mv',
                        'pe_ttm', 'pb', 'turnover', 'amplitude'))
    f10.update(price=quote['price'], industry='', concepts=[], board_code='',
               amount=_number(fields[37], nonnegative=True, scale=1 if hk else 10000),
               float_mv=_number(fields[44], positive=True, scale=100000000),
               total_mv=_number(fields[45], positive=True, scale=100000000))
    if not hk:
        f10.update(turnover=_number(fields[38], nonnegative=True),
                   amplitude=_number(fields[43], nonnegative=True))
        if len(fields) > 48:
            f10.update(pb=_number(fields[46], positive=True),
                       limit_up_price=_number(fields[47], positive=True),
                       limit_down_price=_number(fields[48], positive=True))
    return {'f10': f10, 'flow': dict.fromkeys(('main', 'super', 'large', 'medium', 'small')),
            'industry_pct': None, 'source': SOURCE, 'source_ts': quote['source_ts']}
