"""Repair a persistent leased queue, including cross-process claim races."""
SPEC = '''Repair durable_queue.py using only the Python standard library.

Keep the public API Queue(path), enqueue(job_id, payload), claim(owner, now,
lease_seconds), ack(job_id, token, now), and close(). Each Queue owns a SQLite
connection. Independent Queue instances and processes may share the database.
Opening or closing a Queue must not delete jobs. Methods commit their changes
before returning. Correctness must not depend on a process-local lock alone.

enqueue accepts a nonempty string job_id (no stripping) and any JSON-compatible
payload; reject NaN/Infinity and non-JSON values with ValueError before changing
state. Snapshot the payload. Return True for a newly inserted ID, False when the
same ID already exists with the same canonical JSON payload (sorted keys), and
raise ValueError for a conflicting payload. Completed IDs remain reserved.

claim requires nonempty string owner, finite int/float now excluding bool, and
finite positive int/float lease_seconds excluding bool; invalid inputs raise
ValueError without changing state. Return None if no job is available. Otherwise
atomically claim the oldest enqueued unfinished job whose lease is absent or
expired (now >= deadline). Return exactly job_id, payload, token, attempt in a
dict. attempt starts at 1 and increments on every claim/redelivery. token must
be a fresh, nonempty string on every claim, including redelivery to the same
owner. The returned payload is a decoded JSON value, not a serialized string.
An unexpired lease must prevent every other instance/process from claiming that
job. Select and claim must be one transaction; no duplicate successful claims.

ack requires nonempty string job_id/token and finite int/float now excluding
bool; invalid inputs raise ValueError. Return True only when that job has a
matching current token and an unexpired lease. Atomically mark it complete.
Return False for unknown IDs, stale tokens, expired leases, or completed jobs.
Completed jobs are never claimed again. An expired job can be redelivered even
after a failed stale ack. All time comes from the explicit now argument.

You own durable_queue.py only. Do not modify test_visible.py. Run the visible
tests before and after changes. Do not read or write outside this working
directory, use network tools, install packages, or spawn agents. Finish with a
concise description of the behavior you verified.
'''

FILES = {'durable_queue.py': '''import json
import sqlite3

class Queue:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE, payload TEXT, token TEXT, deadline REAL, attempt INTEGER DEFAULT 0, done INTEGER DEFAULT 0)")
        self.db.execute("DELETE FROM jobs")
        self.db.commit()

    def enqueue(self, job_id, payload):
        self.db.execute("INSERT OR REPLACE INTO jobs(job_id,payload) VALUES (?,?)", (job_id,json.dumps(payload)))
        self.db.commit()
        return True

    def claim(self, owner, now, lease_seconds):
        row = self.db.execute("SELECT job_id,payload,attempt FROM jobs WHERE done=0 ORDER BY seq LIMIT 1").fetchone()
        if row is None:
            return None
        token = owner
        self.db.execute("UPDATE jobs SET token=?,deadline=?,attempt=attempt+1 WHERE job_id=?", (token,now+lease_seconds,row[0]))
        self.db.commit()
        return dict(job_id=row[0], payload=json.loads(row[1]), token=token, attempt=row[2]+1)

    def ack(self, job_id, token, now):
        result = self.db.execute("UPDATE jobs SET done=1 WHERE job_id=?", (job_id,))
        self.db.commit()
        return result.rowcount > 0

    def close(self):
        self.db.close()
'''}

VISIBLE = '''import tempfile
import unittest
from pathlib import Path
from durable_queue import Queue

class VisibleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'jobs.db')
        self.q = Queue(self.path)
        self.addCleanup(self.q.close)

    def test_payload(self):
        self.q.enqueue('one', {'value': 1})
        self.assertEqual(self.q.claim('worker', 0, 10)['payload'], {'value': 1})

    def test_open_preserves_jobs(self):
        self.q.enqueue('one', None)
        other = Queue(self.path)
        try:
            self.assertEqual(other.claim('worker', 0, 10)['job_id'], 'one')
        finally:
            other.close()

    def test_stale_ack(self):
        self.q.enqueue('one', None)
        self.q.claim('worker', 0, 10)
        self.assertFalse(self.q.ack('one', 'wrong-token', 1))

if __name__ == '__main__':
    unittest.main()
'''
