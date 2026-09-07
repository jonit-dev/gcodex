"""Independent persistent-queue acceptance cases; outside candidate directories.

Version 1 is the 12-check grader used for every trial recorded in
benchmarks/README.md. Version 2 adds two cases that post-hoc probing showed
both harnesses could miss (see probe_queue_edges.py). Historical scores are
version 1 scores and are not recomputed; the version is recorded in the
grader output so a score is never compared across versions by accident.
"""
import importlib.util
import json
import multiprocessing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

GRADER_VERSION = 2


def load_queue(directory):
    spec = importlib.util.spec_from_file_location('durable_queue', Path(directory) / 'durable_queue.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.Queue


def claim_worker(directory, path, barrier, output, worker):
    queue = None
    try:
        queue = load_queue(directory)(path)
        barrier.wait(timeout=5)
        output.put(('ok', queue.claim(str(worker), 10, 100)))
    except Exception as error:
        output.put(('error', type(error).__name__ + ': ' + str(error)))
    finally:
        if queue is not None:
            queue.close()


class QueueChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'queue.db')
        self.q = Queue(self.path)
        self.addCleanup(self.q.close)

    def test_restart_preserves_pending_and_completed_ids(self):
        self.q.enqueue('pending', {'a': 1})
        self.q.enqueue('done', None)
        one = self.q.claim('w', 0, 10)
        self.assertTrue(self.q.ack(one['job_id'], one['token'], 1))
        other = Queue(self.path)
        try:
            self.assertFalse(other.enqueue('pending', {'a': 1}))
            self.assertEqual(other.claim('w', 2, 10)['job_id'], 'done')
        finally:
            other.close()

    def test_duplicate_is_canonical_and_does_not_change_order(self):
        self.assertTrue(self.q.enqueue('a', {'z': 1, 'a': [None]}))
        self.q.enqueue('b', False)
        self.assertFalse(self.q.enqueue('a', {'a': [None], 'z': 1}))
        with self.assertRaises(ValueError):
            self.q.enqueue('a', 2)
        self.assertEqual(self.q.claim('w', 0, 10)['job_id'], 'a')

    def test_invalid_payload_and_id_are_atomic(self):
        for payload in [float('nan'), {'x': float('inf')}, {1, 2}, object()]:
            with self.assertRaises(ValueError):
                self.q.enqueue('bad', payload)
        for job_id in ['', None, 1, True]:
            with self.assertRaises(ValueError):
                self.q.enqueue(job_id, None)
        self.assertIsNone(self.q.claim('w', 0, 10))

    def test_payload_snapshot(self):
        payload = {'a': [1]}
        self.q.enqueue('a', payload)
        payload['a'].append(2)
        one = self.q.claim('w', 0, 10)
        self.assertEqual(one['payload'], {'a': [1]})
        one['payload']['a'].append(3)
        self.assertEqual(self.q.claim('w', 10, 10)['payload'], {'a': [1]})

    def test_fifo_skips_leased_and_redelivers_oldest(self):
        for job in ['a', 'b', 'c']:
            self.q.enqueue(job, job)
        self.assertEqual(self.q.claim('w', 0, 10)['job_id'], 'a')
        self.assertEqual(self.q.claim('w', 1, 100)['job_id'], 'b')
        self.assertEqual(self.q.claim('w', 10, 10)['job_id'], 'a')
        self.assertEqual(self.q.claim('w', 11, 10)['job_id'], 'c')

    def test_tokens_and_attempts_change_on_redelivery(self):
        self.q.enqueue('a', 1)
        one = self.q.claim('same-owner', 0, 2)
        two = self.q.claim('same-owner', 2, 2)
        self.assertEqual(set(one), {'job_id', 'payload', 'token', 'attempt'})
        self.assertIsInstance(one['token'], str)
        self.assertTrue(one['token'])
        self.assertNotEqual(one['token'], two['token'])
        self.assertEqual((one['attempt'], two['attempt']), (1, 2))

    def test_ack_rejects_expiry_and_stale_token(self):
        self.q.enqueue('a', None)
        one = self.q.claim('w', 0, 10)
        self.assertFalse(self.q.ack('a', one['token'], 10))
        two = self.q.claim('w', 10, 10)
        self.assertFalse(self.q.ack('a', one['token'], 11))
        self.assertTrue(self.q.ack('a', two['token'], 11))
        self.assertFalse(self.q.ack('a', two['token'], 12))
        self.assertIsNone(self.q.claim('w', 100, 10))

    def test_claim_validation_preserves_pending_job(self):
        self.q.enqueue('a', None)
        invalid = [None, True, '1', float('nan'), float('inf'), float('-inf')]
        for value in invalid:
            with self.assertRaises(ValueError):
                self.q.claim('w', value, 10)
            with self.assertRaises(ValueError):
                self.q.claim('w', 0, value)
        for lease in [0, -1]:
            with self.assertRaises(ValueError):
                self.q.claim('w', 0, lease)
        for owner in ['', None, 1, False]:
            with self.assertRaises(ValueError):
                self.q.claim(owner, 0, 10)
        self.assertEqual(self.q.claim('w', 0, 10)['attempt'], 1)

    def test_ack_validation_preserves_lease(self):
        self.q.enqueue('a', None)
        one = self.q.claim('w', 0, 10)
        for value in [None, True, '1', float('nan'), float('inf')]:
            with self.assertRaises(ValueError):
                self.q.ack('a', one['token'], value)
        for value in ['', None, 1, False]:
            with self.assertRaises(ValueError):
                self.q.ack(value, one['token'], 1)
            with self.assertRaises(ValueError):
                self.q.ack('a', value, 1)
        self.assertFalse(self.q.ack('missing', one['token'], 1))
        self.assertTrue(self.q.ack('a', one['token'], 1))

    def test_independent_connections_observe_commits(self):
        other = Queue(self.path)
        try:
            self.q.enqueue('a', [1, None, False])
            one = other.claim('w', 0, 10)
            self.assertIsNone(self.q.claim('v', 1, 10))
            self.assertTrue(self.q.ack('a', one['token'], 2))
            self.assertIsNone(other.claim('w', 20, 10))
        finally:
            other.close()

    # --- version 2 -----------------------------------------------------------
    # Both cases reproduce a defect observed in real generated output. They are
    # written against the specification, not against either implementation:
    # "Opening or closing a Queue must not delete jobs" and "reject NaN/Infinity
    # and non-JSON values with ValueError before changing state".

    def test_opens_database_with_preexisting_schema(self):
        # A repair task's own starter schema is the schema most likely to
        # already be on disk. Adding a column must not make old databases
        # unreadable, and opening one must not drop its rows.
        legacy = str(Path(self.tmp.name) / 'legacy.db')
        with sqlite3.connect(legacy) as db:
            db.execute('CREATE TABLE jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, '
                       'job_id TEXT UNIQUE, payload TEXT, token TEXT, deadline REAL, '
                       'attempt INTEGER DEFAULT 0, done INTEGER DEFAULT 0)')
            db.execute('INSERT INTO jobs(job_id, payload) VALUES (?, ?)', ('existing', '{"v":1}'))
        db.close()
        queue = Queue(legacy)
        try:
            job = queue.claim('worker', 0, 10)
            self.assertIsNotNone(job, 'pre-existing job was lost when the database was opened')
            self.assertEqual(job['job_id'], 'existing')
            self.assertEqual(job['payload'], {'v': 1})
            self.assertTrue(queue.ack('existing', job['token'], 1))
        finally:
            queue.close()

    def test_circular_payload_is_rejected_atomically(self):
        circular = []
        circular.append(circular)
        with self.assertRaises(ValueError):
            self.q.enqueue('circular', circular)
        self.assertIsNone(self.q.claim('w', 0, 10))
        self.assertTrue(self.q.enqueue('circular', {'ok': True}))


    def concurrent_claims(self, jobs):
        for i in range(jobs):
            self.q.enqueue(str(i), i)
        context = multiprocessing.get_context('spawn')
        barrier, output = context.Barrier(6), context.Queue()
        workers = [context.Process(target=claim_worker,
                   args=(DIRECTORY, self.path, barrier, output, i)) for i in range(6)]
        try:
            for worker in workers:
                worker.start()
            results = [output.get(timeout=8) for _ in workers]
            self.assertTrue(all(kind == 'ok' for kind, _ in results), results)
            return [value for _, value in results if value is not None]
        finally:
            for worker in workers:
                worker.join(timeout=1)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=1)
            output.close()

    def test_six_processes_claim_one_job_once(self):
        claims = self.concurrent_claims(1)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]['job_id'], '0')

    def test_six_processes_claim_distinct_jobs(self):
        claims = self.concurrent_claims(6)
        self.assertEqual(len(claims), 6)
        self.assertEqual({item['job_id'] for item in claims}, {'0', '1', '2', '3', '4', '5'})


if __name__ == '__main__':
    DIRECTORY = sys.argv.pop(1)
    Queue = load_queue(DIRECTORY)
    result = unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(QueueChecks))
    print(json.dumps(dict(grader_version=GRADER_VERSION, tests=result.testsRun,
                          failures=len(result.failures), errors=len(result.errors))))
    sys.exit(not result.wasSuccessful())
