"""Regression tests for Firecrawl's official asynchronous batch API."""
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from batch_backfill import (FirecrawlBatch, batch_url_keys, canonical_url,
                            firecrawl_description, per_shard_credit_budget,
                            chitai_alternative, rotated_candidates)
from collector import Sheets, BookRow


class FirecrawlBatchTests(unittest.TestCase):
    def test_book24_product_sku_maps_to_chitai_gorod(self):
        row = [""] * 18
        row[1] = "Элегия смерти"
        row[2] = "Хаохуэй Чжоу"
        row[8] = "https://book24.ru/product/elegiya-smerti-6757550/"
        row[15] = "2990861"
        book = BookRow("Book24", 1, 20, row, 16, 17)
        self.assertEqual(chitai_alternative(book),
                         "https://www.chitai-gorod.ru/product/elegiya-smerti-2990861")
        row[15] = "not-a-sku"
        self.assertIsNone(chitai_alternative(book))

    def test_book24_non_matching_store_has_no_alternative(self):
        row = [""] * 18
        row[8] = "https://labirint.ru/books/123456/"
        row[15] = "2990861"
        self.assertIsNone(chitai_alternative(BookRow("Лабиринт", 1, 3, row, 17, 18)))

    def test_credit_batch_rotation_changes_attempts(self):
        urls = [f"https://book24.ru/product/item-{n}/" for n in range(100)]
        one = rotated_candidates(urls, 2, 4567)
        two = rotated_candidates(urls, 2, 4568)
        self.assertEqual(set(one), set(two))
        self.assertNotEqual(one[:25], two[:25])
        self.assertEqual(one, rotated_candidates(urls, 2, 4567))

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
