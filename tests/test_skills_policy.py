"""The keep-list must trim the prompt without breaking skill tagging.

Codex ships every installed skill's name, description and path in the developer
message on every model call: 81 KB of a 114 KB request here, 21,169 input
tokens, resent 12-15 times per task against a per-request subscription quota.

The first trim used `skills.config` with enabled=false for every skill outside
the keep-list. It cut the prompt, and it also removed those skills from the `$`
picker in the composer -- tagging one by hand stopped working, which is the
cheapest way to use a skill because it costs nothing until it is used. So the
trim now drops the catalog with `skills.include_instructions=false` and
re-advertises the keep-list through the profile's own developer_instructions.
Everything stays enabled and taggable.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'skills-policy.py'


class SkillsPolicy(unittest.TestCase):
    def build(self, skills, keep_lines, instructions='Base guidance.'):
        """skills: name -> SKILL.md text, or None for a directory with no manifest."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        root = base / 'skills'
        for name, manifest in skills.items():
            directory = root / name
            directory.mkdir(parents=True)
            if manifest is not None:
                (directory / 'SKILL.md').write_text(manifest)
        keep = base / 'keep'
        keep.write_text(keep_lines)
        config = base / 'gcodex.config.toml'
        if instructions is None:
            config.write_text('model = "gemini-3.8-flash"\n')
        else:
            config.write_text('model = "gemini-3.8-flash"\ndeveloper_instructions = """\n%s\n"""\n'
                              % instructions)
        return base, root, keep, config

    def run_policy(self, keep, roots, config=None, home=None):
        env = dict(os.environ)
        env['CODEX_HOME'] = str(home or Path(tempfile.mkdtemp()))
        env['HOME'] = env['CODEX_HOME']
        argv = ['python3', str(POLICY), str(keep)]
        if config is not None:
            argv += ['--config', str(config)]
        argv += [str(r) for r in roots]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def instructions_from(self, output):
        self.assertTrue(output.startswith('developer_instructions='), output[:80])
        # `-c key=value` parses the value as TOML, so the whole line must too.
        return tomllib.loads(output)['developer_instructions']

    def manifest(self, description):
        return '---\nname: skill\ndescription: %s\n---\n\n# Skill\n' % description

    def test_advertises_only_the_kept_skills(self):
        base, root, keep, config = self.build(
            {'alpha': self.manifest('does alpha'),
             'beta': self.manifest('does beta'),
             'gamma': self.manifest('does gamma')}, 'beta\n')
        text = self.instructions_from(self.run_policy(keep, [root], config))
        self.assertIn('- beta: does beta', text)
        self.assertIn(str(root / 'beta' / 'SKILL.md'), text)
        self.assertNotIn('alpha', text)
        self.assertNotIn('gamma', text)

    def test_never_disables_a_skill(self):
        # A disabled skill vanishes from the composer's `$` picker, so the user
        # cannot tag it by hand. That is the regression this file exists for.
        base, root, keep, config = self.build(
            {'alpha': self.manifest('a'), 'beta': self.manifest('b')}, 'beta\n')
        out = self.run_policy(keep, [root], config)
        self.assertNotIn('enabled=false', out)
        self.assertNotIn('skills.config', out)

    def test_profile_instructions_are_kept_ahead_of_the_catalog(self):
        base, root, keep, config = self.build(
            {'alpha': self.manifest('a')}, 'alpha\n', instructions='Verify against old data.')
        text = self.instructions_from(self.run_policy(keep, [root], config))
        self.assertTrue(text.startswith('Verify against old data.'), text[:60])
        self.assertLess(text.index('Verify against old data.'), text.index('- alpha'))

    def test_missing_profile_instructions_still_advertise(self):
        base, root, keep, config = self.build(
            {'alpha': self.manifest('a')}, 'alpha\n', instructions=None)
        self.assertIn('- alpha', self.instructions_from(self.run_policy(keep, [root], config)))

    def test_unreadable_profile_emits_nothing(self):
        # Emitting the catalog alone would silently drop the profile's own
        # verification guidance, which the override replaces wholesale.
        base, root, keep, config = self.build({'alpha': self.manifest('a')}, 'alpha\n')
        self.assertEqual(self.run_policy(keep, [root], base / 'absent.toml'), '')
        self.assertEqual(self.run_policy(keep, [root], None), '')

    def test_long_descriptions_are_truncated(self):
        base, root, keep, config = self.build(
            {'alpha': self.manifest('x' * 900)}, 'alpha\n')
        text = self.instructions_from(self.run_policy(keep, [root], config))
        self.assertIn('x' * 240, text)
        self.assertNotIn('x' * 241, text)

    def test_skill_without_a_description_is_still_listed(self):
        base, root, keep, config = self.build({'alpha': '# Skill\n'}, 'alpha\n')
        text = self.instructions_from(self.run_policy(keep, [root], config))
        self.assertIn('- alpha: [', text)

    def test_empty_keep_list_emits_nothing(self):
        # The caller then drops the catalog and adds nothing back.
        base, root, keep, config = self.build({'alpha': self.manifest('a')},
                                              '\n# only a comment\n')
        self.assertEqual(self.run_policy(keep, [root], config), '')

    def test_comments_and_blank_lines_are_ignored(self):
        base, root, keep, config = self.build(
            {'alpha': self.manifest('a'), 'beta': self.manifest('b')},
            '\n# keep alpha\nalpha  # trailing\n\n')
        text = self.instructions_from(self.run_policy(keep, [root], config))
        self.assertIn('- alpha', text)
        self.assertNotIn('- beta', text)

    def test_kept_name_that_is_not_installed_is_dropped(self):
        base, root, keep, config = self.build({'alpha': self.manifest('a')}, 'alpha\nghost\n')
        text = self.instructions_from(self.run_policy(keep, [root], config))
        self.assertNotIn('ghost', text)

    def test_only_directories_with_a_manifest_count(self):
        base, root, keep, config = self.build(
            {'real': self.manifest('r'), 'notaskill': None}, 'real\nnotaskill\n')
        text = self.instructions_from(self.run_policy(keep, [root], config))
        self.assertIn('- real', text)
        self.assertNotIn('notaskill', text)

    def test_dotted_internal_directories_are_never_advertised(self):
        base, root, keep, config = self.build(
            {'real': self.manifest('r'), '.sys': self.manifest('s')}, 'real\n.sys\n')
        text = self.instructions_from(self.run_policy(keep, [root], config))
        self.assertNotIn('.sys', text)

    def test_quotes_in_metadata_cannot_break_out_of_the_toml_value(self):
        base, root, keep, config = self.build(
            {'alpha': self.manifest('a "quoted" \\ description')}, 'alpha\n')
        text = self.instructions_from(self.run_policy(keep, [root], config))
        self.assertIn('a "quoted" \\ description', text)

    def test_missing_keep_list_is_not_an_error(self):
        base, root, keep, config = self.build({'alpha': self.manifest('a')}, '')
        self.assertEqual(self.run_policy(base / 'absent', [root], config), '')


if __name__ == '__main__':
    unittest.main()
