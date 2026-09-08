import asyncio
import json
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
            result = fn(*args)
            if fn.__name__ == 'acquire_and_track':
                raise asyncio.CancelledError()
            return result
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


class CooldownLadderTests(QueueFixture):
    """The backoff stays exponential; only its ceiling changes.

    Upstream doubles 120s per consecutive failure to 1920s, and a 1920s wait
    is not a wait at all here: it outlives the request's budget, so the turn
    fails where a shorter interval would have retried and often succeeded.
    The cap applies to the locally guessed ladder only -- a `Retry-After` is
    the backend stating its own terms, and shortening that is what earns a
    harder limit.
    """

    def duration(self, backoff, retry_after=None, cap=None):
        overrides = {} if cap is None else {'MAX_PAUSE_SECONDS': cap}
        with patch.dict(self.safety['cooldown_duration'].__globals__, overrides):
            return self.safety['cooldown_duration'](backoff, retry_after)

    def test_ladder_below_the_cap_is_untouched(self):
        self.assertEqual(self.duration(120), 120)
        self.assertEqual(self.duration(240), 240)

    def test_deep_ladder_is_capped(self):
        self.assertEqual(self.duration(1920, cap=300), 300)

    def test_stated_retry_after_outlives_the_cap(self):
        self.assertEqual(self.duration(120, 3600, cap=300), 3600)

    def test_retry_after_keeps_upstreams_day_ceiling(self):
        self.assertEqual(self.duration(120, 999_999, cap=300), 86_400)

    def test_unreadable_retry_after_falls_back_to_the_ladder(self):
        self.assertEqual(self.duration(120, 'soon', cap=300), 120)

    def test_zero_cap_restores_the_upstream_ladder(self):
        self.assertEqual(self.duration(1920, cap=0), 1920)


class KeepaliveTests(QueueFixture):
    """A waiting stream has to say something or the client hangs up.

    Codex times an idle stream out between SSE events, and a comment line is
    not an event: verified against 0.153.4, keepalive comments every 2s still
    tripped an 8s idle timeout, while an unknown event type reset the timer
    and never reached the transcript. So the pause is sliced, and each slice
    ends in an event the client will ignore.
    """

    async def beats(self, seconds, keepalive=None):
        overrides = {} if keepalive is None else {'KEEPALIVE_SECONDS': keepalive}
        slept = []
        async def sleep(duration):
            slept.append(duration)
        with patch.dict(self.safety['keepalive_sleep'].__globals__, overrides):
            with patch('asyncio.sleep', sleep):
                sent = [beat async for beat in self.safety['keepalive_sleep'](seconds)]
        return slept, sent

    async def test_long_wait_is_sliced_with_a_beat_between_slices(self):
        slept, sent = await self.beats(300, keepalive=60)
        self.assertEqual(slept, [60] * 5)
        # Four beats, not five: nothing is emitted after the last slice, since
        # the retry itself is the next thing the client hears.
        self.assertEqual(sent, [self.safety['KEEPALIVE_EVENT']] * 4)

    async def test_short_wait_needs_no_beat(self):
        self.assertEqual(await self.beats(10, keepalive=60), ([10], []))

    async def test_remainder_is_slept_without_a_trailing_beat(self):
        slept, sent = await self.beats(70, keepalive=60)
        self.assertEqual(slept, [60, 10])
        self.assertEqual(len(sent), 1)

    async def test_disabled_keepalive_sleeps_straight_through(self):
        self.assertEqual(await self.beats(300, keepalive=0), ([300], []))

    async def test_beat_is_an_event_the_client_can_skip(self):
        self.assertTrue(self.safety['KEEPALIVE_EVENT'].startswith('data: '))
        self.assertTrue(self.safety['KEEPALIVE_EVENT'].endswith('\n\n'))
        payload = json.loads(self.safety['KEEPALIVE_EVENT'][len('data: '):])
        # Not a protocol event: no output, no terminal status, nothing the
        # transcript could mistake for part of the answer.
        self.assertEqual(list(payload), ['type'])
        self.assertEqual(payload['type'], 'response.gcodex_keepalive')


class LeaseTokenTests(QueueFixture):
    """A paused turn must not send the token it captured before the pause.

    Acquire hands a request the account dict that was live at the time, and
    every later state write rebuilds the store from disk into fresh dicts --
    so the background refresher writes somewhere the running turn cannot see.
    Acquire only promises 300s of remaining life, which a turn that waits out
    several cooldowns routinely outlives. The 401 that follows is scored as an
    account failure, so it stops the gateway rather than just the turn.
    """

    def setUp(self):
        super().setUp()
        self.refreshed = []
        self.manager.refresh_expiring_accounts = self.refreshed.append

    def store(self, **fields):
        self.data['accounts'] = [dict({'email': 'test'}, **fields)]

    async def repoint(self, account):
        await self.safety['refresh_lease_token'](self.manager, account, self.pool)
        return account

    async def test_live_stored_token_is_copied_without_a_refresh(self):
        self.store(accessToken='live', expiresAt=time.time() + 3600)
        account = await self.repoint({'email': 'test', 'accessToken': 'stale'})
        self.assertEqual(account['accessToken'], 'live')
        self.assertEqual(self.refreshed, [])

    async def test_expiring_stored_token_is_refreshed_then_copied(self):
        self.store(accessToken='old', expiresAt=time.time() + 10)
        def refresh(window):
            self.refreshed.append(window)
            self.store(accessToken='new', expiresAt=time.time() + 3600)
        self.manager.refresh_expiring_accounts = refresh
        account = await self.repoint({'email': 'test', 'accessToken': 'old'})
        self.assertEqual(account['accessToken'], 'new')
        self.assertEqual(self.refreshed, [int(self.safety['TOKEN_MARGIN_SECONDS'])])

    async def test_expiry_in_milliseconds_is_not_read_as_the_far_future(self):
        # Upstream normalises both forms on load, so a stored millisecond
        # expiry must not be mistaken for a token good for another 56000 years.
        self.store(accessToken='old', expiresAt=(time.time() + 10) * 1000)
        account = await self.repoint({'email': 'test', 'accessToken': 'old'})
        self.assertEqual(len(self.refreshed), 1)

    async def test_project_id_travels_with_a_refreshed_token(self):
        # A refresh can discover the project id for the first time, and the
        # lease reads it off this same dict.
        self.store(accessToken='live', expiresAt=time.time() + 3600, projectId='p-1')
        account = await self.repoint({'email': 'test'})
        self.assertEqual(account['projectId'], 'p-1')

    async def test_unknown_account_leaves_the_snapshot_alone(self):
        self.store(accessToken='live', expiresAt=time.time() + 3600)
        account = await self.repoint({'email': 'other', 'accessToken': 'stale'})
        self.assertEqual(account['accessToken'], 'stale')

    async def test_account_without_an_email_is_not_looked_up(self):
        self.store(accessToken='live', expiresAt=time.time() + 3600)
        self.assertEqual(await self.repoint({}), {})

    async def test_refresh_that_stores_nothing_keeps_the_last_known_token(self):
        # A failed refresh must not blank the lease: sending a stale token
        # earns one 401, sending none aborts the turn outright.
        self.store(accessToken='old', expiresAt=time.time() + 10)
        account = await self.repoint({'email': 'test', 'accessToken': 'old'})
        self.assertEqual(account['accessToken'], 'old')


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
