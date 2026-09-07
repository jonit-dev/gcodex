"""The skill keep-list must disable exactly the skills that are not kept.

`skills.config` is a per-skill override layered on top of "everything enabled",
not an allowlist: naming one skill with enabled=true leaves every other skill
on. Measured on a 255-entry library, the catalog was 81 KB of a 114 KB request
and cost 21,169 input tokens per call; keeping 24 skills cost 6,673. So the
override has to name every skill that is NOT kept, and it has to stay correct
as skills are installed and removed.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'skills-policy.py'


class SkillsPolicy(unittest.TestCase):
    def build(self, skills, keep_lines, extra_roots=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        root = base / 'skills'
        for name, has_manifest in skills.items():
            directory = root / name
            directory.mkdir(parents=True)
            if has_manifest:
                (directory / 'SKILL.md').write_text('# skill\n')
        keep = base / 'keep'
        keep.write_text(keep_lines)
        return base, root, keep

    def run_policy(self, keep, roots, home=None):
        env = dict(os.environ)
        env['CODEX_HOME'] = str(home or Path(tempfile.mkdtemp()))
        env['HOME'] = env['CODEX_HOME']
        result = subprocess.run(['python3', str(POLICY), str(keep), *[str(r) for r in roots]],
                                capture_output=True, text=True, timeout=20, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def names_in(self, output):
        import re
        return re.findall(r'name="([^"]+)"', output)

    def test_disables_everything_not_kept(self):
        base, root, keep = self.build(
            {'alpha': True, 'beta': True, 'gamma': True}, 'beta\n')
        out = self.run_policy(keep, [root])
        self.assertEqual(sorted(self.names_in(out)), ['alpha', 'gamma'])
        self.assertNotIn('beta', self.names_in(out))
        self.assertTrue(out.startswith('skills.config=['))
        self.assertIn('enabled=false', out)

    def test_empty_keep_list_emits_nothing(self):
        # The caller then falls back to dropping the catalog entirely; emitting
        # an override that disables every skill would work but is needless.
        base, root, keep = self.build({'alpha': True}, '\n# only a comment\n')
        self.assertEqual(self.run_policy(keep, [root]), '')

    def test_comments_and_blank_lines_are_ignored(self):
        base, root, keep = self.build(
            {'alpha': True, 'beta': True}, '\n# keep alpha\nalpha  # trailing\n\n')
        self.assertEqual(self.names_in(self.run_policy(keep, [root])), ['beta'])

    def test_only_directories_with_a_manifest_count(self):
        base, root, keep = self.build({'real': True, 'notaskill': False}, 'other\n')
        self.assertEqual(self.names_in(self.run_policy(keep, [root])), ['real'])

    def test_dotted_internal_directories_are_never_named(self):
        base, root, keep = self.build({'real': True, '.sys': True}, 'other\n')
        self.assertEqual(self.names_in(self.run_policy(keep, [root])), ['real'])

    def test_keeping_every_installed_skill_emits_nothing(self):
        base, root, keep = self.build({'alpha': True}, 'alpha\n')
        self.assertEqual(self.run_policy(keep, [root]), '')

    def test_names_that_could_break_out_of_the_toml_string_are_dropped(self):
        base, root, keep = self.build({'ok-name': True, 'bad"name': True}, 'other\n')
        out = self.run_policy(keep, [root])
        self.assertEqual(self.names_in(out), ['ok-name'])
        self.assertNotIn('bad', out)

    def test_missing_keep_list_is_not_an_error(self):
        base, root, keep = self.build({'alpha': True}, '')
        self.assertEqual(self.run_policy(base / 'absent', [root]), '')


if __name__ == '__main__':
    unittest.main()
