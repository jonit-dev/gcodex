"""Rate-limit notices must survive waits, retries, and later HTTP failures."""
import asyncio
import json
from pathlib import Path
import runpy
import unittest

SAFETY = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'gateway_safety.py'))


class WaitNotices(unittest.IsolatedAsyncioTestCase):
    async def run_app(self, *, stream=True, early=True, failure=False):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {'type': 'http.disconnect'}

        async def app(scope, receive, send):
            SAFETY['enable_wait_notices'](stream)
            if early:
                await SAFETY['notify_wait'](299.2)
                self.assertEqual(bool(sent), stream)
            await send(dict(type='http.response.start', status=429 if failure else 200,
                            headers=[(b'content-type', b'text/event-stream')]))
            if not early:
                await SAFETY['notify_wait'](299.2)
            if failure:
                await send(dict(type='http.response.body', body=b'{"detail":"limit exceeded"}',
                                more_body=False))
            else:
                await SAFETY['notify_wait'](120)
                await send(dict(type='http.response.body',
                                body=b'data: {"type":"response.completed"}\n\n', more_body=False))

        await SAFETY['RequestLeaseMiddleware'](app)({'type': 'http'}, receive, send)
        return sent

    async def test_both_wait_locations_render_without_duplicate_headers(self):
        for early in (True, False):
            sent = await self.run_app(early=early)
            self.assertEqual(len([m for m in sent if m['type'] == 'http.response.start']), 1)
            body = b''.join(m.get('body', b'') for m in sent).decode()
            events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')]
            notices = [e['item'] for e in events if e['type'] == 'response.output_item.done']
            self.assertEqual(len(notices), 2)
            self.assertNotEqual(notices[0]['id'], notices[1]['id'])
            self.assertEqual(notices[0]['phase'], 'commentary')
            self.assertIn('wait 300 seconds', notices[0]['content'][0]['text'])
            self.assertEqual(events[-1]['type'], 'response.completed')

    async def test_later_http_error_is_terminal_stream_error(self):
        sent = await self.run_app(failure=True)
        event = json.loads(sent[-1]['body'].decode()[6:])
        self.assertEqual(event['type'], 'response.failed')
        self.assertEqual(event['response']['error']['message'], 'limit exceeded')
        self.assertFalse(sent[-1]['more_body'])

    async def test_non_streaming_retains_http_error(self):
        sent = await self.run_app(stream=False, failure=True)
        self.assertEqual(sent[0]['status'], 429)
        self.assertEqual(len(sent), 2)

    async def test_notice_context_is_reset_after_request(self):
        await self.run_app()
        self.assertIsNone(SAFETY['_WAIT_NOTICES'].get())


if __name__ == '__main__':
    unittest.main()
