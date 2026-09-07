# Real-usage verification — 2026-09-07

Verified with Codex CLI 0.153.4, codex-antigravity-auth 2.2.0, and the installed
Gemini 3.8 Flash medium/high/low catalog entries.

| Check | Observed result |
| --- | --- |
| Edit/test workflow | gcodex repaired a broken duration parser; all 13 fixture tests independently passed. |
| Native tools and restart/resume | Native `apply_patch` created a file. After restarting the gateway, the same session recalled its token, appended a second line using the native tool, and completed its tests. Both lines were independently verified. |
| Effort selection | Low and high each ran the fixture tests successfully; medium handled the edit/resume workflows. |
| Concurrent clients | Two clients started simultaneously and both returned their expected markers. Local tests verify waiting, timeout, and cancellation without releasing another request's slot. |
| HTTP disconnect recovery | A localhost FastAPI/uvicorn fixture using the actual lease middleware completed 20 stream-disconnect/follow-up cycles and eight concurrent follow-ups: 48 acquisitions, 48 releases, zero slots left occupied, peak occupancy one. It uses a fake account manager and makes no Google requests. |
| Local regressions | 93 tests passed, including an isolated install/reapply/revert/reinstall cycle against the installed gateway's original source files, warm-cache expiry/restart consistency, and checks that all three comparison graders reject broken starters. |

## Fixes made during iteration

Signatures now survive restarts in a private SQLite cache. Generated call IDs use
full UUIDs. The patcher refuses unsafe reverts when backups are missing. Busy
requests wait locally for up to 30 seconds and receive an explicit busy response
on timeout. Authentication stops also prevent background token refresh.

A waiter no longer reports a cooldown that does not exist. `acquire_serially`
used to decide between "gateway busy" and "account is cooling down" by reading
the in-flight counter after its own acquire had already failed. `release_email`
pops the counter's key, so a slot released in that window left the counter empty
and the waiter raised `429 gcodex: account is cooling down; no request sent to
Google` **immediately** — no wait, no retry, and a free slot. Codex spends its
retry budget on those and ends the turn with `exceeded retry limit, last status:
429`. Cooldowns are now read from persisted `accountState`, scoped per account
and model family, so only a recorded cooldown, an auth stop, or a bad account
count ends the wait early; everything else queues to the deadline and then
reports busy. Four regression tests cover the race, an expired cooldown, and
both cooldown scopes; two of them fail against the previous code.

Measured on this machine with three concurrent gcodex sessions of 16 tool calls
each — before: three 429s, two sessions killed, 19 requests served; after: one
429 (the deliberate 30-second busy timeout), one session killed, 38 requests
served. Six direct requests issued while a session held the slot returned 429
`cooling down` on the first attempt before the fix and 200 on every attempt
after it.

