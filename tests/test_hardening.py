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
    def run_launcher(self, *args, catalog_failure=False, env=None, servers='', config=None):
        source = (ROOT / "templates/gcodex.launcher.sh").read_text()
        # Keep argument parsing and exec intact; replace only external commands
        # with subprocess-local shell functions and the final exec for capture.
        source = source.replace('CONFIG="${CODEX_HOME:-$HOME/.codex}/${PROFILE}.config.toml"',
                                'CONFIG=%s' % (config or '/dev/null'))
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
python3() { if [ "$1" = "-" ]; then cat > /dev/null; printf '%%s\n' $FAKE_SERVERS;
elif [ "${1##*/}" = "gcodex-skills-policy.py" ]; then command python3 "$@";
else echo 'model_catalog_json="/unused"'; fi; }
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
    def pristine_copy(self, tmp):
        """Patch a throwaway copy of the installed package's original sources."""
        try:
            installed = PATCH['find_package'](None)
        except SystemExit:
            self.skipTest('optional integration fixture: gateway is not installed')
        pkg = Path(tmp) / 'codex_antigravity_auth'
        pkg.mkdir()
        for source in installed.glob('*.py'):
            original = source.with_name(source.name + PATCH['BACKUP_SUFFIX'])
            shutil.copy2(original if original.exists() else source, pkg / source.name)
        return pkg

    def test_patched_cooldown_ladder_is_capped(self):
        # Executes the real patched _apply_cooldown: the ladder must still
        # double per consecutive failure, stop at the cap, and hand a stated
        # Retry-After straight through.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self.pristine_copy(tmp)
            result = subprocess.run(['python3', str(ROOT / 'patch-gateway.py'), str(pkg)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            tree = ast.parse((pkg / 'account_state.py').read_text())
            node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                        and n.name == '_apply_cooldown')
            namespace = dict(__package__='state_fixture',
                             stop_after_auth_failure=SAFETY['stop_after_auth_failure'])
            with patch.dict(__import__('sys').modules, {
                'state_fixture.gateway_safety': types.SimpleNamespace(
                    cooldown_duration=SAFETY['cooldown_duration'])
            }):
                exec(compile(ast.Module(body=[node], type_ignores=[]), 'account_state.py', 'exec'),
                     namespace)
                apply_cooldown = namespace['_apply_cooldown']
                manager = types.SimpleNamespace(
                    data={}, _now=lambda: 0.0,
                    state={'failures': {}, 'cooldowns': {}})
                outcome = types.SimpleNamespace(
                    scope='family', category='rate_limit', retry_after_seconds=None)
                ladder = [apply_cooldown(manager, 'test', 'gemini', outcome) for _ in range(5)]
                self.assertEqual(ladder, [120, 240, SAFETY['MAX_PAUSE_SECONDS'],
                                          SAFETY['MAX_PAUSE_SECONDS'],
                                          SAFETY['MAX_PAUSE_SECONDS']])
                self.assertEqual(manager.state['cooldowns']['test']['gemini'],
                                 SAFETY['MAX_PAUSE_SECONDS'])
                outcome.retry_after_seconds = 3600
                self.assertEqual(apply_cooldown(manager, 'test', 'gemini', outcome), 3600)

    def test_upgrade_over_an_older_patch_does_not_stack_edits(self):
        # An edit whose replacement text changes is not recognised as applied,
        # so re-patching would insert a second copy of it. The result of
        # patching a package that still carries the previous gcodex patch must
        # be identical to patching a pristine one.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self.pristine_copy(tmp)
            server = pkg / 'server.py'
            subprocess.run(['python3', str(ROOT / 'patch-gateway.py'), str(pkg)],
                           check=True, capture_output=True)
            current = server.read_text()
            # Each entry is one edit as some earlier release wrote it. Roll
            # them back one at a time from the patched copy: an install
            # carries a single shape per anchor, so reverting several at once
            # would describe a version that never shipped.
            self.assertTrue(PATCH['LEGACY_SERVER_EDITS'])
            for anchor, superseded in PATCH['LEGACY_SERVER_EDITS']:
                replacement = next(new for old, new in PATCH['SERVER_EDITS'] if old == anchor)
                self.assertIn(replacement, current)
                older = current.replace(replacement, superseded, 1)
                self.assertNotEqual(older, current)
                server.write_text(older)
                result = subprocess.run(['python3', str(ROOT / 'patch-gateway.py'), str(pkg)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(server.read_text(), current)
            self.assertEqual(current.count('rate_limit_pause_seconds(model'), 1)

    def test_rotation_cannot_raise_out_of_a_started_response(self):
        # A rotation acquire runs while the request still holds the single
        # slot, so a waiting acquire always times out; raising there kills an
        # SSE body that already started and the client sees only a
        # disconnected stream instead of the real upstream error.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self.pristine_copy(tmp)
            result = subprocess.run(['python3', str(ROOT / 'patch-gateway.py'), str(pkg)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            tree = ast.parse((pkg / 'server.py').read_text())
            waiting, rotating = [], []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id == 'acquire_active_account_for_request':
                        waiting.append(node.lineno)
                    elif node.func.id == 'rotate_active_account_for_request':
                        rotating.append(node.lineno)
            # Only the initial acquire precedes any response, so it alone may
            # raise; every rotation must use the non-raising helper.
            self.assertEqual(len(waiting), 1, f'waiting acquires at lines {waiting}')
            self.assertEqual(len(rotating), 3, f'rotations at lines {rotating}')
            stream = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                          and n.name == 'sse_generator')
            self.assertNotIn(waiting[0], range(stream.lineno, stream.end_lineno + 1))

    def run_patched_stream(self, pkg, *, failures, visible_output, quota=False):
        """Drive the patched sse_generator with a stubbed Google transport.

        `failures` is how many leading attempts raise HTTP 429 before one
        succeeds. The returned namespace carries what the stubs observed:
        `chunks`, `attempts` (one lease snapshot each), `sleeps`, `failed`,
        `recorded` (the outcome categories that reached account state) and
        `refreshes` (one per token re-point before a retry).
        """
        tree = ast.parse((pkg / 'server.py').read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == 'sse_generator')
        completed = {'type': 'response.completed', 'response': {'id': 'r', 'usage': {}}}
        attempts, sleeps, failed_events, refreshes = [], [], [], []
        # Upstream's own per-request dedupe, reproduced exactly: it is the
        # thing the retry path has to defeat, so stubbing it away would hide
        # the bug rather than test the fix.
        recorded_emails, recorded = set(), []

        async def record_stream_attempt(selected_account, outcome, **kwargs):
            email = selected_account.get('email', '')
            if email in recorded_emails:
                return
            recorded_emails.add(email)
            recorded.append(outcome.category)

        async def refresh_lease_token(manager, account, pool):
            refreshes.append(account.get('accessToken'))
            account['accessToken'] = 'fresh-%d' % len(refreshes)

        class HTTPError(Exception):
            status_code = 429
            response = types.SimpleNamespace(text='quota') if quota else None
            outcome = types.SimpleNamespace(scope='family', category='rate_limit')

        async def capture_quota(*args):
            return 'Google quota exhausted. Quota resets on February 17, 2031 at 15:16:53 PST.' if quota else None

        class Adapter:
            visible_output_started = visible_output
            created_emitted = True
            def reset_attempt(self): pass
            def created(self): return {'type': 'response.created'}
            def fail(self, code, message):
                failed_events.append((code, message))
                return [{'type': 'response.failed'}]

        async def stream_events(codex_req, lease, **kwargs):
            attempts.append(lease)
            if len(attempts) <= failures:
                raise HTTPError()
            yield completed

        async def sleep(seconds):
            sleeps.append(seconds)

        # Three keepalive slices: long enough to prove the wait is broken up
        # and that the client hears something between the pieces.
        async def pause(model, pool, budget):
            return 3 * SAFETY['KEEPALIVE_SECONDS'] if budget > 0 else None

        modules = {
            'stream_fixture.custom_tools': types.SimpleNamespace(
                CustomToolDecoder=lambda codex_req: (lambda event: event)),
            'stream_fixture.gateway_safety': types.SimpleNamespace(
                COOLDOWN_WAIT_SECONDS=900, log_pause=lambda message: None,
                rate_limit_pause_seconds=pause,
                capture_quota=capture_quota,
                refresh_lease_token=refresh_lease_token,
                notify_wait=SAFETY['notify_wait'],
                keepalive_sleep=SAFETY['keepalive_sleep']),
        }
        namespace = dict(
            __package__='stream_fixture', AsyncGenerator=__import__('typing').AsyncGenerator,
            json=json, model='gemini-3.8-flash', family='gemini', codex_req={},
            stream_attempts=[{'email': 'test', 'accessToken': 'stale'}], attempt_num=0,
            recorded_stream_attempts=recorded_emails, account_manager=object(),
            GoogleStreamEventAdapter=lambda **kwargs: Adapter(),
            GoogleHTTPError=HTTPError, GoogleStreamPayloadError=type('Payload', (Exception,), {}),
            AttemptOutcome=lambda **kwargs: types.SimpleNamespace(**kwargs),
            google_transport=types.SimpleNamespace(stream_events=stream_events),
            # Snapshot per attempt: the real lease copies the token out of
            # the account dict, so sharing one object would hide a re-point.
            account_lease=lambda account: dict(account),
            is_validation_required_error=lambda status, text: False,
            retry_after_seconds_from_response=lambda response: None,
            run_in_threadpool=None,
            record_stream_attempt=record_stream_attempt, log_request=self.noop,
            rotate_active_account_for_request=self.none,
            release_account_for_request=self.noop,
        )
        with patch.dict(__import__('sys').modules, modules):
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'server.py', 'exec'), namespace)
            async def drain():
                with patch('asyncio.sleep', sleep):
                    return [chunk async for chunk in namespace['sse_generator']()]
            chunks = asyncio.run(drain())
        return types.SimpleNamespace(chunks=chunks, attempts=attempts, sleeps=sleeps,
                                     failed=failed_events, recorded=recorded,
                                     refreshes=refreshes)

    @staticmethod
    async def noop(*args, **kwargs):
        return None

    def test_quota_stream_reports_reset_without_retrying(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self.pristine_copy(tmp)
            subprocess.run(['python3', str(ROOT / 'patch-gateway.py'), str(pkg)],
                           check=True, capture_output=True)
            run = self.run_patched_stream(pkg, failures=1, visible_output=False, quota=True)
            self.assertEqual(len(run.attempts), 1)
            self.assertEqual(run.sleeps, [])
            self.assertEqual(run.recorded, ['quota'])
            self.assertEqual(run.failed[0][0], 'quota')
            self.assertIn('February 17, 2031', run.failed[0][1])

    @staticmethod
    async def none(*args, **kwargs):
        return None

    def test_rate_limited_stream_waits_and_retries(self):
        # The profile allows the client no retries, so reporting a 429 ends the
        # turn. Nothing has been shown to the user yet at this point, so the
        # gateway may hold the stream open, sleep off the cooldown, and send
        # exactly one more request.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self.pristine_copy(tmp)
            result = subprocess.run(['python3', str(ROOT / 'patch-gateway.py'), str(pkg)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run = self.run_patched_stream(pkg, failures=1, visible_output=False)
            self.assertEqual(len(run.attempts), 2)
            keepalive = SAFETY['KEEPALIVE_SECONDS']
            self.assertEqual(run.sleeps, [keepalive] * 3)
            self.assertEqual(run.failed, [])
            body = ''.join(run.chunks)
            self.assertIn('response.completed', body)
            # Two beats for three slices: the client hears from the gateway
            # inside every idle window, and the retry follows the last slice
            # with no filler of its own.
            self.assertEqual(body.count(SAFETY['KEEPALIVE_EVENT']), 2)
            self.assertLess(body.index(SAFETY['KEEPALIVE_EVENT']),
                            body.index('response.completed'))

    def test_every_retry_reaches_account_state(self):
        # Upstream records one outcome per account per request, which is right
        # for rotation -- each attempt is a different account -- and wrong for
        # a retry, where every attempt after the first was silently dropped.
        # Nothing then wrote a fresh cooldown, so the next pause read a stale
        # one, fell back to a flat interval and the ladder never climbed: the
        # turn kept asking Google on a fixed beat until its budget ran out.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self.pristine_copy(tmp)
            subprocess.run(['python3', str(ROOT / 'patch-gateway.py'), str(pkg)],
                           check=True, capture_output=True)
            run = self.run_patched_stream(pkg, failures=3, visible_output=False)
            self.assertEqual(len(run.attempts), 4)
            # One record per attempt, in order, and the success at the end is
            # what clears the failure ladder the three limits just built.
            self.assertEqual(run.recorded, ['rate_limit'] * 3 + ['success'])

    def test_each_retry_sends_a_re_pointed_token(self):
        # The account dict handed to a request is a snapshot: every later
        # state write rebuilds the store into fresh dicts, so the background
        # refresher cannot reach a turn already in flight. Acquire only
        # guarantees 300s of token life and a paused turn outlives that, so
        # without a re-point the retry ships an expired token and earns a 401
        # -- which is scored against the account, not just the turn.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self.pristine_copy(tmp)
            subprocess.run(['python3', str(ROOT / 'patch-gateway.py'), str(pkg)],
                           check=True, capture_output=True)
            run = self.run_patched_stream(pkg, failures=2, visible_output=False)
            self.assertEqual(run.refreshes, ['stale', 'fresh-1'])
            self.assertEqual([lease['accessToken'] for lease in run.attempts],
                             ['stale', 'fresh-1', 'fresh-2'])

    def test_rate_limit_after_visible_output_is_not_retried(self):
        # Once tokens have shipped, a second attempt would repeat them, so the
        # failure has to reach the client as it did before.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self.pristine_copy(tmp)
            subprocess.run(['python3', str(ROOT / 'patch-gateway.py'), str(pkg)], check=True,
                           capture_output=True)
            run = self.run_patched_stream(pkg, failures=1, visible_output=True)
            self.assertEqual(len(run.attempts), 1)
            self.assertEqual(run.sleeps, [])
            self.assertEqual(run.refreshes, [])
            self.assertEqual([code for code, _ in run.failed], ['rate_limit'])

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

    def profile_with_keep_list(self, keep):
        """A profile directory holding the keep-list and the policy helper."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        config = home / 'gcodex.config.toml'
        config.write_text('model = "gemini-3.8-flash"\ndeveloper_instructions = "Base rule."\n')
        (home / 'gcodex.skills').write_text(keep)
        shutil.copy2(ROOT / 'skills-policy.py', home / 'gcodex-skills-policy.py')
        skills = home / 'skills'
        (skills / 'kept').mkdir(parents=True)
        (skills / 'kept' / 'SKILL.md').write_text('---\ndescription: kept skill\n---\n')
        (skills / 'dropped').mkdir(parents=True)
        (skills / 'dropped' / 'SKILL.md').write_text('---\ndescription: dropped skill\n---\n')
        return home, config

    def test_keep_list_is_advertised_without_disabling_anything(self):
        # Disabling the rest would trim the same bytes and also remove them
        # from the composer's `$` picker, so tagging one by hand would stop
        # working. Nothing may be disabled per skill.
        home, config = self.profile_with_keep_list('kept\n')
        result = self.run_launcher('exec', 'hi', config=config,
                                   env={'CODEX_HOME': str(home)})
        self.assertEqual(result.returncode, 0, result.stderr)
        args = result.stdout.split(b'\0')
        self.assertIn(b'skills.include_instructions=false', args)
        self.assertNotIn(b'skills.config', result.stdout)
        self.assertNotIn(b'enabled=false', result.stdout.replace(b'mcp_servers', b''))
        advertised = [a for a in args if a.startswith(b'developer_instructions=')]
        self.assertEqual(len(advertised), 1, args)
        self.assertIn(b'Base rule.', advertised[0])
        self.assertIn(b'- kept: kept skill', advertised[0])
        self.assertNotIn(b'dropped', advertised[0])

    def test_empty_keep_list_advertises_nothing(self):
        home, config = self.profile_with_keep_list('# nothing kept\n')
        result = self.run_launcher('exec', 'hi', config=config,
                                   env={'CODEX_HOME': str(home)})
        # An empty keep-list adds nothing and still launches codex.
        self.assertEqual(result.returncode, 0, result.stderr)
        args = result.stdout.split(b'\0')
        self.assertIn(b'skills.include_instructions=false', args)
        self.assertEqual([a for a in args if a.startswith(b'developer_instructions=')], [])

    def test_no_servers_declared_adds_no_server_flags(self):
        result = self.run_launcher('exec', 'hi', servers='')
        self.assertNotIn(b'mcp_servers.', result.stdout)
        self.assertIn(b'skills.include_instructions=false', result.stdout.split(b'\0'))
