"""Local account guards for the gcodex gateway patch."""
from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass
import os

BLOCK_KEY = "gcodexAuthBlocked"
QUEUE_TIMEOUT_SECONDS = 30
# A waiter re-reads persisted account state at most this often while queueing.
STATE_POLL_SECONDS = 1.0
_REQUEST_LEASES = ContextVar('gcodex_request_leases', default=None)


def _seconds_from_env(name, default):
    """Read a non-negative seconds value from the environment, else `default`."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


# A rate limit is a wait, not a verdict. Upstream records a cooldown -- 120s
# doubling per consecutive failure, capped at 1920s -- and the unpatched
# gateway turns that into an immediate 429, which ends the Codex turn because
# the profile deliberately allows no client retries. Waiting the cooldown out
# inside the gateway keeps the turn alive without sending Google anything
# extra: the pause is local and exactly one request is sent when the cooldown
# expires. A mid-stream retry keeps the slot it already holds; a request still
# waiting to start holds none.
#
# Total wait per turn, across as many limits as one turn hits. The stream is
# held open by keepalives while this runs, so it is not bounded by the
# client's idle timeout the way the pre-stream wait below is.
COOLDOWN_WAIT_SECONDS = _seconds_from_env("GCODEX_COOLDOWN_WAIT", 3600.0)
# A request that has not started streaming has sent no headers, so nothing can
# be emitted to keep the client interested: this wait is plain silence, and
# only 900s of it is verified against Codex 0.153.4.
ACQUIRE_WAIT_SECONDS = _seconds_from_env("GCODEX_ACQUIRE_WAIT", 900.0)
# Used when a rate limit arrives without a recorded cooldown to read, so the
# retry still backs off instead of answering Google immediately.
RATE_LIMIT_FALLBACK_PAUSE_SECONDS = _seconds_from_env("GCODEX_RATE_LIMIT_PAUSE", 60.0)
# Backoff has to be exponential to be polite and capped to stay usable. The
# upstream ladder is 120s doubling per consecutive failure to 1920s, and a
# 1920s wait ends the turn anyway: it outlives both the request's wait budget
# and the client's tolerance for silence. Cap the ladder instead, so the turn
# keeps retrying on an interval that fits. Zero or less disables the cap.
MAX_PAUSE_SECONDS = _seconds_from_env("GCODEX_MAX_PAUSE", 300.0)
# Codex measures stream_idle_timeout between SSE *events*. A comment line
# resets nothing -- verified against 0.153.4: keepalive comments every 2s
# still tripped an 8s idle timeout -- while an event type it does not know
# resets the timer and is dropped without reaching the transcript. That is
# what lets a pause outlive the client's idle timeout instead of being
# capped by it. Emitted only while waiting, never as part of an answer.
KEEPALIVE_EVENT = 'data: {"type": "response.gcodex_keepalive"}\n\n'
KEEPALIVE_SECONDS = _seconds_from_env("GCODEX_KEEPALIVE", 60.0)
# How much life a token must have left to be worth sending. Acquire uses the
# same 300s, so this is upstream's own threshold applied a second time, at the
# point where it actually matters: just before a retry, rather than only at the
# start of a turn that may pause for far longer than the token lives.
TOKEN_MARGIN_SECONDS = _seconds_from_env("GCODEX_TOKEN_MARGIN", 300.0)
# Copied onto the in-flight snapshot; everything else about the account is
# either unchanged by a refresh or none of a lease's business.
LEASE_FIELDS = ("accessToken", "expiresAt", "projectId", "managedProjectId")


async def keepalive_sleep(seconds):
    """Sleep, yielding a keepalive event often enough to hold the stream open.

    Yields raw SSE text, not adapter events: the client must ignore these, and
    the response protocol has no event for "still waiting". Nothing is yielded
    after the last slice -- the retry follows immediately.
    """
    import asyncio
    remaining = float(seconds)
    while remaining > 0:
        step = KEEPALIVE_SECONDS if 0 < KEEPALIVE_SECONDS < remaining else remaining
        await asyncio.sleep(step)
        remaining -= step
        if remaining > 0:
            yield KEEPALIVE_EVENT


def cooldown_duration(backoff, retry_after_seconds):
    """Seconds of cooldown to record for a failed attempt.

    Mirrors upstream's `max(backoff, retry_after)` but clamps the guessed half
    of it. A backend that sent no `Retry-After` never told us to wait 1920s --
    we inferred that from a doubling counter, and re-checking sooner costs one
    request. A stated `Retry-After` is the backend's own instruction and is
    never shortened, only bounded by upstream's 24h ceiling.
    """
    try:
        retry_after = min(float(retry_after_seconds or 0), 86_400.0)
    except (TypeError, ValueError):
        retry_after = 0.0
    backoff = float(backoff)
    if MAX_PAUSE_SECONDS > 0:
        backoff = min(backoff, MAX_PAUSE_SECONDS)
    return max(backoff, retry_after)


def stored_account(data, email):
    for stored in data.get("accounts") or []:
        if isinstance(stored, dict) and stored.get("email") == email:
            return stored
    return None


def token_expiry(stored):
    """Seconds-since-epoch expiry, tolerating the milliseconds upstream also writes."""
    try:
        value = float(stored.get("expiresAt") or 0)
    except (TypeError, ValueError):
        return 0.0
    return value / 1000 if value > 10_000_000_000 else value


async def refresh_lease_token(manager, account, run_in_threadpool):
    """Re-point an in-flight account snapshot at a token that is still valid.

    A request carries the account dict that was live when it acquired: every
    later state write rebuilds the store from disk into fresh dicts, so the
    background refresher can never reach a turn that is already running, and
    the lease keeps sending the token captured at acquire time. Acquire only
    guarantees 300s of remaining life while a paused turn routinely outlives
    that, which is where a mid-turn 401 comes from -- and a 401 is scored as
    an account failure, so it stops the gateway rather than just this turn.

    Refresh is attempted only when the stored token is itself close to
    expiring, which is when upstream's own refresher would act too: this adds
    no new class of token request, only the copy back onto the snapshot.
    """
    import time
    from .storage import load_accounts_read_only

    email = account.get("email") if isinstance(account, dict) else None
    if not email:
        return
    data = await run_in_threadpool(load_accounts_read_only)
    stored = stored_account(data, email)
    if stored is not None and token_expiry(stored) <= time.time() + TOKEN_MARGIN_SECONDS:
        # Refreshing writes through storage, so re-read instead of trusting
        # the copy already in hand.
        await run_in_threadpool(manager.refresh_expiring_accounts, int(TOKEN_MARGIN_SECONDS))
        data = await run_in_threadpool(load_accounts_read_only)
        stored = stored_account(data, email) or stored
    if stored is None:
        return
    for field in LEASE_FIELDS:
        if stored.get(field):
            account[field] = stored[field]


@dataclass
class RequestLease:
    manager: object
    email: str
    released: bool = False

    def release(self):
        if not self.released:
            self.released = True
            self.manager.release_account(self.email)


class RequestLeaseMiddleware:
    """Release request-owned slots even if a response body never starts."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        leases = []
        token = _REQUEST_LEASES.set(leases)
        try:
            return await self.app(scope, receive, send)
        finally:
            for lease in leases:
                lease.release()
            _REQUEST_LEASES.reset(token)


