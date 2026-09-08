import contextlib
import io
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import claude_usage as usage


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'projects'
        self.project = self.root / 'example-project'
        self.project.mkdir(parents=True)
        self.now = datetime.now(timezone.utc)
        self.sid = str(uuid.uuid4())
        usage.FILE_CACHE.clear()

    def row(self, key, hours=1, model='claude-fable-5-1'):
        return {
            'timestamp': (self.now - timedelta(hours=hours)).isoformat(),
            'requestId': key,
            'cwd': '/example/project',
            'message': {
                'id': key, 'model': model,
                'usage': {
                    'input_tokens': 100, 'output_tokens': 200,
                    'cache_read_input_tokens': 300,
                    'cache_creation_input_tokens': 400,
                    'cache_creation': {'ephemeral_1h_input_tokens': 400},
                    'output_tokens_details': {'thinking_tokens': 50},
                },
            },
        }

    def write(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def collect(self, hours=24, sid=None):
        return usage.collect(hours, sid, self.now, [self.root])

    def test_dedup_subagents_and_time_scopes(self):
        recent = self.row('recent')
        self.write(self.project / (self.sid + '.jsonl'), [self.row('old', 30), recent, recent])
        self.write(self.project / self.sid / 'subagents' / 'agent-a.jsonl',
                   [self.row('child', .1, 'claude-opus-5')])
        groups, _, cutoff, warnings = self.collect()
        rows = groups[self.sid]
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(r['sub'] for r in rows), 1)
        self.assertEqual(warnings, [])
        recent_totals = usage.totals([r for r in rows if r['time'] >= cutoff])
        self.assertEqual(recent_totals['requests'], 2)
        self.assertEqual(recent_totals['thinking'], 100)
        self.assertEqual(recent_totals['output'], 400)
        self.assertAlmostEqual(recent_totals['usd'], .019075 + .00965)

    def test_copied_requests_count_once_across_sessions(self):
        other = str(uuid.uuid4())
        self.write(self.project / (self.sid + '.jsonl'), [self.row('shared', 2)])
        self.write(self.project / (other + '.jsonl'), [self.row('shared', 1), self.row('new')])
        groups = self.collect()[0]
        self.assertEqual(sum(len(v) for v in groups.values()), 2)
        self.assertEqual(groups[self.sid][0]['key'][0], 'shared')

    def test_partial_append_is_retried(self):
        path = self.project / (self.sid + '.jsonl')
        self.write(path, [self.row('one')])
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(self.row('two')))
        self.assertEqual(len(self.collect()[0][self.sid]), 1)
        with path.open('a', encoding='utf-8') as f:
            f.write('\n')
        self.assertEqual(len(self.collect()[0][self.sid]), 2)

    def test_requested_old_session_is_still_available(self):
        self.write(self.project / (self.sid + '.jsonl'), [self.row('old', 30)])
        self.assertEqual(self.collect()[0], {})
        self.assertEqual(len(self.collect(sid=self.sid)[0][self.sid]), 1)

    def test_unknown_price_and_fast_usage_are_marked(self):
        unknown = self.row('one', model='unknown')['message']
        fast = self.row('two')['message']
        fast['usage']['speed'] = 'fast'
        result = usage.totals([unknown, fast])
        self.assertEqual(result['unpriced'], 2)
        self.assertEqual(result['requests'], 2)
        self.assertTrue(usage.money(result).endswith('*'))

    def test_unknown_cache_ttl_is_not_silently_priced(self):
        msg = self.row('one')['message']
        msg['usage']['cache_creation'] = {}
        self.assertEqual(usage.totals([msg])['unpriced'], 1)

    def test_cached_window_expires_at_reset(self):
        reset = self.now + timedelta(hours=1)
        state = {'fetched_at': self.now.timestamp(), 'data': {'limits': [
            {'kind': 'session', 'percent': 20, 'resets_at': reset.isoformat()},
        ]}}
        limits, start, _ = usage.limits_and_window(state, self.now)
        self.assertEqual(start, reset - timedelta(hours=5))
        self.assertNotIn('Fable', limits)
        limits, start, _ = usage.limits_and_window(state, reset)
        self.assertIsNone(start)
        self.assertTrue(limits['5小时']['expired'])

    def test_reset_text_timezone_countdown_and_expiration(self):
        now = datetime(2026, 9, 8, 18, 21, tzinfo=timezone.utc)
        end = datetime(2026, 9, 8, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(usage.reset_text(dict(reset=end, expired=False), now),
                         '重置 09-09 06:30:00，剩 4小时9分')
        self.assertEqual(usage.reset_text(dict(reset=None), now), '重置未知')
        self.assertIn('已过期待刷新', usage.reset_text(dict(reset=end), end))
        self.assertIn('剩 6天0分', usage.reset_text(dict(reset=now+timedelta(days=6)), now))

    def test_status_line_includes_separate_reset_times(self):
        from types import SimpleNamespace
        s = dict(now=self.now, limits={
            '5小时': dict(percent=37, reset=self.now+timedelta(hours=2), expired=False),
            '总周': dict(percent=4, reset=self.now+timedelta(days=3), expired=False),
        }, groups={}, recent=usage.totals([]), all=usage.totals([]), within=None,
                 fetched=0, note='local', warnings=[])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            usage.render(s, SimpleNamespace(hours=24), compact=True)
        self.assertIn('5小时 37%（重置 ', out.getvalue())
        self.assertIn('总周 4%（重置 ', out.getvalue())
        self.assertIn('剩 2小时0分', out.getvalue())
        self.assertIn('剩 3天0分', out.getvalue())

    def test_429_cooldown_survives_next_invocation(self):
        credentials = self.base / 'fixture-credentials.json'
        credentials.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'test-placeholder'}}))
        cache = self.base / 'cache'
        error = HTTPError('https://api.anthropic.com/api/oauth/usage', 429,
                          'limited', {'Retry-After': '3600'}, None)
        with patch.object(usage, 'CACHE', cache), patch.object(usage, 'CREDENTIALS', credentials):
            with patch.object(usage, 'urlopen', side_effect=error) as network:
                state, note = usage.quota(False)
                self.assertEqual(network.call_count, 1)
            self.assertIn('HTTP 429', note)
            self.assertGreater(state['next_query'], self.now.timestamp() + 3500)
            self.assertNotIn('test-placeholder', (cache / 'quota.json').read_text())
            with patch.object(usage, 'urlopen', side_effect=AssertionError('unexpected request')):
                usage.quota(False)
                usage.quota(True)

    def test_default_cli_does_not_query_network(self):
        with patch.object(usage, 'CONFIG_DIR', self.base), patch.object(usage, 'CACHE_BASE', self.base / 'cache'), \
                patch.object(usage, 'quota', return_value=({}, 'local')) as quota, \
                patch('sys.argv', ['claude-usage']), contextlib.redirect_stdout(io.StringIO()):
            usage.main()
        quota.assert_called_once_with(True)

    def test_cli_custom_pricing(self):
        rates = self.base / 'prices.json'
        rates.write_text(json.dumps({'fixture-model': dict(input=1, output=2, cache_read=.1,
                                                         cache_write_1h=2, cache_write_5m=1.25)}))
        with patch.object(usage, 'RATES', dict(usage.RATES)), \
                patch.object(usage, 'CONFIG_DIR', self.base), \
                patch.object(usage, 'quota', return_value=({}, 'local')), \
                patch('sys.argv', ['claude-usage', '--pricing', str(rates)]), \
                contextlib.redirect_stdout(io.StringIO()):
            usage.main()
            self.assertEqual(usage.RATES['fixture-model'], (1, 2, .1, 2, 1.25))


if __name__ == '__main__':
    unittest.main()
