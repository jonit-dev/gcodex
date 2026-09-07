"""Run the agreed paired comparison: 3 tasks x 2 pairs, alternating run order.

Order alternates within each task so a warm cache, a cooling rate limit, or any
drift over the session cannot favour one harness systematically. Trials are
serial on purpose: the gateway permits one in-flight request, and concurrent
runs would corrupt both the timing and the token comparison.
"""
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PLAN = [
    ('ttl', ['gcodex', 'agy']),
    ('ttl', ['agy', 'gcodex']),
    ('ledger', ['agy', 'gcodex']),
    ('ledger', ['gcodex', 'agy']),
    ('queue', ['gcodex', 'agy']),
    ('queue', ['agy', 'gcodex']),
]


def main():
    out = ROOT / '.benchmarks' / 'paired-results.jsonl'
    skip = set()
    for entry in sys.argv[1:]:
        task, harness = entry.split(':')
        skip.add((task, harness))
    with out.open('a') as sink:
        for index, (task, order) in enumerate(PLAN):
            for harness in order:
                if (task, harness) in skip:
                    skip.discard((task, harness))
                    print(f'skip pair {index} {task} {harness} (already run)', flush=True)
                    continue
                print(f'--- pair {index} {task} {harness} ---', flush=True)
                proc = subprocess.run(['python3', str(ROOT / 'benchmarks/run.py'), harness,
                                       '--task', task],
                                      capture_output=True, text=True, timeout=420)
                record = None
                for line in proc.stdout.splitlines():
                    if line.startswith('{') and '"harness"' in line and '"grading"' in line:
                        record = json.loads(line)
                if record is None:
                    record = {'harness': harness, 'task': task, 'pair': index,
                              'error': proc.stdout[-2000:] + proc.stderr[-2000:]}
                record['pair'] = index
                sink.write(json.dumps(record) + '\n')
                sink.flush()
                tokens = record.get('tokens') or {}
                print(json.dumps({'pair': index, 'task': task, 'harness': harness,
                                  'seconds': record.get('duration_seconds'),
                                  'grading': (record.get('grading') or '').strip(),
                                  'total_tokens': tokens.get('total')}), flush=True)
                time.sleep(10)


if __name__ == '__main__':
    main()
