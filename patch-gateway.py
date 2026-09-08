#!/usr/bin/env python3
"""Patch codex-antigravity-auth to preserve Gemini 3.x thought signatures.

Without this, any multi-turn tool call against gemini-3.x fails with HTTP 400:

    Function call is missing a thought_signature in functionCall parts.

The gateway reads the signature off each ``functionCall`` response part, keeps
it keyed by the call id it hands to Codex, and replays it onto the matching
part when it rebuilds the request history.

Usage:
    python3 patch-gateway.py            # apply (idempotent)
    python3 patch-gateway.py --revert   # restore the pristine files
    python3 patch-gateway.py --check    # report status only
"""

from __future__ import annotations

import argparse
import ast
import glob
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".pre-gcodex-patch"

DEFAULT_GLOBS = [
    "~/.local/share/uv/tools/codex-antigravity-auth/lib/python*/site-packages/codex_antigravity_auth",
    "~/.local/pipx/venvs/codex-antigravity-auth/lib/python*/site-packages/codex_antigravity_auth",
]

TRANSFORM_EDITS = [
    (
        "from .schema import clean_json_schema\n",
        "from .schema import clean_json_schema\n"
        "from .thought_signatures import (\n"
        "    attach as attach_thought_signature,\n"
        "    part_signature,\n"
        "    remember as remember_thought_signature,\n"
        ")\n",
    ),
    (
        """            return [{
                "functionCall": {
                    "name": name,
                    "args": _function_call_args(part.get("input", {}))
                }
            }]
""",
        """            return [attach_thought_signature({
                "functionCall": {
                    "name": name,
                    "args": _function_call_args(part.get("input", {}))
                }
            }, call_id)]
""",
    ),
    (
        """                append_content("assistant", [{
                    "functionCall": {
                        "name": name,
                        "args": args
                    }
                }])
""",
        """                append_content("assistant", [attach_thought_signature({
                    "functionCall": {
                        "name": name,
                        "args": args
                    }
                }, call_id)])
""",
    ),
    (
        """            call_id = _stream_text(fc.get("id")) or f"call_{uuid.uuid4().hex[:8]}"
""",
        """            call_id = _stream_text(fc.get("id")) or f"call_{uuid.uuid4().hex[:8]}"
            remember_thought_signature(call_id, part_signature(part))
""",
    ),
]

TRANSPORT_EDITS = [
    (
        """from .transform import (
    function_call_arguments_json,
""",
        """from .thought_signatures import (
    part_signature,
    remember as remember_thought_signature,
)
from .transform import (
    function_call_arguments_json,
""",
    ),
    (
        """                    call_id = function_call.get("id")
                    if not isinstance(call_id, str) or not call_id:
                        call_id = f"call_{uuid.uuid4().hex[:8]}"
""",
        """                    call_id = function_call.get("id")
                    if not isinstance(call_id, str) or not call_id:
                        call_id = f"call_{uuid.uuid4().hex[:8]}"
                    remember_thought_signature(call_id, part_signature(part))
""",
    ),
    (
        """                call_id = function_call.get("id")
                events.extend(
                    self.builder.add_function_call(
                        name,
                        function_call_arguments_json(function_call.get("args", {})),
                        call_id=call_id if isinstance(call_id, str) else None,
                    )
                )
""",
        """                call_id = function_call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    call_id = f"call_{uuid.uuid4().hex[:12]}"
                remember_thought_signature(call_id, part_signature(part))
                events.extend(
                    self.builder.add_function_call(
                        name,
                        function_call_arguments_json(function_call.get("args", {})),
                        call_id=call_id,
                    )
                )
""",
    ),
]

LEGACY_TRANSFORM_EDITS = list(TRANSFORM_EDITS)
LEGACY_TRANSPORT_EDITS = list(TRANSPORT_EDITS)
TRANSFORM_EDITS = [(old, new.replace("uuid.uuid4().hex[:8]", "uuid.uuid4().hex"))
                   for old, new in TRANSFORM_EDITS]
TRANSPORT_EDITS = [(old, new.replace("uuid.uuid4().hex[:8]", "uuid.uuid4().hex")
                   .replace("uuid.uuid4().hex[:12]", "uuid.uuid4().hex"))
                   for old, new in TRANSPORT_EDITS]


