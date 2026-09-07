"""Run one controlled gcodex/agy comparison trial (never retries a trial).

Token accounting: each harness reports its own usage, so nothing is
estimated. Codex emits one `turn.completed` event per turn under --json and
agy emits a single result object under --output-format json. Both count an
agentic loop's repeated history resends inside their input totals, so
`total = input + output` is the comparable figure. Cached and reasoning
counts are recorded separately and are already included in those totals;
they are reported, not subtracted.
"""
import argparse
import hashlib
import json
import os
import signal
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SPEC = '''Implement cache.py using only the Python standard library.

Provide TTLCache(capacity, clock=time.monotonic), with put(key, value, ttl=None),
get(key, default=None), and len(cache). Keys are hashable. Values may include None.
Capacity must be a positive integer, excluding bool; invalid capacity raises ValueError.
Successful get and every retained put make that key most recently used. Missing get
returns default and does not alter order. When full, evict the least recently used
unexpired entry. Expired entries must be removed before considering capacity.
TTL is measured from put using the injected clock; expiration is inclusive (now >=
deadline). ttl=None never expires. A zero or negative TTL removes that key if present,
without evicting any other entry. TTL must be int or float, excluding bool, and finite;
invalid TTL raises ValueError without changing state. Overwriting a key replaces its
value and deadline. len(cache) excludes expired entries and does not update recency.
Do not modify test_visible.py. Run it before and after your change. You own cache.py
only. Do not read or write outside this working directory, use network tools, install
packages, or spawn agents. Finish with a concise description of what works.
'''
STARTER = '''import time

class TTLCache:
    def __init__(self, capacity, clock=time.monotonic):
        self.capacity = capacity
        self.clock = clock
        self.items = {}

    def put(self, key, value, ttl=None):
        self.items[key] = value

    def get(self, key, default=None):
        return self.items.get(key, default)

    def __len__(self):
        return len(self.items)
'''
VISIBLE = '''import unittest
from cache import TTLCache

class VisibleTests(unittest.TestCase):
    def test_get(self):
        c = TTLCache(2)
        c.put("a", 3)
        self.assertEqual(c.get("a"), 3)

    def test_capacity(self):
        c = TTLCache(1)
        c.put("a", 1)
        c.put("b", 2)
        self.assertIsNone(c.get("a"))

    def test_expiry(self):
        now = [10]
        c = TTLCache(2, lambda: now[0])
        c.put("a", 1, ttl=2)
        now[0] = 12
        self.assertIsNone(c.get("a"))

if __name__ == "__main__":
    unittest.main()
'''



def read_usage(harness, transcript):
    """Sum the harness's own reported token usage; never estimate."""
    try:
        text = transcript.read_text(errors='replace')
    except OSError:
        return {'available': False, 'reason': 'transcript unreadable'}
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if harness == 'gcodex':
        turns = [e['usage'] for e in events
                 if e.get('type') == 'turn.completed' and isinstance(e.get('usage'), dict)]
        if not turns:
            return {'available': False, 'reason': 'no turn.completed usage event'}
        totals = {'input': 0, 'output': 0, 'cached_input': 0, 'reasoning_output': 0}
        for usage in turns:
            totals['input'] += int(usage.get('input_tokens') or 0)
            totals['output'] += int(usage.get('output_tokens') or 0)
            totals['cached_input'] += int(usage.get('cached_input_tokens') or 0)
            totals['reasoning_output'] += int(usage.get('reasoning_output_tokens') or 0)
        totals.update(available=True, turns=len(turns), raw=turns[-1])
    else:
        results = [e['usage'] for e in events if isinstance(e.get('usage'), dict)]
        if not results:
            return {'available': False, 'reason': 'no usage object in json result'}
        usage = results[-1]
        totals = {'input': int(usage.get('input_tokens') or 0),
                  'output': int(usage.get('output_tokens') or 0),
                  'cached_input': int(usage.get('cache_read_tokens') or 0),
                  'reasoning_output': int(usage.get('thinking_tokens') or 0),
                  'available': True,
                  'turns': int(next((e.get('num_turns') for e in events
                                     if isinstance(e.get('num_turns'), int)), 0)),
                  'raw': usage}
    totals['total'] = totals['input'] + totals['output']
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('harness', choices=['gcodex', 'agy'])
    parser.add_argument('--task', choices=['ttl', 'ledger', 'queue'], default='ttl')
    args = parser.parse_args()
    if args.task == 'queue':
        from queue_task import SPEC as task_spec, FILES as files, VISIBLE as visible
    elif args.task == 'ledger':
        from ledger_task import SPEC as task_spec, FILES as files, VISIBLE as visible
    else:
        task_spec, files, visible = SPEC, {'cache.py': STARTER}, VISIBLE
    base = ROOT / '.benchmarks'
    subprocess.run(['git', 'check-ignore', '--quiet', str(base / 'probe')], cwd=ROOT, check=True)
    base.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=args.harness + '-' + args.task + '-', dir=base))
    for name, contents in files.items():
        target = run / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    (run / 'test_visible.py').write_text(visible)
    (run / 'TASK.md').write_text(task_spec)
    if args.harness == 'gcodex':
        command = ['gcodex', '--med', 'exec', '--json', '--sandbox', 'workspace-write',
                   '--skip-git-repo-check', '--ephemeral', task_spec]
    else:
        command = ['agy', '--model', 'gemini-3.8-flash-medium', '--effort', 'medium',
                   '--sandbox', '--dangerously-skip-permissions', '--print-timeout', '4m',
                   '--output-format', 'json', '--print', task_spec]
    print(json.dumps({'event': 'started', 'harness': args.harness, 'run': str(run)}), flush=True)
    log = run / 'transcript.log'
    fd = os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    started = time.monotonic()
    with os.fdopen(fd, 'w') as output:
        process = subprocess.Popen(command, cwd=run, stdout=output, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            exit_code = process.wait(timeout=300)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            exit_code = 'timeout'
    duration = time.monotonic() - started
    grader = subprocess.Popen(['python3', str(ROOT / f'benchmarks/grade_{args.task}.py'), str(run)],
                              cwd=run, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True)
    try:
        grading_output, grading_errors = grader.communicate(timeout=20)
        grading_exit = grader.returncode
    except subprocess.TimeoutExpired:
        os.killpg(grader.pid, signal.SIGKILL)
        grading_output, grading_errors = grader.communicate()
        grading_exit = 'timeout'
    result = dict(harness=args.harness, task=args.task, model='gemini-3.8-flash-medium',
                  run=str(run), spec_sha256=hashlib.sha256(task_spec.encode()).hexdigest(),
                  duration_seconds=round(duration, 2), exit_code=exit_code,
                  visible_tests_unchanged=(run / 'test_visible.py').exists() and (run / 'test_visible.py').read_text() == visible,
                  tokens=read_usage(args.harness, log),
                  grader_exit_code=grading_exit, grading=grading_output,
                  grading_errors=grading_errors[-3000:])
    (run / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
