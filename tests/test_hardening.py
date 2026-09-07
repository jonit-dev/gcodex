import contextlib
import ast
import asyncio
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import tomllib
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PATCH = runpy.run_path(str(ROOT / "patch-gateway.py"))
CLIENT = runpy.run_path(str(ROOT / "install-client.py"))
SAFETY = runpy.run_path(str(ROOT / "gateway_safety.py"))


class Credentials(unittest.TestCase):
    def test_invalid_json_does_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "client.json"
            result = subprocess.run(["python3", str(ROOT / "install-client.py"), str(target)],
                                    input="", text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_private_publication_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "client.json"
            data = {"client_id": "test", "client_secret": "test"}
            CLIENT["install"](target, data)
            CLIENT["check"](target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                CLIENT["install"](target, data)
            target.chmod(0o644)
            with self.assertRaises(ValueError):
                CLIENT["check"](target)

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "client.json"
            target.symlink_to(Path(tmp) / "missing")
            with self.assertRaises(ValueError):
                CLIENT["check"](target)
            with self.assertRaises(FileExistsError):
                CLIENT["install"](target, {"client_id": "x", "client_secret": "y"})


class Launcher(unittest.TestCase):
    def run_launcher(self, *args, catalog_failure=False, env=None, servers=''):
        source = (ROOT / "templates/gcodex.launcher.sh").read_text()
        # Keep argument parsing and exec intact; replace only external commands
        # with subprocess-local shell functions and the final exec for capture.
        source = source.replace('CONFIG="${CODEX_HOME:-$HOME/.codex}/${PROFILE}.config.toml"', 'CONFIG=/dev/null')
        source = source.replace('CREDS="$HOME/.codex/antigravity-credentials.json"', 'CREDS=/dev/null')
        source = source.replace('exec codex ', 'codex ')
        source = source.replace('CATALOG_HELPER="$(dirname "$CONFIG")/gcodex-model-catalog.py"', 'CATALOG_HELPER=/dev/null')
        stub = '''codex-antigravity() {
case "$1" in
status) echo 'reachable: yes';;
models) %s echo '- gemini-3.8-flash: default'; echo '- gemini-3.8-flash-high: high';;
esac
}
codex() { printf '%%s\\0' "$@"; }
python3() { if [ "$1" = "-" ]; then cat > /dev/null; printf '%%s\n' $FAKE_SERVERS; else echo 'model_catalog_json="/unused"'; fi; }
''' % ('return 1;' if catalog_failure else '')
        environment = dict(os.environ, FAKE_SERVERS=servers)
        environment.setdefault('GCODEX_SKILLS', '0')
        environment.setdefault('GCODEX_MCP', '0')
        environment.update(env or {})
        return subprocess.run(['bash', '-c', stub + source, 'launcher', *args],
                              capture_output=True, env=environment)

    def test_delimiter_preserves_prompt(self):
        result = self.run_launcher('exec', '--', '--high')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(b'\0')[-4:-1], [b'exec', b'--', b'--high'])
        self.assertIn(b'model="gemini-3.8-flash"', result.stdout)

    def test_model_preserves_next_argument(self):
        result = self.run_launcher('--model', 'gemini-3.8-flash-high', 'exec', 'hello')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'exec\0hello\0', result.stdout)

    def test_missing_model(self):
        for args in [('--model',), ('--model=',), ('--model', '--high')]:
            result = self.run_launcher(*args)
            self.assertEqual(result.returncode, 2)
            self.assertTrue(result.stderr)

    def test_model_is_literal(self):
        result = self.run_launcher('--model', 'gemini-3x8-flash')
        self.assertIn(b'model="gemini-3.8-flash"', result.stdout)
        self.assertIn(b'not an Antigravity model', result.stderr)

    def test_catalog_failure_stops(self):
        result = self.run_launcher('exec', 'hello', catalog_failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')


class Patching(unittest.TestCase):
    def test_revert_missing_backup_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp)
            for name in ('transform.py', 'google_transport.py', 'account_state.py', 'server.py'):
                (pkg / name).write_text('from .thought_signatures import recall\n')
            (pkg / 'transform.py.pre-gcodex-patch').write_text('original = True\n')
            (pkg / 'thought_signatures.py').write_text('helper = True\n')
            before = {p.name: p.read_bytes() for p in pkg.iterdir()}
            with patch('sys.argv', ['patch-gateway.py', str(pkg), '--revert']):
                with self.assertRaises(SystemExit):
                    PATCH['main']()
            self.assertEqual(before, {p.name: p.read_bytes() for p in pkg.iterdir()})

    def test_anchor_failure_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / 'a.py', Path(tmp) / 'b.py'
            first.write_text('x = 1\n')
            second.write_text('y = 1\n')
            with self.assertRaises(SystemExit):
                PATCH['commit_files']({
                    first: PATCH['render_edits'](first.read_text(), [('x = 1', 'x = 2')], 'a'),
                    second: PATCH['render_edits'](second.read_text(), [('missing', 'new')], 'b'),
                })
            self.assertEqual(first.read_text(), 'x = 1\n')
            self.assertEqual(len(list(Path(tmp).iterdir())), 2)

    def test_syntax_validation_precedes_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'a.py'
            with self.assertRaises(SyntaxError):
                PATCH['commit_files']({first: 'x = 1', Path(tmp) / 'b.py': 'def !!!'})
            self.assertFalse(first.exists())

    def test_write_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / 'a.py', Path(tmp) / 'b.py'
            first.write_text('x = 1\n')
            second.write_text('y = 1\n')
            real = PATCH['atomic_write']
            def fail(path, content):
                if path == second:
                    raise OSError('simulated write failure')
                real(path, content)
            with patch.dict(PATCH['commit_files'].__globals__, atomic_write=fail):
                with self.assertRaises(OSError):
                    PATCH['commit_files']({first: 'x = 2', second: 'y = 2'})
            self.assertEqual(first.read_text(), 'x = 1\n')

    def test_partial_patch_check_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp)
            (pkg / 'thought_signatures.py').write_bytes((ROOT / 'thought_signatures.py').read_bytes())
            (pkg / 'transform.py').write_text('\n'.join(new for _, new in PATCH['TRANSFORM_EDITS']))
            (pkg / 'google_transport.py').write_text('')
            (pkg / 'account_state.py').write_text('')
            with patch('sys.argv', ['patch-gateway.py', str(pkg), '--check']), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    PATCH['main']()
            self.assertEqual(caught.exception.code, 1)


