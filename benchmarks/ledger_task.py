"""A multi-file, transactional JSONL command-line implementation task."""
SPEC = '''Complete the ledger package using only the Python standard library.

ledger.core.summarize(records) consumes an iterable of transaction dictionaries
and returns a dict of account -> balance formatted with exactly two decimal
places. Every record requires id, account, and amount. id and account must be
strings nonempty after stripping surrounding whitespace; use the stripped values.
amount must be a string matching -?[0-9]+(\\.[0-9]{1,2})? exactly. Whitespace,
exponents, NaN, floats, booleans, and leading + are invalid. Use exact arithmetic;
amounts are not limited to machine integer size or Decimal's default precision.
Normalize zero balances to 0.00. Preserve input dictionaries unchanged. Ignore
additional fields. Empty input returns {}. Invalid input raises ValueError.

IDs are global across accounts. Repeated IDs with the same normalized account
and numeric amount count only once (1 and 1.00 are equivalent). A repeated ID
with different account or numeric amount raises ValueError. Return accounts in
lexicographic insertion order.

python3 -m ledger [FILE|-] [--pretty] reads JSON Lines from FILE, or stdin when
FILE is omitted or '-'. Ignore blank lines. On success print one JSON object
with sorted account keys and a final newline; --pretty uses indent=2. On any
malformed JSON, invalid record, or conflicting duplicate, print a diagnostic
containing 'line N' on stderr using the physical one-based line number, exit 2,
and print nothing on stdout. File-read failures also exit 2 with stderr and no
stdout. Do not emit partial totals before validation completes.

You own ledger/*.py. Do not modify test_visible.py. Run python3 -m unittest -v
before and after changes. Do not read or write outside this working directory,
use network tools, install packages, or spawn agents. Finish with a concise
description of the behavior you verified.
'''

FILES = {
    'ledger/__init__.py': '',
    'ledger/core.py': '''def summarize(records):
    result = {}
    for record in records:
        account = record['account']
        result[account] = result.get(account, 0) + float(record['amount'])
    return {key: str(value) for key, value in result.items()}
''',
    'ledger/__main__.py': '''import json
import sys
from .core import summarize

records = [json.loads(line) for line in sys.stdin if line.strip()]
print(json.dumps(summarize(records)))
''',
}

VISIBLE = '''import subprocess
import sys
import unittest
from ledger.core import summarize

class VisibleTests(unittest.TestCase):
    def test_exact_sum(self):
        self.assertEqual(summarize([
            {'id': 'a', 'account': 'cash', 'amount': '0.10'},
            {'id': 'b', 'account': 'cash', 'amount': '0.20'},
        ]), {'cash': '0.30'})

    def test_duplicate(self):
        row = {'id': 'a', 'account': 'cash', 'amount': '1'}
        self.assertEqual(summarize([row, row]), {'cash': '1.00'})

    def test_cli_invalid(self):
        result = subprocess.run([sys.executable, '-m', 'ledger'], input='broken\\n',
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('line 1', result.stderr)

if __name__ == '__main__':
    unittest.main()
'''
