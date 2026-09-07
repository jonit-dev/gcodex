import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BenchmarkTasks(unittest.TestCase):
    def test_queue_starter_is_importable_but_fails_behavioral_grader(self):
        task = runpy.run_path(str(ROOT / 'benchmarks/queue_task.py'))
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in task['FILES'].items():
                (Path(tmp) / name).write_text(content)
            result = subprocess.run(['python3', str(ROOT / 'benchmarks/grade_queue.py'), tmp],
                                    capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        counts = json.loads(result.stdout)
        self.assertEqual(counts['tests'], 14)
        self.assertEqual(counts['grader_version'], 2)
        self.assertGreater(counts['failures'] + counts['errors'], 0)

    def test_ledger_starter_is_importable_but_fails_behavioral_grader(self):
        task = runpy.run_path(str(ROOT / 'benchmarks/ledger_task.py'))
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in task['FILES'].items():
                path = Path(tmp) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            result = subprocess.run(['python3', str(ROOT / 'benchmarks/grade_ledger.py'), tmp],
                                    capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        counts = json.loads(result.stdout)
        self.assertEqual(counts['tests'], 12)
        self.assertGreater(counts['failures'] + counts['errors'], 0)

    def test_ttl_starter_is_importable_but_fails_behavioral_grader(self):
        task = runpy.run_path(str(ROOT / 'benchmarks/run.py'))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'cache.py').write_text(task['STARTER'])
            result = subprocess.run(['python3', str(ROOT / 'benchmarks/grade_ttl.py'), tmp],
                                    capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        counts = json.loads(result.stdout)
        self.assertEqual(counts['tests'], 11)
        self.assertGreater(counts['failures'] + counts['errors'], 0)


class QueueGraderVersionTwo(unittest.TestCase):
    """The two version-2 cases must detect real defects, not merely be strict.

    Each case is checked twice: it passes a correct reference implementation,
    and it fails a copy of that reference with one specific defect reintroduced.
    A case that cannot fail proves nothing; a case that fails the reference is
    over-strict. Both defects were first seen in real generated output.
    """

    REFERENCE = ROOT / 'benchmarks/reference_queue.py'

    def grade(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'durable_queue.py').write_text(source)
            result = subprocess.run(['python3', str(ROOT / 'benchmarks/grade_queue.py'), tmp],
                                    capture_output=True, text=True, timeout=60)
        return result, json.loads(result.stdout)

    def test_reference_passes_every_case(self):
        result, counts = self.grade(self.REFERENCE.read_text())
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(counts, {'grader_version': 2, 'tests': 14, 'failures': 0, 'errors': 0})

    def defect(self, old, new):
        source = self.REFERENCE.read_text()
        self.assertIn(old, source)
        return source.replace(old, new, 1)

    def test_detects_schema_change_that_breaks_existing_databases(self):
        # The observed defect: a new column is added to CREATE TABLE, so an
        # older database is opened successfully but every query naming that
        # column fails. Only the pre-existing-schema case sees it.
        broken = self.defect(
            'attempt INTEGER DEFAULT 0, done INTEGER DEFAULT 0)"',
            'attempt INTEGER DEFAULT 0, done INTEGER DEFAULT 0, owner TEXT)"')
        broken = broken.replace(
            "'SELECT job_id,payload,attempt FROM jobs WHERE done=0 '",
            "'SELECT job_id,payload,attempt,owner FROM jobs WHERE done=0 '", 1)
        result, counts = self.grade(broken)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('test_opens_database_with_preexisting_schema', result.stderr)
        self.assertNotIn('test_circular_payload', result.stderr)

    def test_detects_circular_payload_that_is_not_a_value_error(self):
        # The observed defect: the implementation validates the payload with its
        # own recursive walk that has no cycle detection, and does not treat the
        # resulting RecursionError as a rejection. json.dumps alone would have
        # raised ValueError, so the defect only appears once the walk is added.
        broken = self.defect(
            '    except (TypeError, RecursionError) as error:',
            '    except TypeError as error:')
        broken = broken.replace(
            "        return json.dumps(payload, sort_keys=True, allow_nan=False, separators=(',', ':'))",
            "        _walk(payload)\n"
            "        return json.dumps(payload, sort_keys=True, allow_nan=False, separators=(',', ':'))", 1)
        broken = broken.replace(
            'def _canonical(payload):',
            'def _walk(value):\n'
            '    if isinstance(value, dict):\n'
            '        for item in value.values():\n'
            '            _walk(item)\n'
            '    elif isinstance(value, (list, tuple)):\n'
            '        for item in value:\n'
            '            _walk(item)\n'
            '\n'
            '\n'
            'def _canonical(payload):', 1)
        self.assertIn('_walk(payload)', broken)
        result, counts = self.grade(broken)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('test_circular_payload_is_rejected_atomically', result.stderr)
        self.assertNotIn('test_opens_database_with_preexisting_schema', result.stderr)


class TokenAccounting(unittest.TestCase):
    """Usage must come from each harness's own report, or be marked unavailable."""

    RUN = runpy.run_path(str(ROOT / 'benchmarks/run.py'))

    def usage(self, harness, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'transcript.log'
            path.write_text(text)
            return self.RUN['read_usage'](harness, path)

    def test_gcodex_sums_every_turn(self):
        lines = [
            '{"type":"thread.started","thread_id":"t"}',
            'Reading additional input from stdin...',
            '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":10,'
            '"output_tokens":5,"reasoning_output_tokens":2}}',
            '{"type":"turn.completed","usage":{"input_tokens":200,"cached_input_tokens":20,'
            '"output_tokens":7,"reasoning_output_tokens":3}}',
        ]
        usage = self.usage('gcodex', '\n'.join(lines) + '\n')
        self.assertTrue(usage['available'])
        self.assertEqual((usage['input'], usage['output'], usage['total']), (300, 12, 312))
        self.assertEqual((usage['cached_input'], usage['reasoning_output'], usage['turns']), (30, 5, 2))

    def test_agy_uses_final_result_object(self):
        line = ('{"conversation_id":"c","status":"SUCCESS","num_turns":4,"usage":'
                '{"input_tokens":1000,"output_tokens":50,"thinking_tokens":40,'
                '"cache_read_tokens":9,"total_tokens":1050}}')
        usage = self.usage('agy', line + '\n')
        self.assertTrue(usage['available'])
        self.assertEqual((usage['input'], usage['output'], usage['total']), (1000, 50, 1050))
        self.assertEqual((usage['cached_input'], usage['reasoning_output'], usage['turns']), (9, 40, 4))

    def test_missing_usage_is_reported_not_guessed(self):
        for harness, text in [('gcodex', '{"type":"turn.started"}\n'), ('agy', 'plain text\n')]:
            usage = self.usage(harness, text)
            self.assertFalse(usage['available'])
            self.assertNotIn('total', usage)
