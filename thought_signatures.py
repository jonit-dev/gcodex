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

The store is process-local and bounded; losing it (gateway restart) only costs
the in-flight conversation, which will 400 once and then recover on a new one.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import Any

MAX_ENTRIES = 2048

_LOCK = Lock()
_SIGNATURES: "OrderedDict[str, str]" = OrderedDict()


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
        _SIGNATURES[call_id] = signature
        _SIGNATURES.move_to_end(call_id)
        while len(_SIGNATURES) > MAX_ENTRIES:
            _SIGNATURES.popitem(last=False)


def recall(call_id: Any) -> str | None:
    """Return the signature previously stored for ``call_id``, if any."""
    if not isinstance(call_id, str) or not call_id:
        return None
    with _LOCK:
        signature = _SIGNATURES.get(call_id)
        if signature is not None:
            _SIGNATURES.move_to_end(call_id)
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
