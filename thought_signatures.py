"""Per-call cache of Gemini 3.x ``thoughtSignature`` values.

Gemini 3.x returns an opaque ``thoughtSignature`` on the *part* that carries a
``functionCall``.  That signature MUST be echoed back verbatim on the matching
part of the conversation history in the next request, otherwise the backend
rejects the turn with::

    Function call is missing a thought_signature in functionCall parts.
    This is required for tools to work correctly. (INVALID_ARGUMENT)

The Responses API wire format Codex speaks has nowhere to carry the signature
(unknown fields are dropped by the client), so the gateway keeps them here,
keyed by the ``call_id`` it handed to Codex, and replays them when it rebuilds
``contents`` for the next request.

The private SQLite store survives gateway restarts. A small process-local LRU
avoids repeated disk reads while replaying conversation history. No prompts,
tool arguments, account credentials, or response bodies are stored here.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
from threading import Lock
import time
from typing import Any

MAX_ENTRIES = 2048
MAX_DISK_ENTRIES = 100_000
RETENTION_SECONDS = 30 * 24 * 60 * 60

_LOCK = Lock()
_SIGNATURES: "OrderedDict[str, tuple[str, float]]" = OrderedDict()


@contextmanager
def _database():
    directory = Path(os.environ.get("GCODEX_SIGNATURE_DIR", "~/.codex/gcodex-signatures")).expanduser()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("gcodex signature directory must be private and owned by this user")
    path = directory / "signatures.sqlite3"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("gcodex signature database must be a private regular file")
    finally:
        os.close(fd)
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS signatures (call_id TEXT PRIMARY KEY, signature TEXT NOT NULL, touched REAL NOT NULL)")
        connection.execute("CREATE INDEX IF NOT EXISTS signatures_touched ON signatures(touched)")
        yield connection
        connection.commit()
    finally:
        connection.close()


def part_signature(part: Any) -> str | None:
    """Return the thought signature carried by a Google response *part*."""
    if not isinstance(part, dict):
        return None
    for key in ("thoughtSignature", "thought_signature"):
        value = part.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def remember(call_id: Any, signature: Any) -> None:
    """Associate ``signature`` with ``call_id`` (no-op on empty values)."""
    if not isinstance(call_id, str) or not call_id:
        return
    if not isinstance(signature, str) or not signature:
        return
    with _LOCK:
        with _database() as db:
            now = time.time()
            db.execute("INSERT OR REPLACE INTO signatures VALUES (?, ?, ?)", (call_id, signature, now))
            db.execute("DELETE FROM signatures WHERE touched < ?", (now - RETENTION_SECONDS,))
            db.execute("DELETE FROM signatures WHERE call_id IN (SELECT call_id FROM signatures ORDER BY touched DESC LIMIT -1 OFFSET ?)", (MAX_DISK_ENTRIES,))
        _SIGNATURES[call_id] = (signature, now)
        _SIGNATURES.move_to_end(call_id)
        while len(_SIGNATURES) > MAX_ENTRIES:
            _SIGNATURES.popitem(last=False)


def recall(call_id: Any) -> str | None:
    """Return the signature previously stored for ``call_id``, if any."""
    if not isinstance(call_id, str) or not call_id:
        return None
    with _LOCK:
        now = time.time()
        cached = _SIGNATURES.get(call_id)
        signature = None
        with _database() as db:
            if cached is not None and cached[1] >= now - RETENTION_SECONDS:
                # Memory hits must refresh the durable lifetime too. Otherwise
                # active sessions unexpectedly lose signatures after a restart.
                updated = db.execute(
                    "UPDATE signatures SET touched = ? WHERE call_id = ? AND touched >= ?",
                    (now, call_id, now - RETENTION_SECONDS))
                if updated.rowcount:
                    signature = cached[0]
            else:
                row = db.execute("SELECT signature FROM signatures WHERE call_id = ? AND touched >= ?",
                                 (call_id, now - RETENTION_SECONDS)).fetchone()
                if row:
                    signature = row[0]
                    db.execute("UPDATE signatures SET touched = ? WHERE call_id = ?", (now, call_id))
        if signature is not None:
            _SIGNATURES[call_id] = (signature, now)
            _SIGNATURES.move_to_end(call_id)
            while len(_SIGNATURES) > MAX_ENTRIES:
                _SIGNATURES.popitem(last=False)
        else:
            _SIGNATURES.pop(call_id, None)
    return signature


def attach(part: dict[str, Any], call_id: Any) -> dict[str, Any]:
    """Attach the stored signature for ``call_id`` onto a request ``part``."""
    signature = recall(call_id)
    if signature:
        part["thoughtSignature"] = signature
    return part


def clear() -> None:
    with _LOCK:
        _SIGNATURES.clear()
        with _database() as db:
            db.execute("DELETE FROM signatures")
