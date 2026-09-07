"""Local account guards for the gcodex gateway patch."""
from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass

BLOCK_KEY = "gcodexAuthBlocked"
QUEUE_TIMEOUT_SECONDS = 30
# A waiter re-reads persisted account state at most this often while queueing.
STATE_POLL_SECONDS = 1.0
_REQUEST_LEASES = ContextVar('gcodex_request_leases', default=None)


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


async def acquire_serially(manager, model, run_in_threadpool):
    """Wait locally for a busy account, without retrying any Google request."""
    import asyncio
    import time
    from fastapi import HTTPException
    from .storage import load_accounts_read_only

    loop = asyncio.get_running_loop()
    deadline = loop.time() + QUEUE_TIMEOUT_SECONDS
    leases = _REQUEST_LEASES.get()
    def acquire_and_track():
        account = manager.acquire_account(model)
        if account is not None and leases is not None:
            leases.append(RequestLease(manager, account.get('email')))
        return account
    next_state_read = None
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
                raise HTTPException(429, "gcodex: account is cooling down; no request sent to Google",
                                    headers={"Retry-After": str(max(1, int(remaining) + 1))})
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
