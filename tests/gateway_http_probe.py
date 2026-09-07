"""Exercise request leases over real localhost HTTP, without Google or credentials.

Run with the gateway virtual environment's Python (FastAPI, httpx, uvicorn).
"""
import asyncio
import json
from pathlib import Path
import runpy
import socket
import sys
import threading
import types
import unittest

import httpx
import uvicorn
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

NEGATIVE_CONTROL = '--negative-control' in sys.argv
if NEGATIVE_CONTROL:
    sys.argv.remove('--negative-control')


class Manager:
    def __init__(self):
        self._lock = threading.Lock()
        self._in_flight = {'fixture': 0}
        self.acquired = self.released = self.peak = 0

    def acquire_account(self, model):
        with self._lock:
            if self._in_flight['fixture']:
                return None
            self._in_flight['fixture'] += 1
            self.acquired += 1
            self.peak = max(self.peak, self._in_flight['fixture'])
            return {'email': 'fixture'}

    def release_account(self, email):
        with self._lock:
            self._in_flight[email] -= 1
            self.released += 1

    def snapshot(self):
        with self._lock:
            return dict(in_flight=self._in_flight['fixture'], acquired=self.acquired,
                        released=self.released, peak=self.peak)


class HTTPLeaseProbe(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        safety = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'gateway_safety.py'))
        storage = types.ModuleType('http_fixture.storage')
        storage.load_accounts_read_only = lambda: {'accounts': [{'email': 'fixture'}]}
        sys.modules['http_fixture.storage'] = storage
        self.addCleanup(sys.modules.pop, 'http_fixture.storage', None)
        safety['acquire_serially'].__globals__['__package__'] = 'http_fixture'
        if NEGATIVE_CONTROL:
            safety['acquire_serially'].__globals__['QUEUE_TIMEOUT_SECONDS'] = 0.2
        self.manager = Manager()
        app = FastAPI()
        if not NEGATIVE_CONTROL:
            app.add_middleware(safety['RequestLeaseMiddleware'])

        @app.get('/stream')
        async def stream():
            await safety['acquire_serially'](self.manager, 'fixture', run_in_threadpool)

            async def body():
                yield b'data: acquired\n\n'
                # Only disconnect cancellation ends this response.
                await asyncio.Event().wait()

            return StreamingResponse(body(), media_type='text/event-stream')

        @app.get('/next')
        async def next_request():
            account = await safety['acquire_serially'](self.manager, 'fixture', run_in_threadpool)
            await asyncio.sleep(0.02)
            safety['release_tracked_account'](self.manager, account['email'])
            return {'ok': True}

        @app.get('/state')
        async def state():
            return self.manager.snapshot()

        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(128)
        self.port = self.listener.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
        self.thread = threading.Thread(target=self.server.run,
                                       kwargs={'sockets': [self.listener]}, daemon=True)
        self.thread.start()
        self.client = httpx.AsyncClient(base_url=f'http://127.0.0.1:{self.port}', timeout=5)
        for _ in range(100):
            if self.server.started:
                return
            await asyncio.sleep(0.02)
        self.fail('fixture HTTP server did not start within two seconds')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.server.should_exit = True
        await asyncio.to_thread(self.thread.join, 5)
        self.listener.close()
        self.assertFalse(self.thread.is_alive(), 'fixture server failed to stop')

    async def test_disconnect_and_queued_followups(self):
        for cycle in range(20):
            async with self.client.stream('GET', '/stream') as response:
                self.assertEqual(response.status_code, 200)
                async for line in response.aiter_lines():
                    if line == 'data: acquired':
                        break
                else:
                    self.fail('stream ended before acquisition marker')
            # A real socket disconnect must release the slot for the next client.
            response = await self.client.get('/next')
            self.assertEqual(response.status_code, 200, (cycle, response.text))
            state = (await self.client.get('/state')).json()
            self.assertEqual(state['in_flight'], 0, state)
            self.assertEqual(state['acquired'], state['released'], state)

        responses = await asyncio.gather(*(self.client.get('/next') for _ in range(8)))
        self.assertEqual([r.status_code for r in responses], [200] * 8)
        state = (await self.client.get('/state')).json()
        self.assertEqual(state, dict(in_flight=0, acquired=48, released=48, peak=1))
        print(json.dumps({'disconnect_cycles': 20, 'concurrent_followups': 8, **state}))


if __name__ == '__main__':
    unittest.main()