def release_tracked_account(manager, email):
    leases = _REQUEST_LEASES.get()
    if leases is None:
        manager.release_account(email)
        return
    for lease in leases:
        if lease.manager is manager and lease.email == email and not lease.released:
            lease.release()
            return


def model_family(model):
    """Mirror AccountManager._model_family so cooldown scopes line up."""
    return "claude" if "claude" in str(model).lower() else "gemini"


def _scope_map(value):
    if isinstance(value, dict):
        return value
    # Schema 1 stored a single expiry per account instead of a scope map.
    return {"account": value} if value else {}


def cooldown_remaining(data, model, now):
    """Seconds until every stored account leaves cooldown; 0 if any is ready.

    Read from persisted state, never inferred from the in-flight counter. A
    slot released between a failed acquire and that counter's read leaves the
    counter empty while the account is perfectly healthy, and treating that as
    a cooldown fails a request whose slot is already free.
    """
    state = data.get("accountState")
    cooldowns = state.get("cooldowns") if isinstance(state, dict) else None
    accounts = data.get("accounts")
    if not isinstance(cooldowns, dict) or not isinstance(accounts, list) or not accounts:
        return 0.0
    family = model_family(model)
    shortest = None
    for account in accounts:
        email = account.get("email") if isinstance(account, dict) else None
        scoped = _scope_map(cooldowns.get(str(email))) if email else {}
        remaining = 0.0
        for scope in ("account", family):
            try:
                expiry = float(scoped.get(scope) or 0)
            except (TypeError, ValueError):
                continue
            remaining = max(remaining, expiry - now)
        if remaining <= 0:
            return 0.0
        shortest = remaining if shortest is None else min(shortest, remaining)
    return shortest or 0.0


def _track(leases, manager, account):
    if account is not None and leases is not None:
        leases.append(RequestLease(manager, account.get('email')))
    return account


