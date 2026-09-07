"""Coverage for the Codex CLI compatibility shim and its launcher wiring.

Two upstream `codex` behaviours break invocations that read as obviously
correct, and gcodex rewrites both. These tests pin the rewrite itself, the
cases that must NOT be rewritten (silent scope or instruction changes would be
worse than the original error), and the launcher's tolerance for the helper
being absent or broken.
"""
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPAT = runpy.run_path(str(ROOT / "cli-compat.py"))

PROFILE = '''model = "gemini-3.8-flash"
developer_instructions = """
Base guidance from the profile.
"""
'''


def run_compat(args, config=None, instructions=None, stdin=""):
    """Invoke the helper as the launcher does; returns (rc, override, argv)."""
    command = ["python3", str(ROOT / "cli-compat.py")]
    if config:
        command += ["--config", str(config)]
    command += ["--instructions", instructions or "", "--", *args]
    result = subprocess.run(command, input=stdin, capture_output=True, text=True)
    if result.returncode != 0:
        return result.returncode, None, None
    fields = result.stdout.split("\0")
    self_check = fields.pop()  # trailing NUL leaves one empty tail field
    assert self_check == "", result.stdout
    return 0, fields[0], fields[1:]


class Images(unittest.TestCase):
    """`-i FILE PROMPT` must keep the prompt: --image is variadic upstream."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.first = os.path.join(self.tmp, "one.png")
        self.second = os.path.join(self.tmp, "two.png")
        for path in (self.first, self.second):
            Path(path).write_bytes(b"\x89PNG")

    def test_prompt_survives_a_single_image(self):
        code, override, argv = run_compat(["exec", "-i", self.first, "describe it"])
        self.assertEqual(code, 0)
        self.assertEqual(override, "")
        self.assertEqual(argv, ["exec", "--image=" + self.first, "describe it"])

    def test_multiple_images_all_bind(self):
        code, _, argv = run_compat(["exec", "-i", self.first, self.second, "describe"])
        self.assertEqual(code, 0)
        self.assertEqual(argv, ["exec", "--image=" + self.first,
                                "--image=" + self.second, "describe"])

    def test_long_flag_and_repeated_use(self):
        code, _, argv = run_compat(["--image", self.first, "-i", self.second, "go"])
        self.assertEqual(code, 0)
        self.assertEqual(argv, ["--image=" + self.first, "--image=" + self.second, "go"])

    def test_missing_first_file_still_binds(self):
        """A typo must stay Codex's missing-file error, not a missing prompt."""
        ghost = os.path.join(self.tmp, "absent.png")
        code, _, argv = run_compat(["exec", "-i", ghost, "describe"])
        self.assertEqual(code, 0)
        self.assertEqual(argv, ["exec", "--image=" + ghost, "describe"])

    def test_equals_form_is_left_alone(self):
        code, _, _ = run_compat(["exec", "--image=" + self.first, "describe"])
        self.assertEqual(code, 1, "already correct: nothing to rewrite")

    def test_bare_flag_is_left_to_codex(self):
        code, _, _ = run_compat(["exec", "-i"])
        self.assertEqual(code, 1)

    def test_delimiter_is_not_rewritten(self):
        code, _, argv = run_compat(["exec", "-i", self.first, "--", "-i", "literal"])
        self.assertEqual(code, 0)
        self.assertEqual(argv, ["exec", "--image=" + self.first, "--", "-i", "literal"])


