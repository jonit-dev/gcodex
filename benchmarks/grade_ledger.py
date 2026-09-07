"""Independent grader for the ledger task."""
import copy
import importlib
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

root = Path(sys.argv.pop(1))
sys.path.insert(0, str(root))
module = importlib.import_module('ledger.core')
summarize = module.summarize


def row(identity='a', account='cash', amount='1'):
    return dict(id=identity, account=account, amount=amount)


class Grade(unittest.TestCase):
    def cli(self, data='', *args):
        return subprocess.run([sys.executable, '-m', 'ledger', *args], cwd=root,
                              input=data, text=True, capture_output=True, timeout=5)

    def test_generator_and_empty(self):
        self.assertEqual(summarize(iter([])), {})
        self.assertEqual(summarize(row(str(i)) for i in range(3)), {'cash': '3.00'})

    def test_normalization_and_immutability(self):
        rows = [row(' a ', ' cash ', '1'), row('a', 'cash', '1.00')]
        before = copy.deepcopy(rows)
        self.assertEqual(summarize(rows), {'cash': '1.00'})
        self.assertEqual(rows, before)

    def test_conflicting_ids(self):
        for other in [row(amount='2'), row(account='other')]:
            with self.assertRaises(ValueError):
                summarize([row(), other])

    def test_validation(self):
        invalid = [None, [], {}, row(identity=' '), row(account=3)]
        invalid += [row(amount=value) for value in [True, 1, 1.0, '', '+1', '1e2',
                                                    'NaN', ' 1', '1 ', '.1', '1.',
                                                    '1.001', '١', '1\n']]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                summarize([value])

    def test_large_exact_arithmetic(self):
        self.assertEqual(summarize([row(amount='999999999999999999999999999999999999.99'),
                                    row('b', amount='0.01')]),
                         {'cash': '1000000000000000000000000000000000000.00'})

    def test_zero_and_sorted_keys(self):
        result = summarize([row('a', 'z', '-0.00'), row('b', 'a', '-1.01'), row('c', 'a', '1.01')])
        self.assertEqual(list(result), ['a', 'z'])
        self.assertEqual(result, {'a': '0.00', 'z': '0.00'})

    def test_random_exact_sums(self):
        rng = random.Random(772)
        totals = {}
        rows = []
        for i in range(300):
            cents = rng.randint(-100000, 100000)
            account = str(rng.randrange(5))
            amount = ('-' if cents < 0 else '') + f'{abs(cents)//100}.{abs(cents)%100:02d}'
            rows.append(row(str(i), account, amount))
            totals[account] = totals.get(account, 0) + cents
        expected = {key: ('-' if value < 0 else '') + f'{abs(value)//100}.{abs(value)%100:02d}'
                    for key, value in sorted(totals.items())}
        self.assertEqual(summarize(rows), expected)

    def test_cli_stdin_and_pretty(self):
        data = json.dumps(row()) + '\n'
        result = self.cli(data, '-', '--pretty')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, json.dumps({'cash': '1.00'}, indent=2) + '\n')

    def test_cli_file(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as fixture:
            fixture.write(json.dumps(row()) + '\n'); fixture.flush()
            result = self.cli('', fixture.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'cash': '1.00'})

    def test_cli_physical_line_number_and_atomic_output(self):
        valid = json.dumps(row())
        for invalid in ['broken', json.dumps(row(amount='2')), json.dumps(row('b', amount=True))]:
            result = self.cli('\n' + valid + '\n\n' + invalid + '\n')
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertIn('line 4', result.stderr)

    def test_cli_empty(self):
        result = self.cli('\n \n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{}\n')

    def test_cli_missing_file(self):
        result = self.cli('', '/does-not-exist-gcodex-benchmark.jsonl')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)


result = unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(Grade))
print(json.dumps({'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors)}))
sys.exit(not result.wasSuccessful())
