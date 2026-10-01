"""Local service/collector checks. Never fetch quotes or repair data.

Run from the package parent: python -m chanapp.scripts.selfcheck.
Reads the loopback GET /api/status (collector.status) and the trading calendar
the collector exports to CHANAPP_CACHE_DIR/selfcheck_calendar.json.
ALERT/RECOVERED/NOTICE events go to stdout (systemd journal); status.json is the
current report, including pending/unconfirmed cases. The last seen binding_gen
per binding sits next to it in binding_gen.json. No outbound notification channel.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
ROOT = Path(__file__).resolve().parents[1]
CALENDAR_FILE = 'selfcheck_calendar.json'
BINDING_FILE = 'binding_gen.json'
# Minute datasets are judged stale by the collector as soon as a session opens
# (the last commit is from the previous session); give the first poll time to land.
OPENING_GRACE_S = 300
# Mirrors engine/kline/config.py FINALIZE_DEADLINE (per market): between the first open
# and this time, outside sessions, the collector reports stale=false without judging.
# Kept local so this timer process imports nothing from the engine.
FINALIZE_DEADLINE = {'cn': '21:00', 'hk': '18:30'}
REQUEST_TIMEOUT_S = 10
CODE_RE = re.compile(r'(sh|sz|hk)\d{5,6}')
# Issue keys '<prefix>:<detail>' that are not per-dataset keys.
GLOBAL_PREFIXES = ('cold_standby', 'binding_changed')


def stamp(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=ZONE) if parsed.tzinfo is None else parsed.astimezone(ZONE)


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _hhmm(value):
    return isinstance(value, str) and re.fullmatch(r'\d{2}:\d{2}', value) is not None


def _mmdd_list(value):
    return isinstance(value, list) and all(
        isinstance(d, str) and re.fullmatch(r'\d{2}-\d{2}', d) for d in value)


def load_calendar(path, now):
    """Collector export (engine/kline/calendar.py export_selfcheck), or None when
    missing, damaged, or for another year."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('year') != now.astimezone(ZONE).year:
        return None
    for market in ('cn', 'hk'):
        config = data.get(market)
        if (not isinstance(config, dict) or not _mmdd_list(config.get('closed')) or
                not _mmdd_list(config.get('half_days')) or
                not isinstance(config.get('sessions'), list) or not config['sessions'] or
                not all(isinstance(s, list) and len(s) == 2 and all(map(_hhmm, s))
                        for s in config['sessions'])):
            return None
    return data


def market_window(now, market, calendar):
    now = now.astimezone(ZONE)
    config = calendar[market]
    date = now.strftime('%m-%d')
    if now.weekday() >= 5 or date in config['closed']:
        return {'status': 'closed', 'trading_day': False, 'sessions': []}
    hours = config['sessions'][:1] if date in config['half_days'] else config['sessions']
    sessions = [(stamp(f'{now.date()}T{start}'), stamp(f'{now.date()}T{end}'))
                for start, end in hours]
    current = next(((start, end) for start, end in sessions if start <= now <= end), None)
    return {'status': 'open' if current else 'closed', 'trading_day': True, 'sessions': sessions,
            'opened_at': current[0] if current else None}


def judgement(now, market, dataset, calendar):
    """Whether the collector's stale flag for this dataset is meaningful right now.

    'judged': trust stale either way. 'grace': a session just opened, the minute
    dataset has not had its first poll. 'unjudged': the collector reports
    stale=false without checking (see views.dataset_stale): the day dataset in
    session, and any dataset on a trading day between the first open and
    FINALIZE_DEADLINE outside sessions."""
    window = market_window(now, market, calendar)
    if window['status'] == 'open':
        if dataset == 'day':
            return 'unjudged'  # today's day bar comes from minutes; not checked in session
        if (now - window['opened_at']).total_seconds() <= OPENING_GRACE_S:
            return 'grace'
        return 'judged'
    if window['trading_day']:
        hhmm = now.astimezone(ZONE).strftime('%H:%M')
        if window['sessions'][0][0] <= now and hhmm < FINALIZE_DEADLINE[market]:
            return 'unjudged'
    return 'judged'