class ReviewScope(unittest.TestCase):
    """`review --uncommitted PROMPT` must keep BOTH the scope and the prompt."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.config = Path(self.tmp) / "gcodex.config.toml"
        self.config.write_text(PROFILE)

    def instructions_from(self, override):
        self.assertTrue(override.startswith("developer_instructions="))
        return json.loads(override.split("=", 1)[1])

    def test_uncommitted_keeps_scope_and_moves_prompt(self):
        code, override, argv = run_compat(
            ["review", "--uncommitted", "only coverage defects"], config=self.config)
        self.assertEqual(code, 0)
        self.assertEqual(argv, ["review", "--uncommitted"])
        text = self.instructions_from(override)
        self.assertIn("only coverage defects", text)
        self.assertIn("Base guidance from the profile.", text)

    def test_base_and_commit_are_handled_too(self):
        for scope in (["--base", "main"], ["--commit", "deadbeef"]):
            with self.subTest(scope=scope[0]):
                code, override, argv = run_compat(
                    ["review", *scope, "find races"], config=self.config)
                self.assertEqual(code, 0)
                self.assertEqual(argv, ["review", *scope])
                self.assertIn("find races", self.instructions_from(override))

    def test_scope_value_is_not_mistaken_for_the_prompt(self):
        code, _, argv = run_compat(["review", "--base", "main"], config=self.config)
        self.assertEqual(code, 1, "no prompt present, so nothing to rewrite")
        self.assertIsNone(argv)

    def test_equals_form_scope(self):
        code, override, argv = run_compat(
            ["review", "--base=main", "find races"], config=self.config)
        self.assertEqual(code, 0)
        self.assertEqual(argv, ["review", "--base=main"])
        self.assertIn("find races", self.instructions_from(override))

    def test_title_value_is_not_the_prompt(self):
        code, _, argv = run_compat(
            ["review", "--uncommitted", "--title", "my title"], config=self.config)
        self.assertEqual(code, 1)
        self.assertIsNone(argv)

    def test_prompt_without_scope_is_untouched(self):
        """Upstream accepts this, so rewriting it would change behaviour."""
        code, _, _ = run_compat(["review", "look at everything"], config=self.config)
        self.assertEqual(code, 1)

    def test_other_subcommands_are_untouched(self):
        code, _, _ = run_compat(["exec", "--uncommitted", "hello"], config=self.config)
        self.assertEqual(code, 1)

    def test_stdin_prompt_is_folded_in(self):
        code, override, argv = run_compat(
            ["review", "--uncommitted", "-"], config=self.config, stdin="from stdin\n")
        self.assertEqual(code, 0)
        self.assertEqual(argv, ["review", "--uncommitted"])
        self.assertIn("from stdin", self.instructions_from(override))

    def test_empty_prompt_is_left_to_codex(self):
        code, _, _ = run_compat(["review", "--uncommitted", "   "], config=self.config)
        self.assertEqual(code, 1)

    def test_skill_keep_list_is_not_clobbered(self):
        """The launcher's own developer_instructions override is the base."""
        precomputed = "developer_instructions=" + json.dumps("Skills: alpha, beta")
        code, override, _ = run_compat(
            ["review", "--uncommitted", "only coverage defects"],
            config=self.config, instructions=precomputed)
        self.assertEqual(code, 0)
        text = self.instructions_from(override)
        self.assertIn("Skills: alpha, beta", text)
        self.assertIn("only coverage defects", text)

    def test_unreadable_profile_suppresses_the_rewrite(self):
        """Better the upstream error than a silently emptied developer message."""
        code, _, _ = run_compat(["review", "--uncommitted", "prompt"],
                                config=Path(self.tmp) / "missing.toml")
        self.assertEqual(code, 1)

    def test_review_after_a_global_value_flag(self):
        code, _, argv = run_compat(
            ["-C", "/tmp", "review", "--uncommitted", "prompt"], config=self.config)
        self.assertEqual(code, 0)
        self.assertEqual(argv, ["-C", "/tmp", "review", "--uncommitted"])

    def test_a_flag_value_named_review_is_not_the_subcommand(self):
        code, _, _ = run_compat(
            ["--cd", "review", "exec", "--uncommitted", "prompt"], config=self.config)
        self.assertEqual(code, 1)