DUMP_EDITS = [
    (
        """        url = f"{self.endpoint}/v1internal:streamGenerateContent?alt=sse"
        async with self.client_factory(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                url,
                json=self.build_request(request, lease),
""",
        """        url = f"{self.endpoint}/v1internal:streamGenerateContent?alt=sse"
        _body = self.build_request(request, lease)
        try:
            import os as _os, json as _json
            if _os.environ.get("GCODEX_DUMP"):
                open("/tmp/gcodex-req.json", "w").write(_json.dumps(_body, indent=2, default=str))
        except Exception:
            pass
        async with self.client_factory(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                url,
                json=_body,
""",
    ),
    (
        """        async with self.stream(request, lease) as response:
            if response.status_code != 200:
                raise GoogleHTTPError(
""",
        """        async with self.stream(request, lease) as response:
            if response.status_code != 200:
                try:
                    import os as _os
                    if _os.environ.get("GCODEX_DUMP"):
                        open("/tmp/gcodex-400.txt", "wb").write(await response.aread())
                except Exception:
                    pass
                raise GoogleHTTPError(
""",
    ),
]

# These edits replace the earlier optional body dumps. Existing installations
# are migrated back to the original transport code before applying other edits.
SAFETY_EDITS = [
    ("from typing import Any", "from .gateway_safety import account_allowed, stop_after_auth_failure\nfrom typing import Any"),
    (
        '            accounts = self.data.get("accounts")\n',
        '            if not account_allowed(self.data):\n'
        '                return None\n'
        '            accounts = self.data.get("accounts")\n',
    ),
    (
        '            email = str(account["email"])\n            if acquire:\n',
        '            email = str(account["email"])\n'
        '            if self._in_flight.get(email, 0):\n'
        '                return None\n'
        '            if acquire:\n',
    ),
    (
        '        scope = "account" if outcome.scope == "account" else family\n',
        '        stop_after_auth_failure(self.data, outcome)\n'
        '        scope = "account" if outcome.scope == "account" else family\n',
    ),
    (
        '        duration = max(backoff, min(float(retry_after), 86_400))\n',
        '        # Cap the guessed half of the ladder: an uncapped 1920s cooldown\n'
        '        # outlives the wait budget, so the turn dies where a shorter\n'
        '        # interval would have retried. A stated Retry-After still wins.\n'
        '        from .gateway_safety import cooldown_duration\n'
        '        duration = cooldown_duration(backoff, retry_after)\n',
    ),
]