class Guards(unittest.TestCase):
    def test_exactly_one_account(self):
        for accounts in ([], [{}, {}], None):
            self.assertFalse(SAFETY['account_allowed']({'accounts': accounts}))
        self.assertTrue(SAFETY['account_allowed']({'accounts': [{'email': 'test'}]}))

    def test_auth_stop_survives_round_trip(self):
        data = {'accounts': [{'email': 'test'}]}
        SAFETY['stop_after_auth_failure'](data, types.SimpleNamespace(category='auth'))
        self.assertFalse(SAFETY['account_allowed'](json.loads(json.dumps(data))))

    def test_rate_limit_is_not_auth_stop(self):
        data = {'accounts': [{'email': 'test'}]}
        SAFETY['stop_after_auth_failure'](data, types.SimpleNamespace(category='rate_limit'))
        self.assertTrue(SAFETY['account_allowed'](data))


class InstalledGatewayIntegration(unittest.TestCase):
    def test_patch_upgrade_idempotence_and_real_account_state(self):
        try:
            installed = PATCH['find_package'](None)
        except SystemExit:
            self.skipTest('optional integration fixture: gateway is not installed')
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / 'codex_antigravity_auth'
            pkg.mkdir()
            for source in installed.glob('*.py'):
                original = source.with_name(source.name + PATCH['BACKUP_SUFFIX'])
                shutil.copy2(original if original.exists() else source, pkg / source.name)
            command = ['python3', str(ROOT / 'patch-gateway.py'), str(pkg)]
            for suffix in ([], [], ['--check']):
                result = subprocess.run(command + suffix, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            refresh_tree = ast.parse((pkg / 'accounts.py').read_text())
            refresh = next(n for n in ast.walk(refresh_tree) if isinstance(n, ast.FunctionDef)
                           and n.name == 'refresh_expiring_accounts')
            refresh_data = {'accounts': [{'email': 'test', 'refreshToken': 'fake'}],
                            'gcodexAuthBlocked': True}
            refresh_namespace = dict(
                __package__='refresh_fixture', Any=__import__('typing').Any,
                accounts_json_path_read_only=lambda: pkg,
                time=__import__('time'), update_accounts=lambda mutate: mutate(refresh_data),
            )
            fake_manager = types.SimpleNamespace(_lock=__import__('threading').Lock(),
                                                 _sync_state_from_storage=lambda data: None)
            with patch.dict(__import__('sys').modules, {
                'refresh_fixture.gateway_safety': types.SimpleNamespace(account_allowed=SAFETY['account_allowed'])
            }):
                exec(compile(ast.Module(body=[refresh], type_ignores=[]), 'accounts.py', 'exec'), refresh_namespace)
                self.assertEqual(refresh_namespace['refresh_expiring_accounts'](fake_manager),
                                 {'checked': 0, 'refreshed': 0, 'failed': 0})
            # Execute the real patched nested generator, with cancellation at
            # its first logging await. Lease release must still have happened.
            tree = ast.parse((pkg / 'server.py').read_text())
            node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                        and n.name == 'managed_sse_generator')
            released = []
            async def stream():
                yield 'done'
                raise asyncio.CancelledError()
            async def cancelled_log(*args, **kwargs):
                raise asyncio.CancelledError()
            namespace = dict(AsyncGenerator=__import__('typing').AsyncGenerator,
                             sse_generator=stream, stream_attempts=[{'email': 'test'}],
                             recorded_stream_attempts=set(), model='test', family='gemini',
                             log_request=cancelled_log,
                             release_tracked_account=lambda manager, email: manager.release_account(email),
                             account_manager=types.SimpleNamespace(release_account=released.append))
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'server.py', 'exec'), namespace)
            async def cancel_stream():
                iterator = namespace['managed_sse_generator']()
                self.assertEqual(await anext(iterator), 'done')
                with self.assertRaises(asyncio.CancelledError):
                    await anext(iterator)
            asyncio.run(cancel_stream())
            self.assertEqual(released, ['test'])
            code = '''
from codex_antigravity_auth.account_state import AccountState
from codex_antigravity_auth.response_protocol import AttemptOutcome
data = {'accounts': [{'email': 'test'}]}
state = AccountState(data, now=lambda: 100)
lease = state.acquire('gemini')
assert lease is not None
assert state.acquire('gemini') is None
state.release(lease)
assert state.acquire('gemini') is not None
state.release_email('test')
state.record_email('test', 'gemini', AttemptOutcome(scope='account', category='auth'))
assert data['gcodexAuthBlocked'] is True
reloaded = AccountState(data, now=lambda: 1000000)
assert reloaded.acquire('gemini') is None
multiple = AccountState({'accounts': [{'email': 'a'}, {'email': 'b'}]}, now=lambda: 100)
assert multiple.acquire('gemini') is None
limited = AccountState({'accounts': [{'email': 'a'}]}, now=lambda: 100)
limited.record_email('a', 'gemini', AttemptOutcome(scope='family', category='rate_limit', retry_after_seconds=600))
assert limited.acquire('gemini') is None
assert limited.state['cooldowns']['a']['gemini'] >= 700
from codex_antigravity_auth.custom_tools import CustomToolDecoder
from codex_antigravity_auth.response_protocol import ResponseEventBuilder
builder = ResponseEventBuilder(response_id='response-test', model='gemini', created_at=0)
builder.created()
decoder = CustomToolDecoder({'tools': [{'type': 'custom', 'name': 'apply_patch'}]})
events = [decoder(event) for event in builder.add_function_call('apply_patch', '{"input": "patch"}', call_id='call-test')]
assert events[0]['item']['type'] == 'custom_tool_call'
assert events[1]['delta'] == 'patch'
assert events[2]['input'] == 'patch'
assert events[3]['item']['input'] == 'patch'
'''
            result = subprocess.run(['python3', '-B', '-c', code], cwd=tmp,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for suffix in (['--revert'], [], ['--check']):
                result = subprocess.run(command + suffix, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()


class ProfileConfig(unittest.TestCase):
    """The profile trims Codex's tool surface; goals must survive that trim.

    Codex gates the /goal slash command on features.goals, so setting it false
    removes the command from the gcodex profile entirely while plain `codex`
    keeps it.
    """

    def load(self):
        text = (ROOT / "templates/gcodex.config.toml").read_text()
        return tomllib.loads(text.replace("__PORT__", "51122"))

    def test_goal_command_stays_available(self):
        self.assertIs(self.load()["features"]["goals"], True)

    def test_heavy_features_stay_trimmed(self):
        features = self.load()["features"]
        self.assertIs(features["apps"], False)
        self.assertIs(features["multi_agent"], False)

    def test_verification_guidance_is_top_level_and_task_neutral(self):
        # A key placed after any [table] header silently becomes that table's
        # key and is never applied. It must also stay general: guidance naming a
        # benchmark's own subject would improve the score without improving the
        # harness.
        config = self.load()
        guidance = config["developer_instructions"]
        self.assertIsInstance(guidance, str)
        self.assertNotIn("developer_instructions", config["model_providers"]["antigravity"])
        for word in ["queue", "durable_queue", "owner", "TTLCache", "ledger", "sqlite", "job_id"]:
            self.assertNotIn(word.lower(), guidance.lower(), f"guidance names {word!r}")


class LauncherPromptTrim(unittest.TestCase):
    """The launcher must strip inherited skills and MCP servers by default.

    A profile cannot remove what the base config declares, so without this the
    harness resends the whole skill catalog and every MCP tool schema on every
    model call. Measured here: 21,169 input tokens per request before, 2,209
    after -- on a subscription metered per request with a weekly cap.
    """

    def run_launcher(self, *args, **kwargs):
        return Launcher.run_launcher(self, *args, **kwargs)

    def test_trims_skills_and_every_declared_server_by_default(self):
        result = self.run_launcher('exec', 'hi', servers='playwright blender lsp_typescript')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = result.stdout.split(b'\0')
        self.assertIn(b'skills.include_instructions=false', args)
        for server in [b'playwright', b'blender', b'lsp_typescript']:
            self.assertIn(b'mcp_servers.' + server + b'.enabled=false', args)

    def test_opt_outs_restore_each_surface_independently(self):
        keep_skills = self.run_launcher('exec', 'hi', servers='playwright',
                                        env={'GCODEX_SKILLS': '1'})
        args = keep_skills.stdout.split(b'\0')
        self.assertNotIn(b'skills.include_instructions=false', args)
        self.assertIn(b'mcp_servers.playwright.enabled=false', args)

        keep_mcp = self.run_launcher('exec', 'hi', servers='playwright',
                                     env={'GCODEX_MCP': '1'})
        args = keep_mcp.stdout.split(b'\0')
        self.assertIn(b'skills.include_instructions=false', args)
        self.assertNotIn(b'mcp_servers.playwright.enabled=false', args)

    def test_user_arguments_still_reach_codex_after_the_trim(self):
        result = self.run_launcher('exec', '--', '--high', servers='playwright')
        self.assertEqual(result.stdout.split(b'\0')[-4:-1], [b'exec', b'--', b'--high'])

    def test_no_servers_declared_adds_no_server_flags(self):
        result = self.run_launcher('exec', 'hi', servers='')
        self.assertNotIn(b'mcp_servers.', result.stdout)
        self.assertIn(b'skills.include_instructions=false', result.stdout.split(b'\0'))
