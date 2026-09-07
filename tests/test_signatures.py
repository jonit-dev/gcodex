import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'thought_signatures.py'


class DurableSignatures(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(os.environ, GCODEX_SIGNATURE_DIR=self.directory.name)
        environment.start()
        self.addCleanup(environment.stop)
        self.cache = runpy.run_path(str(MODULE))

    def test_survives_new_process(self):
        self.cache['remember']('call-one', 'test-signature')
        code = 'import runpy,sys; c=runpy.run_path(sys.argv[1]); assert c["recall"]("call-one") == "test-signature"'
        result = subprocess.run(['python3', '-B', '-c', code, str(MODULE)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        db = Path(self.directory.name) / 'signatures.sqlite3'
        self.assertEqual(db.stat().st_mode & 0o777, 0o600)

    def test_lru_eviction_still_replays_from_disk(self):
        with patch.dict(self.cache['remember'].__globals__, MAX_ENTRIES=2):
            for i in range(3):
                self.cache['remember'](str(i), 'sig-' + str(i))
            self.assertNotIn('0', self.cache['_SIGNATURES'])
            self.assertEqual(self.cache['attach']({}, '0'), {'thoughtSignature': 'sig-0'})

    def test_disk_bound(self):
        with patch.dict(self.cache['remember'].__globals__, MAX_DISK_ENTRIES=2):
            for i in range(3):
                self.cache['remember'](str(i), 'signature')
        other = runpy.run_path(str(MODULE))
        self.assertIsNone(other['recall']('0'))
        self.assertEqual(other['recall']('2'), 'signature')

    def test_expiry(self):
        with patch('time.time', return_value=100):
            self.cache['remember']('old', 'signature')
        other = runpy.run_path(str(MODULE))
        self.assertIsNone(other['recall']('old'))

    def test_warm_cache_obeys_expiry(self):
        with patch('time.time', return_value=100):
            self.cache['remember']('old', 'signature')
        with patch('time.time', return_value=101 + self.cache['RETENTION_SECONDS']):
            self.assertIsNone(self.cache['recall']('old'))

    def test_warm_recall_refresh_survives_restart(self):
        retention = self.cache['RETENTION_SECONDS']
        with patch('time.time', return_value=100):
            self.cache['remember']('active', 'signature')
        with patch('time.time', return_value=99 + retention):
            self.assertEqual(self.cache['recall']('active'), 'signature')
        other = runpy.run_path(str(MODULE))
        with patch('time.time', return_value=101 + retention):
            self.assertEqual(other['recall']('active'), 'signature')

    def test_unsafe_directory_rejected(self):
        Path(self.directory.name).chmod(0o755)
        with self.assertRaises(RuntimeError):
            self.cache['remember']('call', 'signature')

    def test_symlink_database_rejected(self):
        target = Path(self.directory.name) / 'signatures.sqlite3'
        target.symlink_to(Path(self.directory.name) / 'missing')
        with self.assertRaises(OSError):
            self.cache['remember']('call', 'signature')

    def test_invalid_values_do_not_create_database(self):
        self.cache['remember']('', 'signature')
        self.cache['remember']('call', None)
        self.assertIsNone(self.cache['recall'](None))
        self.assertFalse((Path(self.directory.name) / 'signatures.sqlite3').exists())