A mid-stream account rotation can no longer kill the stream. `acquire_serially`
replaced upstream's non-raising `acquire_account` at every call site, but three
of them are *rotation* attempts that upstream treats as "no alternative account"
when they return `None`. Rotation runs while the request still holds the single
slot (`AccountState._select` allows one in-flight per account, and the lease is
released only in `managed_sse_generator`'s `finally`), so the waiting acquire
could never succeed: it burned the full 30-second queue timeout and then raised
`429 gateway busy` from inside an SSE body that had already started. Starlette
turns that into `RuntimeError: Caught handled exception, but response already
started` and aborts the connection, so Codex reported `stream disconnected
before completion` and exited 1 instead of receiving the real upstream error —
and nothing was written to the request log, because the crash preceded
`log_request`. Four such aborts were recorded in one gateway log. Rotation now
calls `rotate_active_account_for_request`, a non-waiting, non-raising helper;
only the initial acquire, which runs before any response begins, still raises.
A regression test walks the patched `server.py` AST and fails against the
previous patcher, naming all three bad call sites.

Warm signature-cache hits now check expiry and refresh the durable last-use
timestamp. Two regression tests first reproduced the prior inconsistency:
expired signatures remained usable in memory, while recently used signatures
could disappear after a restart. Both now pass using a controlled clock and real
SQLite storage.
After installing this change and restarting the gateway, a live Codex session
completed two successive shell-tool calls and reported both independently
observed markers (`SIGNATURE_ROUNDTRIP_OK` and `SECOND_TOOL_OK`).

The gateway now translates Responses custom tools and their histories in both
directions. A dedicated model catalog enables Codex's native patch tool reliably.
The initial native-edit probe exposed a false completion claim by the model;
independent file checks caught it. The metadata fix made the actual native tool
available, after which the file creation and resumed edit both succeeded.

A rate limit no longer ends the turn. Upstream records a cooldown on a 429 (120s
doubling per consecutive failure to 1920s, or Google's `Retry-After`), and both
the pre-request acquire and the mid-stream failure path turned that into an
immediate `429` for a client configured with zero retries — so the turn died and
the prompt had to be retyped, with no request having reached Google. Both paths
now wait the recorded cooldown out and then send exactly one request: the
pre-request wait happens before anything is sent, and the mid-stream retry runs
only while `visible_output_started` is false, on the slot the request already
owns, so no output is ever duplicated and the account is never asked twice at
once. Each request carries a wait budget (`GCODEX_COOLDOWN_WAIT`, default 900s);
a cooldown longer than what is left of it is still reported as a 429 with
`Retry-After`.

Evidence: a 45-second cooldown was written into the real account store and
`gcodex exec` then answered in 44 seconds, with
`[*] gcodex: waiting out a 41s cooldown before sending anything to Google` in
the gateway log; before the change the same state returned `429 ... cooling
down` at once. Six regression tests cover the waiting acquire, the budget
ceiling, and a cooldown that keeps being extended; two more execute the real
patched `sse_generator` from the installed sources against a stubbed transport
that fails with 429 once — it retries after one sleep with the fix and makes a
single attempt without it, and a rate limit arriving after visible output is
still reported rather than retried.

The pause is only safe because the client tolerates the silence. Codex 0.153.4
was measured against a stub Responses provider that withheld its answer for 180s
before the response headers and for 180s after them, and then for the full 900s
budget after them: all three completed and rendered the answer, exit 0 (903s
wall clock for the last). The profile's `stream_idle_timeout_ms` is now 20
minutes so the longest permitted wait fits inside it.

Skill tagging survives the prompt trim. The trim used to disable every skill
outside `~/.codex/gcodex.skills` with `skills.config`, which also removed them
from the `$` picker in the composer: typing `$audit` under gcodex returned "no
matches", so a skill you had chosen deliberately was unreachable even by name.
The catalog is now dropped with `skills.include_instructions=false` — same
prompt saving, nothing disabled — and the keep-list is appended to the profile's
`developer_instructions` instead, so the model still knows those skills exist.
Verified live: `$audit` under gcodex now lists `seo-audit`, `squirrelscan` and
`feature-mining-sandbox-validation`, none of them in the keep-list; and a tagged
skill's SKILL.md arrives in the request as a `<skill>` block with its path,
confirmed by dumping the request body at a stub provider. A separate dump
confirms the injected block carries both the profile's own verification guidance
and the keep-list entries.

## Practical limits

The later [harness-comparison pilot](benchmarks/README.md) exposed a local busy
timeout despite correct generated code. Request-wide lease tracking was added
to cover cancellation after acquisition and response bodies that never start.
The earlier workflow checks alone did not establish comparative superiority.

This is workflow evidence, not a guarantee of error-free model behavior or a
long-duration production soak. One resumed edit initially used an invalid hunk
header; Codex reported that error and the model corrected it successfully.
Human review and project tests remain necessary for generated code.

Use one gateway process, and one gcodex session at a time. The gateway serves
exactly one Google request at once — deliberately, since parallel traffic on a
single account is the pattern that reads as abuse. A second session queues for
`QUEUE_TIMEOUT_SECONDS` (30) and then gets `429 gcodex: gateway busy`. Tool-heavy
turns hold the slot for minutes, so running gcodex in a swarm still loses
sessions even with the false-cooldown race fixed. The request limit is per
process. Signatures are retained
for 30 days since last use with a 100,000-entry disk limit; pre-update or expired sessions may
need a fresh conversation. Upstream gateway upgrades require reapplying and
checking the patch before use.

Run local checks:

```sh
python3 -B -m unittest discover -s tests -v
python3 patch-gateway.py --check
```

Run the real-socket disconnect probe using the gateway environment's Python
(requires its existing FastAPI, httpx, and uvicorn dependencies):

```sh
~/.local/share/uv/tools/codex-antigravity-auth/bin/python tests/gateway_http_probe.py -v
```

The probe binds an ephemeral loopback port, does not read account storage or
credentials, and stops its fixture server after testing. This is a separate
integration check, not included in the 93 dependency-free regression tests.
Adding `--negative-control` deliberately disables only the fixture's middleware
and shortens its local queue timeout: the first follow-up fails with HTTP 429.
That expected failure was observed, confirming the probe detects the leaked-slot
condition. No installed gateway code is modified by either mode.

The live fixture was a disposable temporary directory; its files are throwaway
test data. No user project files were edited by the live helper workflows.