async def acquire_without_waiting(manager, model, run_in_threadpool):
    """One acquire attempt for rotation: return None instead of raising.

    Rotation runs while the request already holds the single slot, so waiting
    can only burn the queue timeout, and raising out of a response body that
    has already started tears the stream down: the caller never sees the real
    upstream error, only a disconnected stream. Callers treat None as "no
    alternative account", which is what a busy gateway means here.
    """
    leases = _REQUEST_LEASES.get()
    account = await run_in_threadpool(manager.acquire_account, model)
    return _track(leases, manager, account)


def log_pause(message):
    """Say why the gateway is idle, so a long wait is not mistaken for a hang."""
    print("[*] gcodex: " + message, flush=True)


async def rate_limit_pause_seconds(model, run_in_threadpool, budget):
    """Seconds to wait before retrying a rate-limited attempt; None to give up.

    Called after the failed attempt's cooldown has been recorded, so the
    recorded value is what upstream itself wants us to wait. Returning None
    hands the caller back to its existing failure path.
    """
    import time
    from .storage import load_accounts_read_only

    if budget <= 0:
        return None
    data = await run_in_threadpool(load_accounts_read_only)
    if not account_allowed(data):
        return None
    remaining = cooldown_remaining(data, model, time.time())
    if remaining <= 0:
        remaining = RATE_LIMIT_FALLBACK_PAUSE_SECONDS
    if remaining <= 0 or remaining > budget:
        return None
    return remaining


async def acquire_serially(manager, model, run_in_threadpool):
    """Wait locally for a busy or cooling account, sending Google nothing."""
    import asyncio
    import time
    from fastapi import HTTPException
    from .storage import load_accounts_read_only

    loop = asyncio.get_running_loop()
    deadline = loop.time() + QUEUE_TIMEOUT_SECONDS
    leases = _REQUEST_LEASES.get()
    def acquire_and_track():
        return _track(leases, manager, manager.acquire_account(model))
    next_state_read = None
    wait_budget = ACQUIRE_WAIT_SECONDS
    while True:
        account = await run_in_threadpool(acquire_and_track)
        if account is not None:
            return account
        # Only a recorded cooldown, a stored auth stop, or a bad account count
        # ends the wait early; anything else means another request holds the
        # single slot, whether or not the counter still shows it.
        if next_state_read is None or loop.time() >= next_state_read:
            next_state_read = loop.time() + STATE_POLL_SECONDS
            data = await run_in_threadpool(load_accounts_read_only)
            if data.get(BLOCK_KEY):
                raise HTTPException(403, "gcodex: stopped after an authentication failure; resolve the account error before clearing the local auth stop")
            if len(data.get("accounts", [])) != 1:
                raise HTTPException(409, "gcodex: exactly one Google account must be stored")
            remaining = cooldown_remaining(data, model, time.time())
            if remaining > 0:
                # Pause here instead of failing the turn. Nothing reaches
                # Google while we sleep; no slot is held during the wait, so
                # when the cooldown expires this request competes for the
                # single slot like any other and falls back to the busy wait
                # below if another turn took it first.
                if remaining > wait_budget:
                    raise HTTPException(
                        429,
                        f"gcodex: account is cooling down for {int(remaining) + 1}s, longer than the "
                        f"{int(ACQUIRE_WAIT_SECONDS)}s this gateway waits before a stream starts; "
                        "no request sent to Google",
                        headers={"Retry-After": str(max(1, int(remaining) + 1))})
                log_pause("waiting out a %ds cooldown before sending anything to Google" % (int(remaining) + 1))
                wait_budget -= remaining
                await asyncio.sleep(remaining)
                # The wait is not queue contention: give the busy check its
                # full window again, and re-read state on the next pass.
                deadline = loop.time() + QUEUE_TIMEOUT_SECONDS
                next_state_read = None
                continue
        if loop.time() < deadline:
            await asyncio.sleep(0.1)
            continue
        raise HTTPException(429, "gcodex: gateway busy; wait for the current turn to finish", headers={"Retry-After": "1"})


def account_allowed(data):
    accounts = data.get("accounts")
    if not isinstance(accounts, list) or len(accounts) != 1:
        return False
    return not data.get(BLOCK_KEY)


def stop_after_auth_failure(data, outcome):
    if outcome.category == "auth":
        # Kept outside accountState so upstream migrations and cooldown resets
        # cannot silently clear it. Do not store backend bodies or credentials.
        data[BLOCK_KEY] = True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clear-auth-stop", action="store_true", required=True,
                        help="clear the persistent stop after resolving the account error")
    parser.parse_args()
    from .storage import update_accounts
    def clear(data):
        data.pop(BLOCK_KEY, None)
    update_accounts(clear)
    print("gcodex: authentication stop cleared; no request sent")