SERVER_EDITS = [
    (
        '    stream = response_stream_flag(codex_req)\n',
        '    stream = response_stream_flag(codex_req)\n'
        '    from .gateway_safety import enable_wait_notices\n'
        '    enable_wait_notices(stream)\n',
    ),
    (
        'account_manager = AccountManager()\n',
        'account_manager = AccountManager()\n'
        'from .gateway_safety import RequestLeaseMiddleware, release_tracked_account\n'
        'app.add_middleware(RequestLeaseMiddleware)\n',
    ),
    (
        '    await run_in_threadpool(account_manager.release_account, email)\n',
        '    release_tracked_account(account_manager, email)\n',
    ),
    (
        '        "experimental_supported_tools": [],\n',
        '        "experimental_supported_tools": [],\n'
        '        "apply_patch_tool_type": "freeform",\n',
    ),
    (
        '        adapter = GoogleStreamEventAdapter(response_id=response_id, display_model=model)\n',
        '        adapter = GoogleStreamEventAdapter(response_id=response_id, display_model=model)\n'
        '        from .custom_tools import CustomToolDecoder\n'
        '        custom_decoder = CustomToolDecoder(codex_req)\n',
    ),
    (
        '            return f"data: {json.dumps(event)}\\n\\n"\n',
        '            return f"data: {json.dumps(custom_decoder(event))}\\n\\n"\n',
    ),
    (
        '                return codex_resp\n',
        '                from .custom_tools import CustomToolDecoder\n'
        '                return CustomToolDecoder(codex_req).response(codex_resp)\n',
    ),
    (
        'async def acquire_active_account_for_request(model: str) -> dict | None:\n'
        '    return await run_in_threadpool(account_manager.acquire_account, model)\n',
        'async def acquire_active_account_for_request(model: str) -> dict | None:\n'
        '    from .gateway_safety import acquire_serially\n'
        '    return await acquire_serially(account_manager, model, run_in_threadpool)\n',
    ),
    (
        'async def acquire_active_account_for_request(model: str) -> dict | None:\n'
        '    from .gateway_safety import acquire_serially\n'
        '    return await acquire_serially(account_manager, model, run_in_threadpool)\n',
        'async def acquire_active_account_for_request(model: str) -> dict | None:\n'
        '    from .gateway_safety import acquire_serially\n'
        '    return await acquire_serially(account_manager, model, run_in_threadpool)\n'
        '\n'
        '\n'
        'async def rotate_active_account_for_request(model: str) -> dict | None:\n'
        '    from .gateway_safety import acquire_without_waiting\n'
        '    return await acquire_without_waiting(account_manager, model, run_in_threadpool)\n',
    ),
    (
        '                new_account = await acquire_active_account_for_request(model)\n'
        '                rotation_attempted = True\n'
        '                if new_account:\n'
        '                    await record_attempt_outcome(\n',
        '                new_account = await rotate_active_account_for_request(model)\n'
        '                rotation_attempted = True\n'
        '                if new_account:\n'
        '                    await record_attempt_outcome(\n',
    ),
    (
        '                new_account = await acquire_active_account_for_request(model)\n'
        '                rotation_attempted = True\n'
        '                if new_account:\n'
        '                    response_attempts.append(new_account)\n',
        '                new_account = await rotate_active_account_for_request(model)\n'
        '                rotation_attempted = True\n'
        '                if new_account:\n'
        '                    response_attempts.append(new_account)\n',
    ),
    (
        '                rotated = await acquire_active_account_for_request(model)\n',
        '                rotated = await rotate_active_account_for_request(model)\n',
    ),
    (
        '        finally:\n            cancelled = any(\n',
        '        finally:\n'
        '            # Release in-memory leases before cancellable logging awaits.\n'
        '            for used_account in stream_attempts:\n'
        '                release_tracked_account(account_manager, used_account.get("email"))\n'
        '            cancelled = any(\n',
    ),
    (
        '            released_emails = set()\n',
        '            # Leases were already released synchronously above.\n',
    ),
    (
        '                email = used_account.get("email")\n'
        '                if email and email not in released_emails:\n'
        '                    released_emails.add(email)\n'
        '                    await release_account_for_request(email)\n',
        '                # No second release: a new request may own this slot.\n',
    ),
    (
        '        custom_decoder = CustomToolDecoder(codex_req)\n'
        '        attempt_num = 0\n',
        '        custom_decoder = CustomToolDecoder(codex_req)\n'
        '        import asyncio\n'
        '        from .gateway_safety import (COOLDOWN_WAIT_SECONDS, keepalive_sleep, log_pause,\n'
        '                                     rate_limit_pause_seconds, refresh_lease_token)\n'
        '        rate_limit_budget = COOLDOWN_WAIT_SECONDS\n'
        '        attempt_num = 0\n',
    ),
    (
        '            stream_account = stream_attempts[attempt_num]\n'
        '            terminal_event: dict | None = None\n',
        '            stream_account = stream_attempts[attempt_num]\n'
        '            terminal_event: dict | None = None\n'
        "            # Only this attempt's own failure may trigger a rate-limit\n"
        '            # wait; a stream that ends without a terminal event reaches\n'
        '            # the same code with no outcome of its own.\n'
        '            outcome = None\n',
    ),
    (
        '            if attempt_num == 0 and not adapter.visible_output_started:\n'
        '                rotated = await rotate_active_account_for_request(model)\n',
        '            # A rate limit is a wait, not a verdict: pause the turn here\n'
        '            # rather than failing it, since the profile allows the client\n'
        '            # no retries of its own. Nothing is sent to Google while we\n'
        '            # sleep, and the retry reuses the slot this request already\n'
        '            # holds, so the account is never asked twice at once. Only\n'
        '            # before any visible output -- once tokens have shipped, a\n'
        '            # second attempt would duplicate them.\n'
        '            if (outcome is not None and outcome.category == "rate_limit"\n'
        '                    and not adapter.visible_output_started):\n'
        '                pause = await rate_limit_pause_seconds(model, run_in_threadpool, rate_limit_budget)\n'
        '                if pause is not None:\n'
        '                    rate_limit_budget -= pause\n'
        '                    log_pause("rate limited; waiting %ds before retrying this turn" % (int(pause) + 1))\n'
        '                    # Keepalives, not silence: an idle stream is\n'
        '                    # dropped by the client, and a dropped stream is\n'
        '                    # the turn dying of the limit we are waiting out.\n'
        '                    async for beat in keepalive_sleep(pause):\n'
        '                        yield beat\n'
        '                    # Upstream records one outcome per account per\n'
        '                    # request. That is right for rotation and wrong\n'
        '                    # for a retry: every attempt after the first is\n'
        '                    # dropped, so no fresh cooldown is written, the\n'
        '                    # next pause reads a stale one and falls back to\n'
        '                    # a flat interval, and the ladder never climbs.\n'
        '                    recorded_stream_attempts.discard(stream_account.get("email", ""))\n'
        '                    # The account dict is a snapshot taken at acquire\n'
        '                    # time and the background refresher writes to a\n'
        '                    # different copy, so a paused turn would go on\n'
        '                    # sending a token that expired while it waited.\n'
        '                    await refresh_lease_token(account_manager, stream_account, run_in_threadpool)\n'
        '                    adapter.reset_attempt()\n'
        '                    continue\n'
        '            if attempt_num == 0 and not adapter.visible_output_started:\n'
        '                rotated = await rotate_active_account_for_request(model)\n',
    ),
]

