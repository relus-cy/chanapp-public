"""Offline backup display contracts."""
import unittest
from pathlib import Path
from unittest.mock import patch
from chanapp.engine.feeds import baseline_backup as a
TEXT = (Path(__file__).parent / 'fixtures/baseline_backup/quotes.txt').read_text()
CODES = ['sh600519', 'sz000001', 'sh000001', 'hk00700', 'hk09988']

class BackupTests(unittest.TestCase):
    def test_recorded(self):
        rows = a.parse_quotes(TEXT, CODES)
        self.assertEqual(set(rows), set(CODES))
        self.assertEqual(rows['hk00700']['price'], 432)
        self.assertFalse(rows['sh000001']['limit_up'])
        self.assertGreater(rows['hk00700']['source_ts'], 0)

    def test_invalid_missing_duplicate(self):
        self.assertEqual(a.parse_quotes(TEXT, ['sz999999']), {})
        self.assertEqual(a.parse_quotes(TEXT + TEXT, CODES), {})
        for index, value in [(3,'0'), (3,'-1'), (3,'nan'), (4,'inf'), (4,'0'),
                             (30,'bad'), (30,'29990101000000'), (32,'nan'), (32,'90'), (2,'000001')]:
            fields = TEXT.splitlines()[0].split('"')[1].split('~')
            fields[index] = value
            with self.subTest(index=index, value=value):
                self.assertEqual(a.parse_quotes('v_sh600519="' + '~'.join(fields) + '";', ['sh600519']), {})

    def test_validation(self):
        with patch.object(a, '_fetch_text', return_value=TEXT) as fetch:
            self.assertEqual(set(a.fetch_quotes(['hk00700','sz999999'])), {'hk00700'})
            self.assertEqual(a.fetch_quotes([]), {})
            with self.assertRaises(ValueError):
                a.fetch_quotes(['hk00700&other'])
            self.assertEqual(fetch.call_count, 1)

    def test_f10(self):
        with patch.object(a, '_fetch_text', return_value=TEXT):
            hk = a.fetch_f10('hk00700')
            self.assertAlmostEqual(hk['f10']['amount'], 202156575.590)
            self.assertAlmostEqual(hk['f10']['total_mv'], 39325.6542 * 1e8)
            self.assertIsNone(hk['f10']['pb'])
            self.assertIsNone(hk['f10']['pe_ttm'])
            self.assertTrue(all(v is None for v in hk['flow'].values()))
            self.assertEqual(a.fetch_f10('sh600519')['f10']['pb'], 6.49)
            with self.assertRaises(RuntimeError):
                a.fetch_f10('sz999999')

    def test_amount_scale(self):
        fields = TEXT.splitlines()[0].split('"')[1].split('~')
        fields[37], fields[44], fields[46] = '12.5', 'nan', '-'
        with patch.object(a, '_fetch_text', return_value='v_sh600519="' + '~'.join(fields) + '";'):
            data = a.fetch_f10('sh600519')['f10']
        self.assertEqual(data['amount'], 125000)
        self.assertIsNone(data['float_mv'])
        self.assertIsNone(data['pb'])

    def test_later_batch_failure_preserves_valid_rows(self):
        codes = ['sh600519'] + ['sz%06d' % i for i in range(80)]
        with patch.object(a, '_fetch_text', side_effect=[TEXT, OSError('offline')]):
            self.assertIn('sh600519', a.fetch_quotes(codes))
        with patch.object(a, '_fetch_text', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                a.fetch_quotes(codes)
