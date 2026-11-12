"""Regression tests for Firecrawl's official asynchronous batch API."""
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from batch_backfill import (FirecrawlBatch, batch_url_keys, canonical_url,
                            firecrawl_description, per_shard_credit_budget)
from collector import Sheets


class FirecrawlBatchTests(unittest.TestCase):
    def test_canonical_url(self):
        self.assertEqual(canonical_url('https://www.labirint.ru/books/50/?utm=1'),
                         'https://labirint.ru/books/50')

    def test_reject_bad_http_status(self):
        self.assertIsNone(firecrawl_description({
            'metadata': {'sourceURL': 'https://book24.ru/product/1/', 'statusCode': 403},
            'markdown': 'В городе исчезают несколько человек, и полицейский расследует опасные события.'
        }))

    def test_retry_after_for_429(self):
        slept = []
        batch = FirecrawlBatch('test', sleep_fn=slept.append)
        first = Mock(status_code=429, headers={'Retry-After': '2'})
        second = Mock(status_code=200, headers={})
        second.json.return_value = {'id': 'abc'}
        batch.session.request = Mock(side_effect=[first, second])
        self.assertEqual(batch.request('POST', batch.base, {'urls': []}), {'id': 'abc'})
        self.assertEqual(slept, [2.0])

    def test_paginated_batch_status(self):
        batch = FirecrawlBatch('test', sleep_fn=lambda _: None)
        batch.request = Mock(side_effect=[
            {'status': 'completed', 'data': [{'a': 1}],
             'next': batch.base + '/123?skip=1'},
            {'data': [{'a': 2}]},
        ])
        seen = []
        self.assertEqual(batch.poll('123', seen.append, float('inf')), 'completed')
        self.assertEqual(seen, [{'a': 1}, {'a': 2}])

    def test_reject_untrusted_next_url(self):
        batch = FirecrawlBatch('test', sleep_fn=lambda _: None)
        batch.request = Mock(return_value={'status': 'completed', 'data': [],
                                           'next': 'https://evil.example/token'})
        with self.assertRaises(RuntimeError):
            batch.poll('123', lambda _: None, float('inf'))

    def test_batch_url_keys_normalize_both_sides(self):
        raw = 'https://www.labirint.ru/books/50/?utm_source=ranking'
        self.assertIn('https://labirint.ru/books/50', batch_url_keys([raw]))
        self.assertEqual(canonical_url(raw), 'https://labirint.ru/books/50')

    def test_firecrawl_credit_budget_preserves_reserve(self):
        self.assertEqual(per_shard_credit_budget(1025, 4, reserve=25, max_per_shard=250), 250)
        self.assertEqual(per_shard_credit_budget(425, 4, reserve=25, max_per_shard=250), 100)
        self.assertEqual(per_shard_credit_budget(25, 4, reserve=25, max_per_shard=250), 0)
        self.assertEqual(per_shard_credit_budget(1000, 4, reserve=25, max_per_shard=250), 243)

    def test_remaining_credits_endpoint(self):
        batch = FirecrawlBatch('test')
        batch.request = Mock(return_value={'success': True, 'data': {'remainingCredits': 1025}})
        self.assertEqual(batch.remaining_credits(), 1025)
        batch.request.assert_called_once()

    def test_sheets_retries_transient_socket_errors(self):
        self.assertTrue(Sheets._transient(OSError('SSL EOF')))
        self.assertTrue(Sheets._transient(TimeoutError('timed out')))
        self.assertFalse(Sheets._transient(ValueError('bad data')))


if __name__ == '__main__':
    unittest.main()