# Keep the previous retry block available for upgrades from installed patches.
PRE_NOTICE_RETRY_EDIT = SERVER_EDITS[-1]
SERVER_EDITS[-1] = (PRE_NOTICE_RETRY_EDIT[0], PRE_NOTICE_RETRY_EDIT[1].replace(
    '                    async for beat in keepalive_sleep(pause):\n',
    '                    from .gateway_safety import notify_wait\n'
    '                    await notify_wait(pause)\n'
    '                    async for beat in keepalive_sleep(pause):\n',
))

# Undoing an older gcodex patch before re-applying the current one: an edit
# whose replacement text changed would otherwise be inserted a second time,
# since only its own output marks it as already applied.
LEGACY_SERVER_EDITS = [
    PRE_NOTICE_RETRY_EDIT,
    (
        '            if attempt_num == 0 and not adapter.visible_output_started:\n'
        '                rotated = await rotate_active_account_for_request(model)\n',
        '            # A rate limit is a wait, not a verdict: pause the turn here\n'
        '            # rather than failing it, since the profile allows the client\n'
        '            # no retries of its own. Nothing is sent to Google while we\n'
        '            # sleep, and the retry reuses the slot this request already\n'
        '            # holds, so the account is never asked twice at once. Only\n'
        '            # before any visible output -- once tokens have shipped, a\n'
        '            # second attempt would duplicate them.\n'
        '            if (outcome is not None and outcome.category == "rate_limit"\n'
        '                    and not adapter.visible_output_started):\n'
        '                pause = await rate_limit_pause_seconds(model, run_in_threadpool, rate_limit_budget)\n'
        '                if pause is not None:\n'
        '                    rate_limit_budget -= pause\n'
        '                    log_pause("rate limited; waiting %ds before retrying this turn" % (int(pause) + 1))\n'
        '                    # Keepalives, not silence: an idle stream is\n'
        '                    # dropped by the client, and a dropped stream is\n'
        '                    # the turn dying of the limit we are waiting out.\n'
        '                    async for beat in keepalive_sleep(pause):\n'
        '                        yield beat\n'
        '                    adapter.reset_attempt()\n'
        '                    continue\n'
        '            if attempt_num == 0 and not adapter.visible_output_started:\n'
        '                rotated = await rotate_active_account_for_request(model)\n',
    ),
    (
        '        custom_decoder = CustomToolDecoder(codex_req)\n'
        '        attempt_num = 0\n',
        '        custom_decoder = CustomToolDecoder(codex_req)\n'
        '        import asyncio\n'
        '        from .gateway_safety import (COOLDOWN_WAIT_SECONDS, keepalive_sleep, log_pause,\n'
        '                                     rate_limit_pause_seconds)\n'
        '        rate_limit_budget = COOLDOWN_WAIT_SECONDS\n'
        '        attempt_num = 0\n',
    ),
    (
        '            if attempt_num == 0 and not adapter.visible_output_started:\n'
        '                rotated = await rotate_active_account_for_request(model)\n',
        '            # A rate limit is a wait, not a verdict: pause the turn here\n'
        '            # rather than failing it, since the profile allows the client\n'
        '            # no retries of its own. Nothing is sent to Google while we\n'
        '            # sleep, and the retry reuses the slot this request already\n'
        '            # holds, so the account is never asked twice at once. Only\n'
        '            # before any visible output -- once tokens have shipped, a\n'
        '            # second attempt would duplicate them.\n'
        '            if (outcome is not None and outcome.category == "rate_limit"\n'
        '                    and not adapter.visible_output_started):\n'
        '                pause = await rate_limit_pause_seconds(model, run_in_threadpool, rate_limit_budget)\n'
        '                if pause is not None:\n'
        '                    rate_limit_budget -= pause\n'
        '                    log_pause("rate limited; waiting %ds before retrying this turn" % (int(pause) + 1))\n'
        '                    await asyncio.sleep(pause)\n'
        '                    adapter.reset_attempt()\n'
        '                    continue\n'
        '            if attempt_num == 0 and not adapter.visible_output_started:\n'
        '                rotated = await rotate_active_account_for_request(model)\n',
    ),
    (
        '        custom_decoder = CustomToolDecoder(codex_req)\n'
        '        attempt_num = 0\n',
        '        custom_decoder = CustomToolDecoder(codex_req)\n'
        '        import asyncio\n'
        '        from .gateway_safety import COOLDOWN_WAIT_SECONDS, log_pause, rate_limit_pause_seconds\n'
        '        rate_limit_budget = COOLDOWN_WAIT_SECONDS\n'
        '        attempt_num = 0\n',
    ),
]