def fetch_status(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.username:
        raise ValueError('selfcheck requires a loopback HTTP URL')
    # Ignore proxy environment variables: this request must stay on the host.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url.rstrip('/') + '/api/status', timeout=REQUEST_TIMEOUT_S) as response:
        return validate_status(json.load(response))


def validate_status(data):
    """Top-level shape must hold; a malformed dataset row is flagged per row."""
    if (not isinstance(data, dict) or type(data.get('enabled')) is not bool or
            not isinstance(data.get('datasets'), list) or not isinstance(data.get('probes'), list)):
        raise ValueError('invalid status response')
    for row in data['datasets']:
        if (not isinstance(row, dict) or not isinstance(row.get('code'), str) or
                not isinstance(row.get('dataset'), str)):
            raise ValueError('invalid status dataset')
    for probe in data['probes']:
        if (not isinstance(probe, dict) or
                not all(isinstance(probe.get(k), str) for k in ('market', 'kind', 'item')) or
                type(probe.get('binding_gen')) is not int or
                not (probe.get('keepalive') is None or isinstance(probe['keepalive'], dict))):
            raise ValueError('invalid status probe')
    return data


def binding_key(probe):
    return f"{probe['market']}/{probe['kind']}/{probe['item']}"


def load_bindings(path):
    """Last seen binding_gen per binding; {} when missing or damaged (rebuild silently)."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or any(type(v) is not int for v in data.values()):
        return {}
    return data


def read_watchlist(path):
    items = json.loads(Path(path).read_text())
    if not isinstance(items, list):
        raise ValueError('watchlist must be a list')
    codes = sorted({item['code'] for item in items})
    if any(not isinstance(code, str) or not CODE_RE.fullmatch(code) for code in codes):
        raise ValueError('invalid watchlist')
    return codes


def assess_dataset(row, now, calendar):
    code, dataset = row['code'], row['dataset']
    market = 'hk' if code.startswith('hk') else 'cn'
    out = {'code': code, 'dataset': dataset, 'market': market, 'issues': [],
           **{k: row.get(k) for k in ('stale', 'stale_age_s', 'last_commit_at',
                                      'open_gaps', 'known_gaps', 'pending_review')}}
    if type(row.get('stale')) is not bool:
        out['judgement'] = 'invalid'
        out['issues'].append('status_invalid')
        return out
    out['judgement'] = 'unknown' if calendar is None else judgement(now, market, dataset, calendar)
    # Freshness is the collector's commit-based stale (spec 6.5): a suspended
    # stock whose polls still commit is not stale, whatever its last bar date.
    if row['stale'] and out['judgement'] == 'judged':
        out['issues'].append('stale')
    return out


def collect(root, watchlist, url, now, bindings):
    report = {'checked_at': now.isoformat(), 'issues': [], 'deferred': [], 'rows': [],
              'codes': [], 'probes': []}
    try:
        service = subprocess.run(['systemctl', 'is-active', '--quiet', 'chanapp'], timeout=4)
        if service.returncode:
            raise RuntimeError('service inactive')
        status = fetch_status(url)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        report['issues'].append('service_unavailable')
        report['deferred'].append('*')
        return report
    report['mode'] = status.get('mode')
    if report['mode'] != 'real':
        report['issues'].append('instance_mode_invalid')
        report['deferred'].append('*')
        return report
    report['enabled'] = status['enabled']
    if not status['enabled']:
        report['issues'].append('collector_stopped')
    gens = {}
    for probe in status['probes']:
        key = binding_key(probe)
        gens[key] = probe['binding_gen']
        keepalive = probe['keepalive'] or {}
        report['probes'].append({'binding': key, 'active': probe.get('active'),
                                 'binding_gen': probe['binding_gen'],
                                 'keepalive': keepalive.get('verdict')})
        if keepalive.get('verdict') == 'fail':
            report['issues'].append(f'cold_standby:{key}')
        elif keepalive.get('verdict') not in ('pass', 'waived'):
            report['deferred'].append(f'cold_standby:{key}')
        if key in bindings and bindings[key] != probe['binding_gen']:
            report['issues'].append(f'binding_changed:{key}')
    report['binding_gens'] = gens
    try:
        codes = read_watchlist(watchlist)
    except (OSError, ValueError, TypeError, KeyError):
        report['issues'].append('watchlist_unavailable')
        report['deferred'].append('*')
        return report
    report['codes'] = codes
    calendar = load_calendar(Path(root) / CALENDAR_FILE, now)
    if calendar is None:
        report['issues'].append('calendar_unavailable')
    seen = set()
    for row in status['datasets']:
        if row['code'] not in codes:
            continue  # watchlist edited between the service read and ours
        seen.add(row['code'])
        result = assess_dataset(row, now, calendar)
        pair = f"{row['code']}/{row['dataset']}"
        report['rows'].append(result)
        if result['judgement'] != 'judged':
            report['deferred'].append(pair)
        report['issues'].extend(f'{pair}:{issue}' for issue in result['issues'])
    for code in codes:
        if code not in seen:
            report['issues'].append(f'{code}:status_missing')
            report['deferred'].append(code)
    return report


def load_state(path):
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if (not isinstance(state, dict) or not isinstance(state.get('active'), list) or
            not all(isinstance(key, str) for key in state['active']) or
            not isinstance(state.get('counts'), dict) or
            any(not isinstance(key, str) or type(n) is not int or n < 0 for key, n in state['counts'].items())):
        raise ValueError('invalid selfcheck state')
    stamp(state['checked_at'])
    return state


def _still_unknown(key, report):
    """True when this run cannot confirm that a previously active issue is gone."""
    if key == 'instance_mode_invalid' and report.get('mode') == 'real':
        return False
    deferred = report['deferred']
    if '*' in deferred:
        return key != 'service_unavailable'
    if key in deferred:
        return True
    if ':' not in key:
        return False
    prefix = key.split(':', 1)[0]
    if prefix in GLOBAL_PREFIXES:
        return False  # an unproven keepalive defers its exact issue key (above)
    code = prefix.split('/', 1)[0]
    return prefix in deferred or code in deferred


def update_state(previous, report):
    current = set(report['issues'])
    old_active = set(previous.get('active', []))
    gap = ((stamp(report['checked_at']) - stamp(previous['checked_at'])).total_seconds()
           if previous else 0)
    old_counts = previous.get('counts', {}) if 0 < gap <= 180 else {}
    counts = {key: min(2, old_counts.get(key, 0) + 1) for key in current}
    active = {key for key, count in counts.items() if count >= 2} | (old_active & current)
    rows = {f"{row['code']}/{row['dataset']}" for row in report['rows']}
    codes = set(report.get('codes', []))
    retired = set()
    for key in old_active - current:
        if _still_unknown(key, report):
            active.add(key)
            continue
        prefix = key.split(':', 1)[0] if ':' in key else None
        if prefix is None or prefix in GLOBAL_PREFIXES:
            continue  # recovered
        if prefix.split('/', 1)[0] not in codes or ('/' in prefix and prefix not in rows):
            retired.add(key)  # watchlist item or dataset gone: no longer checked
    # A binding switch is a one-shot fact; it would never reach two consecutive
    # observations, so it is logged on sight instead of waiting to be confirmed.
    notices = [{'event': 'NOTICE', 'issue': key} for key in sorted(current)
               if key.startswith('binding_changed:') and key not in active]
    events = ([{'event': 'ALERT', 'issue': key} for key in sorted(active - old_active)] +
              [{'event': 'RECOVERED', 'issue': key} for key in sorted(old_active - active - retired)] +
              [{'event': 'RETIRED', 'issue': key} for key in sorted(retired)] + notices)
    return {**report, 'counts': counts, 'active': sorted(active), 'events': events,
            'status': 'degraded' if active else 'pending' if current else 'ok'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8899')
    parser.add_argument('--output', type=Path, default=ROOT / '.runtime/selfcheck/status.json')
    args = parser.parse_args(argv)
    now = datetime.now(ZONE)
    root = Path(os.environ.get('CHANAPP_CACHE_DIR') or ROOT / '.cache')
    watchlist = Path(os.environ.get('WATCHLIST_PATH') or ROOT / 'watchlist.json')
    binding_path = args.output.with_name(BINDING_FILE)
    try:
        previous = load_state(args.output)
    except (OSError, ValueError, TypeError, KeyError):
        previous = {}
        print('SELFCHECK_STATE_INVALID rebuilding monitor state', flush=True)
    report = collect(root, watchlist, args.url, now, load_bindings(binding_path))
    state = update_state(previous, report)
    atomic_json(args.output, state)
    if report.get('binding_gens'):
        atomic_json(binding_path, report['binding_gens'])
    if not previous:
        print('SELFCHECK_STARTED status=' + state['status'], flush=True)
    for event in state['events']:
        print(json.dumps(event, ensure_ascii=False), flush=True)
    # Nonzero is reserved for checker execution failure; a detected incident is
    # a successful check, persisted in active/status and logged on transition.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
