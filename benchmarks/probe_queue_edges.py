"""Post-hoc queue checks; deliberately separate from the original trial score."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

from grade_queue import load_queue


def check_existing_schema(Queue, path):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE, payload TEXT, token TEXT, deadline REAL, attempt INTEGER DEFAULT 0, done INTEGER DEFAULT 0)')
        db.execute('INSERT INTO jobs(job_id, payload) VALUES (?, ?)', ('existing', '{"v":1}'))
    queue = Queue(path)
    try:
        job = queue.claim('worker', 0, 10)
        assert job['job_id'] == 'existing' and job['payload'] == {'v': 1}, job
        assert queue.ack('existing', job['token'], 1)
    finally:
        queue.close()


def check_circular_payload(Queue, path):
    queue = Queue(path)
    circular = []
    circular.append(circular)
    try:
        try:
            queue.enqueue('circular', circular)
        except ValueError:
            pass
        else:
            raise AssertionError('circular payload was not rejected with ValueError')
        assert queue.claim('worker', 0, 10) is None
    finally:
        queue.close()


if __name__ == '__main__':
    for directory in sys.argv[1:]:
        Queue = load_queue(directory)
        result = {'run': str(Path(directory).resolve()), 'post_hoc': True}
        for check in [check_existing_schema, check_circular_payload]:
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    check(Queue, str(Path(tmp) / 'jobs.db'))
                    result[check.__name__] = 'pass'
                except Exception as error:
                    result[check.__name__] = type(error).__name__ + ': ' + str(error)
        print(json.dumps(result))