CUSTOM_TRANSFORM_EDITS = [
    (
        '    """Translate standard Codex Responses API request body to Antigravity format."""\n',
        '    """Translate standard Codex Responses API request body to Antigravity format."""\n'
        '    from .custom_tools import encode_request\n'
        '    codex_req = encode_request(codex_req)\n',
    ),
]

ACCOUNT_EDITS = [
    (
        '                self._sync_state_from_storage(data)\n'
        '                accounts = data.get("accounts", [])\n',
        '                self._sync_state_from_storage(data)\n'
        '                from .gateway_safety import account_allowed\n'
        '                if not account_allowed(data):\n'
        '                    return False\n'
        '                accounts = data.get("accounts", [])\n',
    ),
]


def find_package(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_dir():
            sys.exit(f"not a directory: {path}")
        return path
    for pattern in DEFAULT_GLOBS:
        matches = sorted(glob.glob(str(Path(pattern).expanduser())))
        if matches:
            return Path(matches[-1])
    sys.exit("codex_antigravity_auth package not found; pass its path as an argument")


def backup(path: Path) -> None:
    bak = path.with_name(path.name + BACKUP_SUFFIX)
    if not bak.exists():
        shutil.copy2(path, bak)


def render_edits(text: str, edits: list[tuple[str, str]], label: str) -> str:
    for old, new in edits:
        if new in text:
            continue
        if old not in text:
            sys.exit(
                f"{label}: anchor not found; upstream changed.\n"
                f"--- expected ---\n{old}"
            )
        if text.count(old) != 1:
            sys.exit(f"{label}: ambiguous anchor; refusing to patch")
        text = text.replace(old, new, 1)
    return text


def atomic_write(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".gcodex-patch-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def commit_files(planned: dict[Path, str]) -> bool:
    # All anchors and Python syntax are validated before any package mutation.
    for path, content in planned.items():
        ast.parse(content, filename=str(path))
    originals = {p: p.read_bytes() if p.exists() else None for p in planned}
    changed = [p for p, content in planned.items() if originals[p] != content.encode()]
    for path in changed:
        if originals[path] is not None:
            backup(path)
    written = []
    try:
        for path in changed:
            atomic_write(path, planned[path].encode())
            written.append(path)
    except BaseException:
        for path in reversed(written):
            if originals[path] is None:
                path.unlink()
            else:
                atomic_write(path, originals[path])
        raise
    return bool(changed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", nargs="?", help="path to the codex_antigravity_auth package")
    parser.add_argument("--with-dumps", action="store_true", help="deprecated; request body dumps are no longer supported")
    parser.add_argument("--revert", action="store_true", help="restore pristine files")
    parser.add_argument("--check", action="store_true", help="report patch status and exit")
    args = parser.parse_args()

    pkg = find_package(args.package)
    transform = pkg / "transform.py"
    transport = pkg / "google_transport.py"
    module = pkg / "thought_signatures.py"
    state = pkg / "account_state.py"
    server = pkg / "server.py"
    safety = pkg / "gateway_safety.py"
    custom = pkg / "custom_tools.py"
    accounts = pkg / "accounts.py"

    if args.with_dumps:
        parser.error("request body dumps were removed; run without --with-dumps")

    if args.check:
        patched = (module.exists() and module.read_bytes() == (HERE / module.name).read_bytes()
                   and all(new in transform.read_text() for _, new in TRANSFORM_EDITS)
                   and all(new in transport.read_text() for _, new in TRANSPORT_EDITS))
        guarded = (safety.exists() and safety.read_bytes() == (HERE / safety.name).read_bytes()
                   and all(new in state.read_text() for _, new in SAFETY_EDITS)
                   and all(new in server.read_text() for _, new in SERVER_EDITS)
                   and all(new in accounts.read_text() for _, new in ACCOUNT_EDITS))
        custom_ready = (custom.exists() and custom.read_bytes() == (HERE / custom.name).read_bytes()
                        and all(new in transform.read_text() for _, new in CUSTOM_TRANSFORM_EDITS))
        dumps = "GCODEX_DUMP" in transport.read_text()
        print(f"package: {pkg}")
        print(f"thought-signature fix: {'applied' if patched else 'NOT applied'}")
        print(f"debug dumps:           {'applied' if dumps else 'not applied'}")
        print(f"account safeguards:    {'applied' if guarded else 'NOT applied'}")
        print(f"custom tools:          {'applied' if custom_ready else 'NOT applied'}")
        sys.exit(0 if patched and guarded and custom_ready and not dumps else 1)

    if args.revert:
        planned = {}
        backups = []
        for path in (transform, transport, state, server, accounts):
            bak = path.with_name(path.name + BACKUP_SUFFIX)
            if bak.exists():
                planned[path] = bak.read_text()
                backups.append(bak)
            elif any(marker in path.read_text() for marker in (
                "from .thought_signatures", "from .gateway_safety", "from .custom_tools",
                "Release in-memory leases before cancellable logging awaits",
            )):
                sys.exit(f"cannot revert {path.name}: backup missing; no files changed")
        restored_sources = [planned.get(path, path.read_text())
                            for path in (transform, transport, state, server, accounts)]
        if any(any(marker in source for marker in ("from .thought_signatures", "from .gateway_safety", "from .custom_tools"))
               for source in restored_sources):
            sys.exit("cannot revert: backups still depend on patch helpers; no files changed")
        commit_files(planned)
        restored = [path.name for path in planned]
        for bak in backups:
            bak.unlink()
        for helper in (module, safety, custom):
            if helper.exists():
                helper.unlink()
                restored.append(helper.name + " (removed)")
        print("reverted:", ", ".join(restored) if restored else "nothing to revert")
        return

    transport_text = transport.read_text()
    transform_text = transform.read_text()
    server_text = server.read_text().replace(
        '                account_manager.release_account(used_account.get("email"))\n',
        '                release_tracked_account(account_manager, used_account.get("email"))\n',
    )
    server_text = server_text.replace(SERVER_EDITS[-1][1], SERVER_EDITS[-1][0])
    for old, new in LEGACY_SERVER_EDITS:
        server_text = server_text.replace(new, old)
    for old, new in LEGACY_TRANSFORM_EDITS:
        transform_text = transform_text.replace(new, old)
    for old, new in LEGACY_TRANSPORT_EDITS:
        transport_text = transport_text.replace(new, old)
    for original, dumped in DUMP_EDITS:
        transport_text = transport_text.replace(dumped, original)
    if "GCODEX_DUMP" in transport_text:
        sys.exit("unknown debug dump code; refusing to patch")
    changed = commit_files({
        transform: render_edits(transform_text, TRANSFORM_EDITS + CUSTOM_TRANSFORM_EDITS, transform.name),
        transport: render_edits(transport_text, TRANSPORT_EDITS, transport.name),
        state: render_edits(state.read_text(), SAFETY_EDITS, state.name),
        server: render_edits(server_text, SERVER_EDITS, server.name),
        accounts: render_edits(accounts.read_text(), ACCOUNT_EDITS, accounts.name),
        module: (HERE / module.name).read_text(),
        safety: (HERE / safety.name).read_text(),
        custom: (HERE / custom.name).read_text(),
    })

    print(f"package: {pkg}")
    print("gateway patches:", "applied" if changed else "already applied")
    print("debug dumps: disabled; account safeguards: applied")
    print("restart the gateway for changes to take effect")


if __name__ == "__main__":
    main()
