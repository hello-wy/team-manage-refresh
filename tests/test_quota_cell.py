import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


class QuotaCellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / 'app/templates'), autoescape=True)
        cls.cell = staticmethod(env.get_template('admin/_quota_cell.html').module.quota_cell)

    def test_missing_usage_keeps_cached_reset_without_inventing_zero_usage(self):
        html = self.cell(None, None, '2030-01-01T05:00:00Z', '2030-01-07T00:00:00Z')
        self.assertEqual(html.count('暂无额度'), 2)
        self.assertEqual(html.count('quota-cached-label'), 2)
        self.assertIn('data-reset-at="2030-01-01T05:00:00Z"', html)
        self.assertIn('data-reset-at="2030-01-07T00:00:00Z"', html)
        self.assertNotIn('<progress', html)

    def test_snapshot_reset_takes_priority_over_cached_reset(self):
        html = self.cell({'remaining': 75, 'used': 25, 'limit': 100, 'reset_at': 'fresh-reset'}, None, 'old-reset')
        self.assertIn('data-reset-at="fresh-reset"', html)
        self.assertNotIn('old-reset', html)
        self.assertIn('value="25"', html)
        self.assertNotIn('quota-cached-label', html)

    def test_not_applicable_window_does_not_show_stale_countdown(self):
        html = self.cell({'state': 'not_applicable'}, None, 'stale-reset')
        self.assertIn('不适用', html)
        self.assertNotIn('stale-reset', html)
        self.assertNotIn('<progress', html)

    def test_existing_two_argument_call_still_renders_both_windows(self):
        html = self.cell(None, {'remaining': 0, 'used': 100, 'limit': 100, 'state': 'exhausted'})
        self.assertIn('quota-period-5h', html)
        self.assertIn('quota-period-7d', html)
        self.assertIn('is-exhausted', html)
        self.assertIn('value="100"', html)
        self.assertNotIn('quota-cached-label', html)

    def test_unavailable_reason_does_not_look_like_zero_quota(self):
        html = self.cell(None, None, unavailable_text='待授权')
        self.assertEqual(html.count('待授权'), 2)
        self.assertNotIn('暂无额度', html)
        self.assertNotIn('<progress', html)


if __name__ == '__main__':
    unittest.main()
