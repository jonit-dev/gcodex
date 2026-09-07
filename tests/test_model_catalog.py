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

    def test_image_input_is_restored_for_gemini_and_claude(self):
        """`models add` cannot declare image input, and Codex reads that field.

        Without this, `-i shot.png` is accepted by the CLI and dropped before
        the request is built, so the model answers as if nothing was attached.
        """
        payload = {'models': [
            {'slug': 'gemini-3.8-flash', 'input_modalities': ['text']},
            {'slug': 'claude-sonnet-4-6', 'input_modalities': ['text', 'image']},
            {'slug': 'gemini-no-modalities'},
        ]}
        models = {m['slug']: m for m in CATALOG['build_catalog'](payload)['models']}
        self.assertEqual(models['gemini-3.8-flash']['input_modalities'], ['text', 'image'])
        self.assertEqual(models['claude-sonnet-4-6']['input_modalities'], ['text', 'image'])
        self.assertEqual(models['gemini-no-modalities']['input_modalities'], ['text', 'image'])

    def test_text_only_backends_keep_their_modalities(self):
        payload = {'models': [
            {'slug': 'gpt-oss-120b-medium', 'input_modalities': ['text']},
            {'slug': 'ollama:qwen3:8b', 'input_modalities': ['text']},
        ]}
        for model in CATALOG['build_catalog'](payload)['models']:
            self.assertEqual(model['input_modalities'], ['text'], model['slug'])

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
