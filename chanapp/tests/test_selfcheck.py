"""Internal checks read collector status and the exported calendar; never market data."""
import json
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from chanapp.scripts import selfcheck

URL = 'http://127.0.0.1:8899'
# Same shape as engine/kline/calendar.py export_selfcheck.
CALENDAR = {
    'year': 2026, 'sources': {'cn': 'facts.calendar', 'hk': 'facts.calendar'},
    'cn': {'closed': ['10-01', '10-02'], 'half_days': [],
           'sessions': [['09:30', '11:30'], ['13:00', '15:00']]},
    'hk': {'closed': ['10-01'], 'half_days': ['12-24'],
           'sessions': [['09:30', '12:00'], ['13:00', '16:00']]},
}


def at(value):
    return datetime.fromisoformat(value + '+08:00')


def clock(moment):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment
    return Fixed


def dataset(code='sh600000', name='m5', stale=False, stale_judged=True, **extra):
    return {'code': code, 'dataset': name, 'last_commit_at': '2026-09-14T10:29:30+08:00',
            'stale': stale, 'stale_age_s': 400 if stale else None, 'stale_judged': stale_judged,
            'open_gaps': 0, 'known_gaps': 0, 'pending_review': 0, **extra}


def probe(gen=1, keepalive=None, market='CN', kind='stock', item='day'):
    return {'market': market, 'kind': kind, 'item': item, 'active': 'primary', 'binding_gen': gen,
            'probe': None, 'keepalive': keepalive}


def status(datasets=(), probes=(), enabled=True):
    return {'checked_at': '2026-09-14T10:30:00+08:00', 'enabled': enabled, 'mode': 'real',
            'datasets': list(datasets), 'probes': list(probes), 'budget': {},
            'calendar_export': '/cache/selfcheck_calendar.json'}


class SelfcheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = at('2026-09-14T10:30:00')
        (self.root / selfcheck.CALENDAR_FILE).write_text(json.dumps(CALENDAR))
        self.watchlist = self.root / 'watchlist.json'
        self.watchlist.write_text(json.dumps([{'code': 'sh600000'}]))

    def run_collect(self, payload, now=None, bindings=None, service_rc=0):
        with patch.object(selfcheck.subprocess, 'run', return_value=SimpleNamespace(returncode=service_rc)), \
                patch.object(selfcheck, 'fetch_status', return_value=selfcheck.validate_status(payload)):
            return selfcheck.collect(self.root, self.watchlist, URL, now or self.now, bindings or {})

    # --- calendar (exported by the collector) ---

    def test_exported_calendar_closes_holidays_lunch_weekends_and_half_days(self):
        calendar = selfcheck.load_calendar(self.root / selfcheck.CALENDAR_FILE, self.now)
        for market, moment in [('cn', '2026-10-01T10:30:00'), ('cn', '2026-09-14T12:00:00'),
                               ('hk', '2026-09-14T12:30:00'), ('hk', '2026-12-24T14:00:00'),
                               ('cn', '2026-09-20T10:30:00')]:
            with self.subTest(market=market, moment=moment):
                self.assertEqual(selfcheck.market_window(at(moment), market, calendar)['status'], 'closed')
        self.assertEqual(selfcheck.market_window(at('2026-10-02T10:30:00'), 'hk', calendar)['status'], 'open')

    def test_missing_damaged_or_other_year_calendar_is_unavailable(self):
        path = self.root / selfcheck.CALENDAR_FILE
        self.assertIsNone(selfcheck.load_calendar(path, at('2027-01-04T10:30:00')))
        for content in ('{', json.dumps({**CALENDAR, 'cn': {'closed': ['10/01']}}), json.dumps([])):
            path.write_text(content)
            self.assertIsNone(selfcheck.load_calendar(path, self.now), content)
        path.unlink()
        self.assertIsNone(selfcheck.load_calendar(path, self.now))
        result = self.run_collect(status([dataset(stale=True)]))
        self.assertIn('calendar_unavailable', result['issues'])
        # Without a calendar the stale flag cannot be timed: neither alarm nor recovery.
        self.assertNotIn('sh600000/m5:stale', result['issues'])
        self.assertIn('sh600000/m5', result['deferred'])

    # --- freshness comes from the collector's commit-based stale ---

    def test_suspension_is_not_outage(self):
        # A suspended stock keeps committing (suspended rows), so the collector
        # reports stale=false even though no new traded bar exists.
        row = dataset(stale=False, last_commit_at='2026-09-14T10:29:50+08:00')
        result = self.run_collect(status([row, dataset(name='day', stale_judged=False)]))
        self.assertEqual(result['issues'], [])
        self.assertNotIn('sh600000/m5', result['deferred'])  # judged, and healthy

    def test_stale_in_session_is_reported_per_dataset(self):
        result = self.run_collect(status([dataset(stale=True), dataset(name='day', stale_judged=False)]))
        self.assertEqual(result['issues'], ['sh600000/m5:stale'])

    def test_opening_grace_and_unjudged_windows_neither_alarm_nor_recover(self):
        # stale_judged=False 的行（采集器当前时段不判）与开盘宽限一样：不告警也不恢复
        for moment, judged in (('2026-09-14T09:32:00', True), ('2026-09-14T13:03:00', True),   # grace after open
                               ('2026-09-14T12:00:00', False), ('2026-09-14T16:00:00', False)):  # lunch, before deadline
            with self.subTest(moment=moment):
                result = self.run_collect(status([dataset(stale=True, stale_judged=judged)]),
                                          now=at(moment))
                self.assertEqual(result['issues'], [])
                self.assertEqual(result['deferred'], ['sh600000/m5'])
        # After the finalize deadline and on closed days the collector judges and reports stale_judged=true.
        for moment in ('2026-09-14T21:30:00', '2026-10-01T10:30:00'):
            result = self.run_collect(status([dataset(stale=True)]), now=at(moment))
            self.assertEqual(result['issues'], ['sh600000/m5:stale'], moment)

    def test_stale_judged_flag_drives_alarm(self):
        # 定稿时点与截止之间：采集器报 stale_judged=false → 暂缓（不告警也不恢复）；判了才告警
        before = self.run_collect(status([dataset(stale=True, stale_judged=False)]),
                                  now=at('2026-09-14T20:59:00'))
        self.assertEqual((before['issues'], before['deferred']), ([], ['sh600000/m5']))
        after = self.run_collect(status([dataset(stale=True)]), now=at('2026-09-14T21:00:00'))
        self.assertEqual(after['issues'], ['sh600000/m5:stale'])
        self.watchlist.write_text(json.dumps([{'code': 'hk00700'}]))
        hk = self.run_collect(status([dataset(code='hk00700', name='m30', stale=True)]),
                              now=at('2026-09-14T18:30:00'))
        self.assertEqual(hk['issues'], ['hk00700/m30:stale'])

    def test_unjudged_window_keeps_previous_stale_alert(self):
        key = 'sh600000/m5:stale'
        old = {'checked_at': at('2026-09-14T11:59:00').isoformat(), 'active': [key], 'counts': {key: 2}}
        lunch = self.run_collect(status([dataset(stale=False, stale_judged=False)]),
                                 now=at('2026-09-14T12:00:00'))
        state = selfcheck.update_state(old, lunch)
        self.assertEqual(state['active'], [key])
        self.assertEqual(state['events'], [])
        # An unfinalized-day alert from last evening is not cleared by the next session:
        # in session the collector does not judge the day dataset.
        day_key = 'sh600000/day:stale'
        old = {'checked_at': at('2026-09-15T09:39:00').isoformat(), 'active': [day_key], 'counts': {day_key: 2}}
        morning = self.run_collect(status([dataset(name='day', stale=False, stale_judged=False),
                                           dataset()]),
                                   now=at('2026-09-15T09:40:00'))
        state = selfcheck.update_state(old, morning)
        self.assertEqual(state['active'], [day_key])
        self.assertEqual(state['events'], [])

    # --- malformed service answers ---

    def test_bad_status_response_is_service_failure(self):
        for payload in ([], {}, {**status(), 'enabled': 'yes'}, {**status(), 'datasets': {}},
                        status([{'code': 1, 'dataset': 'day'}]),
                        status(probes=[{**probe(), 'binding_gen': '1'}])):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                selfcheck.validate_status(payload)
        with patch.object(selfcheck.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
            for error in (ValueError('invalid status response'),
                          urllib.error.HTTPError(URL, 503, 'no status', {}, None),
                          urllib.error.URLError('refused')):
                with patch.object(selfcheck, 'fetch_status', side_effect=error):
                    result = selfcheck.collect(self.root, self.watchlist, URL, self.now, {})
                self.assertEqual(result['issues'], ['service_unavailable'], error)
                self.assertEqual(result['deferred'], ['*'])

    def test_non_loopback_url_is_refused_without_request(self):
        with patch.object(selfcheck.urllib.request, 'build_opener', side_effect=AssertionError):
            for url in ('https://127.0.0.1:8899', 'http://192.0.2.1:8899', 'http://u@127.0.0.1:1'):
                with self.assertRaises(ValueError):
                    selfcheck.fetch_status(url)

    def test_invalid_row_cannot_resolve_previous_stale_fault(self):
        key = 'sh600000/m5:stale'
        old = {'checked_at': self.now.isoformat(), 'active': [key], 'counts': {}}
        result = self.run_collect(status([dataset(stale=None)]), now=at('2026-09-14T10:31:00'))
        self.assertEqual(result['issues'], ['sh600000/m5:status_invalid'])
        state = selfcheck.update_state(old, result)
        self.assertIn(key, state['active'])
        self.assertEqual(state['events'], [])
        row = dataset()
        del row['stale_judged']
        result = self.run_collect(status([row]), now=at('2026-09-14T10:31:00'))
        self.assertEqual(result['issues'], ['sh600000/m5:status_invalid'])

    def test_valid_fresh_row_really_resolves_previous_stale_fault(self):
        key = 'sh600000/m5:stale'
        old = {'checked_at': self.now.isoformat(), 'active': [key], 'counts': {}}
        result = self.run_collect(status([dataset(stale=False)]), now=at('2026-09-14T10:31:00'))
        state = selfcheck.update_state(old, result)
        self.assertEqual(state['events'], [{'event': 'RECOVERED', 'issue': key}])

    def test_watched_code_missing_from_status_is_not_empty_success(self):
        result = self.run_collect(status([]))
        self.assertEqual(result['issues'], ['sh600000:status_missing'])
        key = 'sh600000/m5:stale'
        state = selfcheck.update_state({'checked_at': self.now.isoformat(), 'active': [key], 'counts': {}},
                                       {**result, 'checked_at': at('2026-09-14T10:31:00').isoformat()})
        self.assertIn(key, state['active'])

    def test_real_mode_required_and_demo_cannot_resolve_data_faults(self):
        stale = 'sh600000/m5:stale'
        old = {'checked_at': self.now.isoformat(), 'active': [stale], 'counts': {}}
        for mode in ('demo', None, 'unexpected'):
            with self.subTest(mode=mode):
                payload = status([dataset()])
                if mode is None:
                    payload.pop('mode')
                else:
                    payload['mode'] = mode
                report = self.run_collect(payload, now=at('2026-09-14T10:31:00'))
                self.assertIn('instance_mode_invalid', report['issues'])
                self.assertEqual(report['deferred'], ['*'])
                self.assertIn(stale, selfcheck.update_state(old, report)['active'])
        old = {'checked_at': self.now.isoformat(), 'active': ['instance_mode_invalid'], 'counts': {}}
        report = self.run_collect(status([dataset()]), now=at('2026-09-14T10:31:00'))
        self.assertEqual(selfcheck.update_state(old, report)['events'],
                         [{'event': 'RECOVERED', 'issue': 'instance_mode_invalid'}])

    def test_real_mode_recovers_even_if_watchlist_is_unavailable(self):
        self.watchlist.unlink()
        old = {'checked_at': self.now.isoformat(), 'active': ['instance_mode_invalid'], 'counts': {}}
        report = self.run_collect(status(), now=at('2026-09-14T10:31:00'))
        self.assertEqual(selfcheck.update_state(old, report)['events'],
                         [{'event': 'RECOVERED', 'issue': 'instance_mode_invalid'}])

    def test_collector_disabled_is_reported(self):
        self.assertIn('collector_stopped', self.run_collect(status([dataset()], enabled=False))['issues'])

    def test_invalid_watchlist_reports_failure_instead_of_empty_success(self):
        for data in ({}, {'a': 1}, [1], [{'code': '../bad'}], [{'code': 'sh000001'}, {'code': None}]):
            self.watchlist.write_text(json.dumps(data))
            result = self.run_collect(status([dataset()]))
            self.assertEqual(result['issues'], ['watchlist_unavailable'], data)

    # --- cold standby keepalive and binding generation ---

    def test_failed_keepalive_is_cold_standby_issue(self):
        failed = self.run_collect(status([dataset()], [probe(keepalive={'verdict': 'fail', 'verified_at': 'x'}),
                                                       probe(item='minute_history', keepalive=None)]))
        self.assertEqual(failed['issues'], ['cold_standby:CN/stock/day'])
        key = 'cold_standby:CN/stock/day'
        old = {'checked_at': self.now.isoformat(), 'active': [key], 'counts': {}}
        later = at('2026-09-14T10:31:00')
        pending = self.run_collect(status([dataset()], [probe(keepalive={'verdict': 'pending'})]), now=later)
        self.assertEqual(selfcheck.update_state(old, pending)['active'], [key])  # not proven fixed
        passed = self.run_collect(status([dataset()], [probe(keepalive={'verdict': 'pass'})]), now=later)
        self.assertEqual(selfcheck.update_state(old, passed)['events'], [{'event': 'RECOVERED', 'issue': key}])

    def test_binding_gen_change_between_runs_is_noticed_once(self):
        output = self.root / 'monitor/status.json'
        gens = [1, 1, 2, 2]
        for i, gen in enumerate(gens):
            payload = selfcheck.validate_status(status([dataset()], [probe(gen=gen)]))
            with patch.object(selfcheck.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                    patch.object(selfcheck, 'fetch_status', return_value=payload), \
                    patch.object(selfcheck, 'datetime', clock(at(f'2026-09-14T10:3{i}:00'))), \
                    patch.dict('os.environ', {'CHANAPP_CACHE_DIR': str(self.root),
                                              'WATCHLIST_PATH': str(self.watchlist)}):
                self.assertEqual(selfcheck.main(['--output', str(output)]), 0)
            saved = json.loads(output.read_text())
            expected = ['binding_changed:CN/stock/day'] if i == 2 else []
            self.assertEqual(saved['issues'], expected, i)
            self.assertEqual([e for e in saved['events'] if e['event'] == 'NOTICE'],
                             [{'event': 'NOTICE', 'issue': 'binding_changed:CN/stock/day'}] if i == 2 else [])
        self.assertEqual(json.loads(output.with_name(selfcheck.BINDING_FILE).read_text()), {'CN/stock/day': 2})
        # A damaged baseline is rebuilt without a false alarm.
        output.with_name(selfcheck.BINDING_FILE).write_text('{')
        self.assertEqual(selfcheck.load_bindings(output.with_name(selfcheck.BINDING_FILE)), {})

    # --- alert state machine ---

    def test_two_failures_deduplicate_then_recover_and_gap_resets_counter(self):
        report = {'checked_at': self.now.isoformat(), 'issues': ['service_unavailable'],
                  'deferred': [], 'rows': []}
        first = selfcheck.update_state({}, report)
        self.assertEqual(first['status'], 'pending')
        second = selfcheck.update_state(first, {**report, 'checked_at': at('2026-09-14T10:31:00').isoformat()})
        self.assertEqual(second['events'], [{'event': 'ALERT', 'issue': 'service_unavailable'}])
        third = selfcheck.update_state(second, {**report, 'checked_at': at('2026-09-14T10:32:00').isoformat()})
        self.assertEqual(third['events'], [])
        recovered = selfcheck.update_state(third, {**report, 'issues': [], 'checked_at': at('2026-09-14T10:33:00').isoformat()})
        self.assertEqual(recovered['events'], [{'event': 'RECOVERED', 'issue': 'service_unavailable'}])
        gap = selfcheck.update_state(first, {**report, 'checked_at': at('2026-09-14T11:00:00').isoformat()})
        self.assertEqual(gap['status'], 'pending')

    def test_service_outage_does_not_claim_any_other_recovery(self):
        active = ['calendar_unavailable', 'cold_standby:CN/stock/day', 'sh600000/m5:stale', 'watchlist_unavailable']
        old = {'checked_at': self.now.isoformat(), 'active': active, 'counts': {}}
        report = self.run_collect(status(), service_rc=3)
        state = selfcheck.update_state(old, {**report, 'checked_at': at('2026-09-14T10:31:00').isoformat()})
        self.assertEqual(state['events'], [])
        self.assertEqual(state['active'], active)

    def test_removing_watchlist_item_retires_alert_instead_of_claiming_recovery(self):
        key = 'sz000001/m5:stale'
        old = {'checked_at': self.now.isoformat(), 'active': [key], 'counts': {}}
        report = self.run_collect(status([dataset()]), now=at('2026-09-14T10:31:00'))
        state = selfcheck.update_state(old, report)
        self.assertEqual(state['events'], [{'event': 'RETIRED', 'issue': key}])
        self.assertEqual(state['active'], [])

    def test_state_roundtrip_and_damaged_state_is_visible(self):
        path = self.root / 'status.json'
        self.assertEqual(selfcheck.load_state(path), {})
        state = {'checked_at': self.now.isoformat(), 'active': [], 'counts': {}}
        selfcheck.atomic_json(path, state)
        self.assertEqual(selfcheck.load_state(path), state)
        path.write_text('{')
        with self.assertRaises(ValueError):
            selfcheck.load_state(path)

    def test_monitor_main_persists_failure_and_recovery_events(self):
        output = self.root / 'monitor/status.json'
        clock = ['2026-09-14T10:30:00', '2026-09-14T10:31:00', '2026-09-14T10:32:00']
        for i, moment in enumerate(clock):
            report = {'checked_at': at(moment).isoformat(), 'issues': ['service_unavailable'] if i < 2 else [],
                      'deferred': [], 'rows': []}
            with patch.object(selfcheck, 'collect', return_value=report):
                self.assertEqual(selfcheck.main(['--output', str(output)]), 0)
            saved = json.loads(output.read_text())
            self.assertEqual(saved['status'], ('pending', 'degraded', 'ok')[i])
        self.assertEqual(saved['events'], [{'event': 'RECOVERED', 'issue': 'service_unavailable'}])

    def test_selfcheck_imports_no_engine_module(self):
        # The timer process must not load fetch code (it never fetches quotes).
        code = ('import sys; import chanapp.scripts.selfcheck; '
                'print([m for m in sys.modules if m.startswith("chanapp.engine")])')
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), '[]')


if __name__ == '__main__':
    unittest.main()
