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


class QueueFixture(unittest.IsolatedAsyncioTestCase):
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


class QueueTests(QueueFixture):
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

    async def test_recorded_cooldown_waits_then_acquires(self):
        # A rate limit ends the Codex turn if the gateway reports it, because
        # the profile allows no client retries. Waiting it out locally keeps
        # the turn alive and sends Google nothing until the cooldown expires.
        self.cool_down(seconds=120)
        results = iter([None, {'email': 'test'}])
        self.manager.acquire_account = lambda model: next(results)
        with patch('asyncio.sleep', new_callable=AsyncMock) as sleep:
            result = await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(result, {'email': 'test'})
        self.assertAlmostEqual(sleep.await_args_list[0].args[0], 120, delta=2)

    async def test_account_scoped_cooldown_waits_too(self):
        self.cool_down(scope='account', seconds=120)
        results = iter([None, {'email': 'test'}])
        self.manager.acquire_account = lambda model: next(results)
        with patch('asyncio.sleep', new_callable=AsyncMock) as sleep:
            result = await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(result, {'email': 'test'})
        self.assertAlmostEqual(sleep.await_args_list[0].args[0], 120, delta=2)

    async def test_cooldown_longer_than_the_budget_is_429(self):
        # Waiting past the client's own stream tolerance would trade a clear
        # error for a silent disconnect, so a long cooldown is still reported.
        self.cool_down(seconds=4000)
        with patch('asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(HTTPError) as error:
                await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(error.exception.status_code, 429)
        self.assertIn('cooling down', error.exception.detail)
        self.assertAlmostEqual(int(error.exception.headers['Retry-After']), 4000, delta=2)
        sleep.assert_not_awaited()

    async def test_repeated_cooldowns_cannot_wait_past_the_budget(self):
        # Each pause spends the request's budget; a cooldown that keeps being
        # extended must not hold the turn open indefinitely.
        self.cool_down(seconds=500)
        self.manager.acquire_account = lambda model: None
        waits = []
        async def sleep(seconds):
            waits.append(seconds)
            self.cool_down(seconds=500)
        with patch('asyncio.sleep', sleep):
            with self.assertRaises(HTTPError) as error:
                await self.safety['acquire_serially'](self.manager, 'gemini', self.pool)
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(len(waits), 1)

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


class RateLimitPauseTests(QueueFixture):
    """A 429 mid-stream is a wait, not a verdict.

    The stream has not shown the user anything yet, so the gateway can hold the
    turn open, sleep off the cooldown upstream just recorded, and send one more
    request on the slot it already owns. What it must never do is retry early,
    retry after output has shipped, or wait longer than the client's own stream
    tolerance -- so this is a bounded pause, not a retry loop.
    """

    def pause(self, budget=900):
        return self.safety['rate_limit_pause_seconds']('gemini', self.pool, budget)

    async def test_pause_matches_the_recorded_cooldown(self):
        self.data['accountState'] = {'cooldowns': {'test': {'gemini': time.time() + 120}}}
        self.assertAlmostEqual(await self.pause(), 120, delta=2)

    async def test_pause_without_a_recorded_cooldown_still_backs_off(self):
        self.assertEqual(await self.pause(),
                         self.safety['RATE_LIMIT_FALLBACK_PAUSE_SECONDS'])

    async def test_cooldown_beyond_the_budget_gives_up(self):
        self.data['accountState'] = {'cooldowns': {'test': {'gemini': time.time() + 4000}}}
        self.assertIsNone(await self.pause())

    async def test_spent_budget_gives_up(self):
        self.assertIsNone(await self.pause(budget=0))
        self.assertIsNone(await self.pause(budget=-1))

    async def test_auth_stop_gives_up(self):
        self.data['gcodexAuthBlocked'] = True
        self.assertIsNone(await self.pause())

    async def test_wrong_account_count_gives_up(self):
        self.data['accounts'] = []
        self.assertIsNone(await self.pause())


class RotationTests(QueueFixture):
    """Rotation runs while the request still owns the single slot.

    Waiting there can never succeed, and raising out of a started SSE body
    aborts the stream: the client sees a disconnect instead of the real
    upstream error, which is exactly what these tests keep from returning.
    """

    async def test_busy_rotation_returns_none_without_waiting(self):
        self.manager._in_flight['test'] = 1
        with patch('asyncio.sleep', new_callable=AsyncMock) as sleep:
            result = await self.safety['acquire_without_waiting'](self.manager, 'gemini', self.pool)
        self.assertIsNone(result)
        sleep.assert_not_awaited()

    async def test_busy_rotation_does_not_raise_on_cooldown_or_auth_stop(self):
        self.manager._in_flight['test'] = 1
        self.data['gcodexAuthBlocked'] = True
        self.data['accountState'] = {'cooldowns': {'test': {'gemini': time.time() + 60}}}
        self.assertIsNone(
            await self.safety['acquire_without_waiting'](self.manager, 'gemini', self.pool))

    async def test_rotated_slot_is_released_with_the_request(self):
        released = []
        self.manager.acquire_account = lambda model: {'email': 'second'}
        self.manager.release_account = released.append
        async def app(scope, receive, send):
            account = await self.safety['acquire_without_waiting'](self.manager, 'gemini', self.pool)
            self.assertEqual(account, {'email': 'second'})
        await self.safety['RequestLeaseMiddleware'](app)({'type': 'http'}, None, None)
        self.assertEqual(released, ['second'])
