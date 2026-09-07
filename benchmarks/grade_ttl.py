"""Independent behavioral grader; never included in the helper workspace."""
import importlib.util
import json
from pathlib import Path
import random
import sys
import unittest

candidate = Path(sys.argv.pop(1)) / 'cache.py'
spec = importlib.util.spec_from_file_location('candidate', candidate)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Cache = module.TTLCache


class Grade(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.cache = Cache(2, lambda: self.now)

    def test_capacity_validation(self):
        for value in [0, -1, True, False, 2.0, '2', None]:
            with self.assertRaises(ValueError):
                Cache(value)

    def test_none_value(self):
        self.cache.put('a', None)
        self.assertIsNone(self.cache.get('a', 'missing'))

    def test_lru_get(self):
        c = self.cache
        c.put('a', 1); c.put('b', 2); c.get('a'); c.put('c', 3)
        self.assertIsNone(c.get('b'))
        self.assertEqual(c.get('a'), 1)

    def test_lru_overwrite(self):
        c = self.cache
        c.put('a', 1); c.put('b', 2); c.put('a', 4); c.put('c', 3)
        self.assertIsNone(c.get('b'))
        self.assertEqual(c.get('a'), 4)

    def test_deadline_boundary(self):
        self.cache.put('a', 1, 1.5)
        self.now = 1.49
        self.assertEqual(self.cache.get('a'), 1)
        self.now = 1.5
        self.assertIsNone(self.cache.get('a'))

    def test_expired_before_eviction(self):
        c = self.cache
        c.put('live', 1); c.put('expired', 2, 1)
        self.now = 1
        c.put('new', 3)
        self.assertEqual(c.get('live'), 1)

    def test_nonpositive_does_not_evict(self):
        for ttl in [0, -1]:
            c = Cache(1)
            c.put('a', 1); c.put('b', 2, ttl)
            self.assertEqual(c.get('a'), 1)
            c.put('a', 3, ttl)
            self.assertEqual(len(c), 0)

    def test_invalid_ttl_is_atomic(self):
        for ttl in [True, False, '1', [], float('nan'), float('inf'), -float('inf')]:
            c = Cache(1)
            c.put('a', 1)
            with self.assertRaises(ValueError):
                c.put('a', 2, ttl)
            self.assertEqual(c.get('a'), 1)

    def test_len_does_not_touch_lru(self):
        c = self.cache
        c.put('a', 1); c.put('b', 2)
        self.assertEqual(len(c), 2)
        c.put('c', 3)
        self.assertIsNone(c.get('a'))

    def test_overwrite_deadline(self):
        self.cache.put('a', 1, 1)
        self.cache.put('a', 2)
        self.now = 100
        self.assertEqual(self.cache.get('a'), 2)

    def test_randomized_reference(self):
        rng = random.Random(413)
        reference = {}
        cache = Cache(3, lambda: self.now)
        for _ in range(300):
            self.now += rng.choice([0, 0.5, 1])
            reference = {k: v for k, v in reference.items() if v[1] is None or self.now < v[1]}
            key = rng.randrange(5)
            if rng.randrange(2):
                ttl = rng.choice([None, 0, -1, 0.5, 2])
                value = rng.randrange(20)
                cache.put(key, value, ttl)
                reference.pop(key, None)
                if ttl is None or ttl > 0:
                    reference[key] = (value, None if ttl is None else self.now + ttl)
                    while len(reference) > 3:
                        reference.pop(next(iter(reference)))
            else:
                expected = reference.pop(key, ('missing', None))
                self.assertEqual(cache.get(key, 'missing'), expected[0])
                if expected[0] != 'missing':
                    reference[key] = expected
            self.assertEqual(len(cache), len(reference))


suite = unittest.defaultTestLoader.loadTestsFromTestCase(Grade)
result = unittest.TextTestRunner(verbosity=0).run(suite)
print(json.dumps({'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors)}))
sys.exit(not result.wasSuccessful())
