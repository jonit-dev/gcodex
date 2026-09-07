import asyncio
from pathlib import Path
import runpy
import sys
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, patch


class HTTPError(Exception):
    def __init__(self, status_code, detail, headers=None):
        self.status_code, self.detail, self.headers = status_code, detail, headers


class QueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'gateway_safety.py'
        self.safety = runpy.run_path(str(path))
        self.data = {'accounts': [{'email': 'test'}]}
        modules = patch.dict(sys.modules, {
            'fastapi': types.SimpleNamespace(HTTPException=HTTPError),
            'queue_fixture.storage': types.SimpleNamespace(load_accounts_read_only=lambda: self.data),
        })
        modules.start()
        self.addCleanup(modules.stop)
        globals_patch = patch.dict(self.safety['acquire_serially'].__globals__, __package__='queue_fixture')
        globals_patch.start()
        self.addCleanup(globals_patch.stop)
        self.manager = types.SimpleNamespace(_lock=threading.Lock(), _in_flight={})
        self.manager.acquire_account = lambda model: None

    async def pool(self, fn, *args):
        return fn(*args)

    async def test_busy_waits_locally_then_acquires(self):
        results = iter([None, {'email': 'test'}])
        self.manager._in_flight['test'] = 1
        self.manager.acquire_account = lambda model: next(results)
        with patch('asyncio.sleep', new_callable=AsyncMock) as sleep:
            result = await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(result, {'email': 'test'})
        sleep.assert_awaited_once()

    async def test_busy_timeout_is_429(self):
        self.manager._in_flight['test'] = 1
        with patch.dict(self.safety['acquire_serially'].__globals__, QUEUE_TIMEOUT_SECONDS=0):
            with self.assertRaises(HTTPError) as error:
                await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(error.exception.status_code, 429)
        self.assertIn('busy', error.exception.detail)

    async def test_auth_stop_is_403(self):
        self.data['gcodexAuthBlocked'] = True
        with self.assertRaises(HTTPError) as error:
            await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(error.exception.status_code, 403)

    async def test_account_count_is_409(self):
        self.data['accounts'] = []
        with self.assertRaises(HTTPError) as error:
            await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(error.exception.status_code, 409)

    def cool_down(self, scope='gemini', seconds=120):
        self.data['accountState'] = {'cooldowns': {'test': {scope: time.time() + seconds}}}

    async def test_recorded_cooldown_is_429_without_waiting(self):
        self.cool_down()
        with patch('asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(HTTPError) as error:
                await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(error.exception.status_code, 429)
        self.assertIn('cooling down', error.exception.detail)
        sleep.assert_not_awaited()

    async def test_account_scoped_cooldown_is_429_without_waiting(self):
        self.cool_down(scope='account')
        with patch('asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(HTTPError) as error:
                await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(error.exception.status_code, 429)
        self.assertIn('cooling down', error.exception.detail)
        sleep.assert_not_awaited()

    async def test_slot_released_before_busy_check_keeps_waiting(self):
        # The holder releases between our failed acquire and the in-flight read,
        # so nothing is busy and no cooldown is recorded. Reporting a cooldown
        # here would fail a request whose slot is already free.
        results = iter([None, {'email': 'test'}])
        self.manager._in_flight = {}
        self.manager.acquire_account = lambda model: next(results)
        with patch('asyncio.sleep', new_callable=AsyncMock) as sleep:
            result = await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(result, {'email': 'test'})
        sleep.assert_awaited_once()

    async def test_expired_cooldown_does_not_shortcut_the_wait(self):
        self.cool_down(seconds=-1)
        results = iter([None, {'email': 'test'}])
        self.manager.acquire_account = lambda model: next(results)
        with patch('asyncio.sleep', new_callable=AsyncMock):
            result = await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(result, {'email': 'test'})

    async def test_cancelling_waiter_keeps_owner_slot(self):
        self.manager._in_flight['test'] = 1
        with patch('asyncio.sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(self.manager._in_flight, {'test': 1})

    async def test_request_cancellation_after_acquisition_releases_slot(self):
        released = []
        self.manager.acquire_account = lambda model: {'email': 'test'}
        self.manager.release_account = released.append
        async def cancelled_delivery(fn, *args):
            fn(*args)
            raise asyncio.CancelledError()
        async def app(scope, receive, send):
            await self.safety['acquire_serially'](self.manager, 'gemini', cancelled_delivery)
        middleware = self.safety['RequestLeaseMiddleware'](app)
        with self.assertRaises(asyncio.CancelledError):
            await middleware({'type': 'http'}, None, None)
        self.assertEqual(released, ['test'])

    async def test_unstarted_response_body_releases_slot(self):
        released = []
        self.manager.acquire_account = lambda model: {'email': 'test'}
        self.manager.release_account = released.append
        async def app(scope, receive, send):
            await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
            # Return before entering any body generator's try/finally.
        await self.safety['RequestLeaseMiddleware'](app)({'type': 'http'}, None, None)
        self.assertEqual(released, ['test'])

    async def test_early_release_not_repeated_at_request_end(self):
        released = []
        self.manager.acquire_account = lambda model: {'email': 'test'}
        self.manager.release_account = released.append
        async def app(scope, receive, send):
            await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
            self.safety['release_tracked_account'](self.manager, 'test')
            self.safety['release_tracked_account'](self.manager, 'test')
        await self.safety['RequestLeaseMiddleware'](app)({'type': 'http'}, None, None)
        self.assertEqual(released, ['test'])
