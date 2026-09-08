"""Google quota errors must terminate retries and retain their real reset time."""
import json
import runpy
import sys
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

SAFETY = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'gateway_safety.py'))


def payload(metadata=None, reason='QUOTA_EXHAUSTED'):
    return {'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED', 'details': [
        {'reason': reason, 'metadata': metadata or {}}]}}


class QuotaParsing(unittest.TestCase):
    def test_reset_timestamp_is_authoritative_and_not_capped_at_one_day(self):
        expected = datetime(2031, 2, 17, 23, 16, 53, tzinfo=timezone.utc).timestamp()
        result = SAFETY['quota_details'](payload({
            'quotaResetTimeStamp': '2031-02-17T23:16:53Z', 'quotaResetDelay': '120s'}), 100)
        self.assertEqual(result, {'resetAt': expected})
        self.assertIn('2031', SAFETY['quota_message'](result))
        self.assertIn('UTC', SAFETY['quota_message'](result))

    def test_duration_fallbacks(self):
        result = SAFETY['quota_details'](payload({'quotaResetDelay': '124h30m5.5s'}), 100)
        self.assertEqual(result['resetAt'], 448305.5)
        value = payload({'quotaResetTimeStamp': 'invalid'})
        value['error']['details'].append({
            '@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '448205.5s'})
        self.assertEqual(SAFETY['quota_details'](value, 100)['resetAt'], 448305.5)

    def test_generic_resource_exhausted_is_not_assumed_to_be_quota(self):
        self.assertIsNone(SAFETY['quota_details'](payload(reason='RATE_LIMIT_EXCEEDED'), 100))
        self.assertIsNone(SAFETY['quota_details']({'error': 'bad'}, 100))

    def test_missing_or_malformed_reset_is_honest(self):
        for metadata in ({}, {'quotaResetDelay': 'NaNs'}, {'quotaResetTimeStamp': 'tomorrow'}):
            result = SAFETY['quota_details'](payload(metadata), 100)
            self.assertIsNone(result['resetAt'])
            self.assertIn('did not provide a reset time', SAFETY['quota_message'](result))


class QuotaFlow(unittest.IsolatedAsyncioTestCase):
    async def test_capture_blocks_later_acquire_until_reset_without_request_or_sleep(self):
        data = {'accounts': [{'email': 'fixture'}]}
        def update(fn):
            fn(data)
        async def pool(fn, *args):
            return fn(*args)
        class HTTPError(Exception):
            def __init__(self, status, detail, headers=None):
                self.status, self.detail, self.headers = status, detail, headers
        storage = types.SimpleNamespace(update_accounts=update, load_accounts_read_only=lambda: data)
        response = types.SimpleNamespace(status_code=429, json=lambda: payload({'quotaResetDelay': '448205s'}))
        manager = types.SimpleNamespace(acquire_account=Mock(return_value={'email': 'fixture'}))
        with patch.dict(sys.modules, {'quota_fixture.storage': storage,
                                     'fastapi': types.SimpleNamespace(HTTPException=HTTPError)}), \
             patch.dict(SAFETY['capture_quota'].__globals__, __package__='quota_fixture'):
            message = await SAFETY['capture_quota'](response, 'fixture', 'gemini-3.8-flash', pool)
            self.assertIn('Google quota exhausted', message)
            self.assertNotIn('details', json.dumps(data))
            with self.assertRaises(HTTPError) as error:
                await SAFETY['acquire_serially'](manager, 'gemini-3.8-flash-high', pool)
            self.assertGreater(int(error.exception.headers['Retry-After']), 86400)
            manager.acquire_account.assert_not_called()
            self.assertIsNone(await SAFETY['rate_limit_pause_seconds']('gemini', pool, 3600))
            self.assertIsNone(SAFETY['active_quota'](data, 'claude', time.time()))
            data[SAFETY['QUOTA_KEY']]['fixture']['gemini']['resetAt'] = time.time() - 1
            self.assertEqual(await SAFETY['acquire_serially'](manager, 'gemini', pool), {'email': 'fixture'})


if __name__ == '__main__':
    unittest.main()
