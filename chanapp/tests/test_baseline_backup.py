"""Offline backup display contracts."""
import unittest
from pathlib import Path
from unittest.mock import patch
from chanapp.engine.feeds import baseline_backup as a
TEXT = (Path(__file__).parent / 'fixtures/baseline_backup/quotes.txt').read_text()

class BackupTests(unittest.TestCase):
    def test_validation(self):
        with patch.object(a, '_fetch_text', return_value=TEXT) as fetch:
            self.assertEqual(a.fetch_f10('hk00700')['f10']['price'], 432)
            self.assertEqual(fetch.call_count, 1)
            with self.assertRaises(ValueError):
                a.fetch_f10('hk00700&other')

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
