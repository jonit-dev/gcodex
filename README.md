# gcodex

Run the OpenAI **Codex CLI harness** against **Gemini 3.8 Flash**, billed to your
**Google AI Pro** subscription instead of API tokens.

`gcodex` is a thin wrapper: it declares a Codex *profile* that points the harness
at a local gateway which speaks the OpenAI Responses protocol on one side and
Google's Antigravity OAuth on the other. Plain `codex` is left completely
untouched — only the `gcodex` launcher switches the backend.

```
Codex harness ──▶ OpenAI Responses protocol ──▶ codex-antigravity gateway
                                                  (localhost:51122, loopback)
                                                        │
                                                        ▼
                                                  Google OAuth ──▶ Gemini 3.8 Flash
```

The provider lives in `~/.codex/gcodex.config.toml` and is selected with
`codex --profile gcodex`, which layers that file over your existing
`~/.codex/config.toml`.

**Status.** Verified 2026-09-07 against `codex-antigravity-auth` 2.2.0 and
Gemini 3.8 Flash: single-turn prompts, multi-turn tool loops, and `apply_patch`
file edits all work with the gateway patch below applied.

Built on [`codex-antigravity-auth`](https://pypi.org/project/codex-antigravity-auth/).
Not affiliated with, endorsed by, or supported by Google or OpenAI.

See [real-usage verification](STABILITY.md) for the tested workflows, fixes,
and remaining limits.

---

## Requirements

- **agy** — the Antigravity CLI. The setup script extracts Google's OAuth client
  from *your own* copy of this binary. Install:
  `curl -fsSL https://antigravity.google/cli/install.sh | bash`
- **codex** — the OpenAI Codex CLI.
- **uv** — used to install the gateway (`codex-antigravity-auth`) if it isn't
  already present.
- **python3** — used by setup to parse the client out of the binary.
- A **Google account with AI Pro**.

---

## Install

```sh
./setup.sh
```

`setup.sh` will:

1. Find your `agy` binary and extract the Antigravity OAuth client from it.
2. Install the gateway (`codex-antigravity-auth`) via `uv` if it's missing.
3. Patch the gateway to preserve Gemini thought signatures (see below).
4. Register the Gemini 3.8 Flash catalog entries (medium / high / low).
5. Install the Codex profile (`~/.codex/gcodex.config.toml`) and the launcher
   (`~/.local/bin/gcodex`).

To undo: `./setup.sh --uninstall`.

### The gateway patch (required)

`codex-antigravity-auth` 2.2.0 (latest as of this writing) does not handle
Gemini 3.x **thought signatures**. Gemini returns an opaque `thoughtSignature`
on every part that carries a `functionCall`, and it must be echoed back verbatim
on that part in the next request. Upstream drops it, so the turn *after* any
tool call fails:

```
HTTP 400 INVALID_ARGUMENT
Function call is missing a thought_signature in functionCall parts.
This is required for tools to work correctly.
```

Single-turn prompts work fine; anything agentic dies on the second turn.

`patch-gateway.py` fixes it: it installs a private `thought_signatures.py` cache
into the gateway package, records each signature against the `call_id` the
gateway hands to Codex, and re-attaches it when the request history is rebuilt.
Signatures survive gateway restarts in `~/.codex/gcodex-signatures/` (directory
mode 700, SQLite file mode 600). The cache stores call IDs and opaque signatures,
not prompts or tool output. It retains up to 100,000 signatures for 30 days
since their last use; memory hits refresh that durable lifetime too.
Very old sessions or sessions created before this update may need a fresh start.

The patch also translates Codex custom tools such as native `apply_patch` into
Gemini function calls and translates the resulting stream back. The launcher
builds a dedicated model catalog from the local gateway so Codex enables these
tools consistently, without depending on its shared model cache.

```sh
python3 patch-gateway.py              # apply (idempotent; setup.sh runs it)
python3 patch-gateway.py --check      # status
python3 patch-gateway.py --revert     # restore pristine files
```

The patch also enforces one stored account, at most one in-flight request per
gateway process, and a persistent stop after an authentication failure. Busy
requests wait locally for up to 30 seconds, then receive a clear busy response;
a rate-limited request waits out its recorded cooldown (see
[Rate limits](#rate-limits-429)). Neither generates additional Google requests
while waiting. Body
dumps are disabled; applying this version removes the old dump hooks.
`--check` exits nonzero if any patch is incomplete or legacy dump hooks remain.
All anchors and Python syntax are validated before writing; write failures
roll back files already replaced. Stop the gateway before patching so it cannot
import modules while they are being replaced, then start it afterwards.
**`uv tool upgrade codex-antigravity-auth`
overwrites the patch** — re-run the script (or `./setup.sh`) after any upgrade,
and check whether upstream has fixed it first, in which case the script will
stop with an "anchor not found" error rather than corrupting anything.

---

## First login

Log in with **your own** Google account — the one that has AI Pro:

```sh
codex-antigravity login
```

**Use exactly one account. Never pass `--count`.** Account rotation is the
pattern that reads as abuse; see **Ban risk** below.

Before blaming the gateway, confirm the account itself is entitled:

```sh
agy -p "hi"
```

If that works, your account is good and any failure is on the gcodex side.

**A failure here does not condemn gcodex.** `agy` reaches Gemini through its own
OAuth client and is metered separately from the Antigravity IDE client the
gateway uses, so `agy` can report `Individual quota reached` — its limit resets
on a multi-day cycle — while gcodex keeps working normally. Observed directly:
two full gcodex sessions completed between two quota-blocked `agy` runs. Treat
this check as confirming entitlement, not as a gcodex health check; for that,
run `gcodex exec "Reply with exactly: ALIVE"`.

`agy` is otherwise only needed at install time, as a *file*: `setup.sh` parses
the OAuth client out of the binary. gcodex never invokes `agy` at runtime.

---

## Usage

```sh
gcodex                 # Gemini 3.8 Flash, medium effort
gcodex --high          # high effort
gcodex --low           # low effort
gcodex --med           # medium effort (default)

GCODEX_MODEL=<id> gcodex     # any id from `codex-antigravity models list`
```

Everything else is passed straight through to `codex`, including subcommands:

```sh
gcodex exec "summarize this repo"
gcodex --high exec "refactor the auth module"
gcodex resume
```

The launcher intercepts `-m` / `--model` / `--model=<id>` and the effort
shorthands so they configure the Gemini backend instead of leaking to `codex`.
It also **validates the chosen model** against the gateway catalog before
launching: if you ask for something Antigravity doesn't serve (e.g.
`gpt-5.6-luna`), it prints the available models and falls back to
`gemini-3.8-flash` rather than letting Google 404 mid-stream.

---

## Prompt size (why this matters on a subscription)

`codex --profile` *layers* config files, so a profile cannot remove what your
base `~/.codex/config.toml` declares. Every installed skill and every MCP server
was therefore shipped on **every model call**. Measured on one request here:

| Part of the request | Bytes | Share |
| --- | ---: | ---: |
| Skill catalog (255 entries) | 81,113 | 71% |
| MCP tool schemas | ~21,000 | 18% |
| Task, coding tools, everything else | ~12,500 | 11% |

That block is resent 12-15 times per task, against a per-request quota. The
launcher now trims both, and both are reversible per invocation:

```sh
gcodex                       # trimmed (default)
GCODEX_SKILLS=1 gcodex       # keep the full skill catalog for this run
GCODEX_MCP=1 gcodex          # keep the MCP servers from your base config
```

**Skills.** Name the ones you want in `~/.codex/gcodex.skills`, one per line.
Those are advertised to the model; plain `codex` is untouched. An empty file
advertises none. Measured on this machine:

| Keep-list | Input tokens per request |
| --- | ---: |
| All 255 entries (before) | 21,169 |
| 24 actively-used skills | 6,673 |
| Empty (catalog dropped) | 2,209 |

**Nothing is disabled, so tagging still works.** Type `$` in the composer and
every installed skill is still there to pick, keep-list or not — a tagged skill
costs nothing until you use it, so there is no reason to hide it from you. The
trim is `skills.include_instructions=false`, which drops the catalog from the
prompt while leaving each skill installed and enabled; `skills-policy.py` then
appends the keep-list to the profile's `developer_instructions` so the model
still knows those exist. The earlier approach — `skills.config` with
`enabled=false` for everything outside the keep-list — cost the same bytes and
also removed those skills from the `$` picker. Note that `skills.enabled=false`
is accepted by Codex and does nothing, and shrinking
`skills.max_context_tokens` prints `Exceeded skills context budget` on each run.

**MCP servers.** Disabled by name, discovered from your config rather than
hardcoded, so the trim follows whatever you have installed.

---

## Rate limits (429)

A rate limit is a wait, not a verdict. Codex is configured with zero retries
here on purpose, so an unhandled 429 ends the turn and you retype the prompt.
The patched gateway instead **pauses and sends one request when the cooldown
expires**:

- Upstream records a cooldown on a 429 — 120s, doubling per consecutive
  failure, capped at 1920s (or Google's `Retry-After`, whichever is longer).
- If the limit lands before any output has reached you, the gateway holds the
  open stream, sleeps that long, and retries on the slot it already owns. One
  request goes out, when the cooldown says it may. Nothing is sent while
  waiting, and the account is never asked twice at once.
- Once tokens have already been streamed, a retry would duplicate them, so the
  failure is reported as before.
- A request that arrives while a cooldown is still live waits it out the same
  way before anything is sent.

Each request has a wait budget, `GCODEX_COOLDOWN_WAIT` (default 900s, read by
the gateway process at start). A cooldown longer than the remaining budget —
the deep end of the backoff, or a quota that resets on a multi-day cycle — is
reported as a 429 with `Retry-After` rather than held open. The profile's
`stream_idle_timeout_ms` is set to 20 minutes to cover the pause; Codex 0.153.4
was verified against a stub provider to accept 900s of silence before the first
event and still render the answer.

The pause is visible in the gateway log
(`~/.codex/antigravity-gateway-<port>.log`):

```
[*] gcodex: rate limited; waiting 121s before retrying this turn
```

Codex itself shows nothing during the wait, so a turn that seems to hang is
worth checking against that log before assuming it died.

---

## Ban risk

Read this once. The gateway authenticates as the **Antigravity desktop client**
— it is an *unofficial* path to Gemini through your subscription, not a
sanctioned API. Google has disabled Antigravity access for accounts observed
doing this.

Mitigations baked into gcodex:

- **Single account enforced.** The patched gateway refuses to select an account
  unless exactly one account is stored. It permits one in-flight request per
  gateway process. Use one gateway process; do not start parallel gateways.
- **Loopback only.** The gateway binds `127.0.0.1` — nothing is exposed off your
  machine.
- **No Codex retries.** The profile sets `request_max_retries` and
  `stream_max_retries` to zero. The gateway retains upstream rate-limit cooldowns
  and stops persistently after authentication failures, including 401/403.
  This stop survives gateway restarts and ordinary cooldown expiry.
- **Rate limits pause instead of retrying.** A 429 makes the gateway wait out
  the recorded cooldown and then send *one* request, rather than the client
  retrying (see below). The wait is local: nothing reaches Google during it.

After resolving an authentication error, deliberately clear the stop using the
gateway's Python interpreter (for the default uv installation):

```sh
~/.local/share/uv/tools/codex-antigravity-auth/bin/python -m codex_antigravity_auth.gateway_safety --clear-auth-stop
```

This only clears local state; it does not restore revoked access. These controls
limit accidental traffic and do not make third-party OAuth access sanctioned.

If Google warns you or you see access revoked, **stop.** This is your risk to
accept.

---

## How it stays secret-free

This repo ships **zero credentials**. The OAuth `client_id` / `client_secret`
belong to Google and are *not* in any file here. `setup.sh` extracts them from
**your own local `agy` binary** at install time and writes them to
`~/.codex/antigravity-credentials.json` (chmod 600), which `.gitignore` keeps
out of the repo. Nothing derived from those secrets is ever committed or pushed.

If you clone this repo, you get the wiring — not anyone's keys. You supply your
own by having `agy` installed.

Credential installation validates JSON and publishes a private file atomically.
Existing invalid files are rejected with a recovery message. OAuth data stays in
`~/.codex`, matching the gateway, even when `CODEX_HOME` selects another profile
directory. Legacy `/tmp/gcodex-req.json` dumps are not deleted by setup; restrict
their permissions or remove them yourself if you previously enabled dumping.

Run local regression checks with `python3 -B -m unittest discover -s tests -v`.
When the gateway is installed, the suite also patches an isolated copy and tests
the real account-selection code without making network requests.

---

## Troubleshooting

**`HTTP 404` mid-stream.**
You passed a model Antigravity doesn't serve (e.g. `-m gpt-5.6-luna`). gcodex is
Gemini-only. The launcher's fallback catches this and switches you to
`gemini-3.8-flash`, but don't force foreign models — pick from
`codex-antigravity models list`.

**`invalid_client` at login.**
The extracted `client_secret` didn't match the client. `setup.sh` writes a
fallback into `~/.codex/antigravity-credentials.json` under
`_fallbacks.other_secret` — swap the `client_secret` for that value and retry.

**`SUBSCRIPTION_REQUIRED` / "no valid license" (#1001) at request time.**
The wrong OAuth client was used. The correct one is the **Antigravity IDE
consumer client** — the `client_id` that appears in
`~/.config/Antigravity IDE/logs/*/auth.log`, *not* the other client baked into
the binary. `setup.sh` already prefers the auth.log client; if you hit this,
re-check that discovery. The working client id family starts `1071006060591-…`;
the other family (`884354919052-…`) produces this error.

**`HTTP 400` right after the model's first tool call.**
The gateway patch isn't applied (or an upgrade wiped it). Run
`python3 patch-gateway.py --check`, apply it, and restart the gateway. Current
sessions retain their signatures across restarts. Sessions created before the
durable-cache update, expired sessions, or a deleted signature database may
require a new conversation.

**A slash command such as `/goal` is missing under `gcodex`.**
The profile trims Codex's tool surface, and some slash commands are gated on a
feature flag rather than on the model. `goals` is enabled; `apps` and
`multi_agent` are not, so their commands stay hidden here while plain `codex`
keeps them. Check the `[features]` block in `~/.codex/gcodex.config.toml`, and
re-run `./setup.sh` if it does not match `templates/gcodex.config.toml`.

**A turn sits there for minutes with no output.**
Most likely the gateway is waiting out a rate-limit cooldown, which is the
intended behaviour. `tail -f ~/.codex/antigravity-gateway-51122.log` and look
for `gcodex: rate limited; waiting`. Set `GCODEX_COOLDOWN_WAIT=0` before
starting the gateway if you would rather have the 429 back immediately.

**General health check.**
Run `agy -p "hi"` first. If that returns a Gemini response, your account is
entitled and the problem is in the gateway/config, not your subscription. Also
useful: `codex-antigravity doctor`, `codex-antigravity status`, and
`codex-antigravity models list`.

---

## License

MIT — see [LICENSE](LICENSE).