class LauncherWiring(unittest.TestCase):
    """The launcher must use the shim when present and survive it when not."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = Path(self.tmp)
        (self.home / "gcodex.config.toml").write_text(PROFILE)

    def install_helper(self, source=None):
        target = self.home / "gcodex-cli-compat.py"
        if source is None:
            shutil.copy(ROOT / "cli-compat.py", target)
        else:
            target.write_text(source)
        return target

    def run_launcher(self, *args, env=None):
        source = (ROOT / "templates/gcodex.launcher.sh").read_text()
        source = source.replace('CONFIG="${CODEX_HOME:-$HOME/.codex}/${PROFILE}.config.toml"',
                                'CONFIG=%s' % (self.home / "gcodex.config.toml"))
        source = source.replace('CREDS="$HOME/.codex/antigravity-credentials.json"', 'CREDS=/dev/null')
        # `exec` replaces the process in the real launcher, so the early
        # help/version path never returns. Stubbing it away has to keep that.
        source = source.replace('exec codex --profile "$PROFILE" "${rest[@]+"${rest[@]}"}"',
                                'codex --profile "$PROFILE" "${rest[@]+"${rest[@]}"}"; exit 0')
        source = source.replace('exec codex ', 'codex ')
        source = source.replace('CATALOG_HELPER="$(dirname "$CONFIG")/gcodex-model-catalog.py"',
                                'CATALOG_HELPER=/dev/null')
        # Only external commands are stubbed; python3 stays real so the shim
        # under test actually runs. The catalog helper is the one python3 call
        # that has no file behind it here, so it is answered inline.
        stub = '''codex-antigravity() {
case "$1" in
status) echo 'reachable: yes';;
models) echo '- gemini-3.8-flash: default';;
esac
}
codex() { printf '%s\\0' "$@"; }
python3() { case "${1##*/}" in
/dev/null|null) echo 'model_catalog_json="/unused"';;
*) command python3 "$@";; esac; }
'''
        environment = dict(os.environ, HOME=str(self.home))
        environment.setdefault('GCODEX_SKILLS', '0')
        environment.setdefault('GCODEX_MCP', '0')
        environment.update(env or {})
        return subprocess.run(['bash', '-c', stub + source, 'launcher', *args],
                              capture_output=True, env=environment)

    def forwarded(self, result):
        return result.stdout.split(b'\0')[:-1]

    def test_image_rewrite_reaches_codex(self):
        self.install_helper()
        image = self.home / "shot.png"
        image.write_bytes(b"\x89PNG")
        result = self.run_launcher('exec', '-i', str(image), 'describe it')
        self.assertEqual(result.returncode, 0, result.stderr)
        forwarded = self.forwarded(result)
        self.assertIn(('--image=%s' % image).encode(), forwarded)
        self.assertIn(b'describe it', forwarded)
        self.assertNotIn(b'-i', forwarded)

    def test_review_rewrite_reaches_codex(self):
        self.install_helper()
        result = self.run_launcher('review', '--uncommitted', 'only coverage defects')
        self.assertEqual(result.returncode, 0, result.stderr)
        forwarded = self.forwarded(result)
        self.assertIn(b'--uncommitted', forwarded)
        self.assertNotIn(b'only coverage defects', forwarded)
        override = [f for f in forwarded if f.startswith(b'developer_instructions=')]
        self.assertTrue(override, forwarded)
        self.assertIn('only coverage defects', json.loads(override[-1].split(b'=', 1)[1]))

    def test_missing_helper_forwards_unchanged(self):
        result = self.run_launcher('exec', 'hello')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.forwarded(result)[-2:], [b'exec', b'hello'])

    def test_broken_helper_forwards_unchanged(self):
        """A crashing shim must never cost the user their command line."""
        self.install_helper('raise SystemExit("boom")\n')
        result = self.run_launcher('exec', 'hello')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.forwarded(result)[-2:], [b'exec', b'hello'])

    def test_help_skips_the_gateway(self):
        """`gcodex --help` must work before anyone has logged in."""
        result = self.run_launcher('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.forwarded(result), [b'--profile', b'gcodex', b'--help'])
        self.assertNotIn(b'starting gateway', result.stderr)

    def test_completion_skips_the_gateway(self):
        result = self.run_launcher('completion', 'zsh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.forwarded(result), [b'--profile', b'gcodex', b'completion', b'zsh'])

    def test_a_prompt_saying_help_still_runs_normally(self):
        result = self.run_launcher('exec', 'write the --help output')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'model="gemini-3.8-flash"', result.stdout)

    def test_delimited_help_is_not_a_help_request(self):
        result = self.run_launcher('exec', '--', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'model="gemini-3.8-flash"', result.stdout)


class HelperUnits(unittest.TestCase):
    """Direct checks on the pieces that are hard to reach through argv."""

    def test_non_string_instructions_suppress_the_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "c.toml"
            config.write_text("developer_instructions = 3\n")
            self.assertIsNone(COMPAT["base_instructions"](str(config), None))

    def test_absent_instructions_key_is_an_empty_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "c.toml"
            config.write_text('model = "x"\n')
            self.assertEqual(COMPAT["base_instructions"](str(config), None), "")

    def test_malformed_precomputed_override_is_rejected(self):
        self.assertIsNone(COMPAT["base_instructions"](None, "developer_instructions=not json"))
        self.assertIsNone(COMPAT["base_instructions"](None, "skills.include_instructions=false"))


if __name__ == "__main__":
    unittest.main()
