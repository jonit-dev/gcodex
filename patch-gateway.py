#!/usr/bin/env python3
"""Patch codex-antigravity-auth to preserve Gemini 3.x thought signatures.

Without this, any multi-turn tool call against gemini-3.x fails with HTTP 400:

    Function call is missing a thought_signature in functionCall parts.

The gateway reads the signature off each ``functionCall`` response part, keeps
it keyed by the call id it hands to Codex, and replays it onto the matching
part when it rebuilds the request history.

Usage:
    python3 patch-gateway.py            # apply (idempotent)
    python3 patch-gateway.py --with-dumps   # also dump request/400 body when GCODEX_DUMP=1
    python3 patch-gateway.py --revert   # restore the pristine files
    python3 patch-gateway.py --check    # report status only
"""

from __future__ import annotations

import argparse
import glob
import shutil
import sys
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


def apply_edits(path: Path, edits: list[tuple[str, str]], label: str) -> bool:
    text = path.read_text()
    changed = False
    for old, new in edits:
        if new in text:
            continue
        if old not in text:
            sys.exit(
                f"{label}: anchor not found in {path.name}; upstream changed.\n"
                f"--- expected ---\n{old}"
            )
        text = text.replace(old, new, 1)
        changed = True
    if changed:
        backup(path)
        path.write_text(text)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", nargs="?", help="path to the codex_antigravity_auth package")
    parser.add_argument("--with-dumps", action="store_true", help="also add GCODEX_DUMP debug dumps")
    parser.add_argument("--revert", action="store_true", help="restore pristine files")
    parser.add_argument("--check", action="store_true", help="report patch status and exit")
    args = parser.parse_args()

    pkg = find_package(args.package)
    transform = pkg / "transform.py"
    transport = pkg / "google_transport.py"
    module = pkg / "thought_signatures.py"

    if args.check:
        patched = module.exists() and "thought_signatures" in transform.read_text()
        dumps = "GCODEX_DUMP" in transport.read_text()
        print(f"package: {pkg}")
        print(f"thought-signature fix: {'applied' if patched else 'NOT applied'}")
        print(f"debug dumps:           {'applied' if dumps else 'not applied'}")
        return

    if args.revert:
        restored = []
        for path in (transform, transport):
            bak = path.with_name(path.name + BACKUP_SUFFIX)
            if bak.exists():
                shutil.copy2(bak, path)
                bak.unlink()
                restored.append(path.name)
        if module.exists():
            module.unlink()
            restored.append(module.name + " (removed)")
        print("reverted:", ", ".join(restored) if restored else "nothing to revert")
        return

    shutil.copy2(HERE / "thought_signatures.py", module)
    a = apply_edits(transform, TRANSFORM_EDITS, "transform.py")
    b = apply_edits(transport, TRANSPORT_EDITS, "google_transport.py")
    c = apply_edits(transport, DUMP_EDITS, "google_transport.py dumps") if args.with_dumps else False

    print(f"package: {pkg}")
    print("thought-signature fix:", "applied" if (a or b) else "already applied")
    if args.with_dumps:
        print("debug dumps:", "applied" if c else "already applied")
    print("restart the gateway for changes to take effect")


if __name__ == "__main__":
    main()
