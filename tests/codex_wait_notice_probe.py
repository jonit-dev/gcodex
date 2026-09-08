"""Verify notices with the installed Codex CLI against a local-only stub."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import runpy
import subprocess
import tempfile
import threading
import time

SAFETY = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'gateway_safety.py'))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.wfile.write(SAFETY['wait_notice_events'](1))
        self.wfile.flush()
        time.sleep(1)
        item = dict(id='msg_answer', type='message', role='assistant', phase='final_answer',
                    status='completed', content=[dict(type='output_text', text='Hello after waiting.',
                                                    annotations=[])])
        for event in [dict(type='response.created', response=dict(id='resp_probe', status='in_progress')),
                      dict(type='response.output_item.added', output_index=0,
                           item=dict(item, status='in_progress', content=[])),
                      dict(type='response.output_text.delta', item_id=item['id'], output_index=0,
                           content_index=0, delta='Hello after waiting.'),
                      dict(type='response.output_item.done', output_index=0, item=item),
                      dict(type='response.completed', response=dict(id='resp_probe', status='completed',
                           output=[item], usage=dict(input_tokens=1, output_tokens=1, total_tokens=2)))]:
            self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
        self.wfile.flush()


with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server, tempfile.TemporaryDirectory() as tmp:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    command = ['codex', 'exec', '--json', '--ephemeral', '--skip-git-repo-check',
               '-m', 'gpt-5.4', '-c', 'model_provider="wait_probe"',
               '-c', f'model_providers.wait_probe={{name="Local probe",base_url="http://127.0.0.1:{port}/v1",wire_api="responses",request_max_retries=0,stream_max_retries=0}}',
               'hi']
    result = subprocess.run(command, cwd=tmp, env=dict(os.environ, CODEX_HOME=tmp),
                            capture_output=True, text=True, timeout=30)
    server.shutdown()
    assert result.returncode == 0, result.stderr + result.stdout
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    messages = [e['item'].get('text', '') for e in events if e.get('type') == 'item.completed']
    assert any('Rate limited. Please wait 1 seconds' in text for text in messages), result.stdout
    assert 'Hello after waiting.' in messages, result.stdout
    print('PASS: installed Codex renders the wait notice and the subsequent answer.')
