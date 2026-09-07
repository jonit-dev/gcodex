"""Correct durable_queue implementation, used only to validate the grader.

The grader must pass this file and fail the starter. It is never given to a
harness under test; it exists so a new acceptance case can be shown to detect a
real defect rather than an over-strict expectation.
"""
import json
import math
import sqlite3
import uuid

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS jobs ("
    "seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE, payload TEXT, "
    "token TEXT, deadline REAL, attempt INTEGER DEFAULT 0, done INTEGER DEFAULT 0)"
)


def _identifier(value):
    if not isinstance(value, str) or not value:
        raise ValueError('expected a nonempty string')
    return value


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('expected a finite int or float')
    return value


def _canonical(payload):
    # Circular and out-of-range values must surface as ValueError before any
    # write. json raises ValueError for NaN/Infinity and circular references,
    # TypeError for non-JSON objects, and RecursionError for deep nesting.
    try:
        return json.dumps(payload, sort_keys=True, allow_nan=False, separators=(',', ':'))
    except (TypeError, RecursionError) as error:
        raise ValueError('payload is not JSON-compatible: ' + str(error)) from error


class Queue:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.execute(SCHEMA)

    def _write(self):
        self.db.execute('BEGIN IMMEDIATE')

    def enqueue(self, job_id, payload):
        _identifier(job_id)
        canonical = _canonical(payload)
        self._write()
        try:
            row = self.db.execute('SELECT payload FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if row is not None:
                if row[0] != canonical:
                    raise ValueError('job_id already exists with a different payload')
                self.db.execute('COMMIT')
                return False
            self.db.execute('INSERT INTO jobs(job_id,payload) VALUES (?,?)', (job_id, canonical))
            self.db.execute('COMMIT')
            return True
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def claim(self, owner, now, lease_seconds):
        _identifier(owner)
        _finite(now)
        _finite(lease_seconds)
        if lease_seconds <= 0:
            raise ValueError('lease_seconds must be positive')
        self._write()
        try:
            row = self.db.execute(
                'SELECT job_id,payload,attempt FROM jobs WHERE done=0 '
                'AND (deadline IS NULL OR ? >= deadline) ORDER BY seq LIMIT 1', (now,)).fetchone()
            if row is None:
                self.db.execute('COMMIT')
                return None
            token = uuid.uuid4().hex
            self.db.execute('UPDATE jobs SET token=?,deadline=?,attempt=attempt+1 WHERE job_id=?',
                            (token, now + lease_seconds, row[0]))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return dict(job_id=row[0], payload=json.loads(row[1]), token=token, attempt=row[2] + 1)

    def ack(self, job_id, token, now):
        _identifier(job_id)
        _identifier(token)
        _finite(now)
        self._write()
        try:
            changed = self.db.execute(
                'UPDATE jobs SET done=1,token=NULL,deadline=NULL WHERE job_id=? AND token=? '
                'AND done=0 AND deadline IS NOT NULL AND ? < deadline', (job_id, token, now)).rowcount
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return changed > 0

    def close(self):
        self.db.close()
