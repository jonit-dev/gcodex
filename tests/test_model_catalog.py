import json
from pathlib import Path
import runpy
import tempfile
import unittest

CATALOG = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'model-catalog.py'))


class ModelCatalog(unittest.TestCase):
    def test_native_tools_and_instructions(self):
        payload = {'models': [{'slug': 'gemini-test', 'base_instructions': 'test instructions'}]}
        result = CATALOG['build_catalog'](payload)
        model = result['models'][0]
        self.assertEqual(model['apply_patch_tool_type'], 'freeform')
        self.assertEqual(model['shell_type'], 'unified_exec')
        self.assertEqual(model['model_messages']['instructions_template'], 'test instructions')
        self.assertNotIn('apply_patch_tool_type', payload['models'][0])

    def test_invalid_catalog_does_not_replace_working_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'models.json'
            path.write_text('original')
            with self.assertRaises(ValueError):
                CATALOG['write_catalog'](path, {'models': []})
            self.assertEqual(path.read_text(), 'original')

    def test_private_atomic_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'models.json'
            CATALOG['write_catalog'](path, {'models': [{'slug': 'gemini-test'}]})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())['models'][0]['slug'], 'gemini-test')
