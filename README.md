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

`gcodex --help`, `--version` and `completion` answer without starting the
gateway, so they work before you have logged in.

### Images

```sh
gcodex exec -i screenshot.png "what is wrong with this layout?"
```

Attaching an image works, and pasting one into the TUI works. Two things had to
be fixed for that, both of which look like the model ignoring the image:

- Codex only sends an attachment when the model catalog says the model accepts
  one, and `codex-antigravity models add` has no modality flag, so every entry
  gcodex registers came back text-only. The catalog gcodex hands to Codex now
  declares `image` input for the Gemini and Claude entries. `gpt-oss` and
  `ollama` entries are left exactly as the gateway reported them.
- `codex`'s own `--image` is variadic (`<FILE>...`), so `-i shot.png "prompt"`
  hands the prompt to clap as a second filename and the run dies with
  `No prompt provided via stdin`. gcodex rewrites those to `--image=shot.png`,
  which binds one value and leaves the prompt alone.

### `codex review` with your own instructions

```sh
gcodex review --uncommitted "only flag coverage-integrity defects"
```

Plain `codex` rejects this — `the argument '--uncommitted' cannot be used with
'[PROMPT]'` — and the two halves are not interchangeable: a bare
`codex review "prompt"` is handed **no diff at all**. gcodex keeps both. The
selector stays on the command line so the review still sees the right changes,
and your instructions are appended to the profile's `developer_instructions`,
alongside the skill keep-list rather than replacing it. The same applies to
`--base <branch>` and `--commit <sha>`. A one-line note on stderr says it
happened.

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
The patched gateway instead **waits out the limit and picks the turn back up
by itself** — capped exponential backoff, one request per interval:

- Upstream records a cooldown on a 429: 120s, doubling per consecutive failure.
  The patch caps that ladder at `GCODEX_MAX_PAUSE` (default 300s) instead of
  upstream's 1920s, because the tail of the ladder is a *guess* — nobody told
  us to wait that long. Google's own `Retry-After` is never shortened.
- If the limit lands before any output has reached you, the gateway holds the
  open stream, sleeps the cooldown out, and retries on the slot it already
  owns. One request goes out, when the cooldown says it may. Nothing is sent
  while waiting, and the account is never asked twice at once.
- Each retry that also gets limited waits the next rung, so a turn rides out a
  string of limits: 120s, 240s, then 300s a rung until the budget is spent.
  Every attempt is recorded, not just the first: upstream keeps one outcome
  per account per request, which is right when each attempt is a *different*
  account and wrong for a retry, where it meant no new cooldown was written,
  the next pause read a stale one, and the ladder never climbed off its
  fallback interval.
- Each retry is sent with a token that is still valid. The account a request
  holds is a snapshot taken when it acquired, and the background refresher
  writes to a different copy, so a turn that pauses would otherwise keep
  presenting the token it captured — good for as little as 300s — and collect
  an HTTP 401 that counts against the *account*, not just the turn.
- Once tokens have already been streamed, a retry would duplicate them, so the
  failure is reported as before.
- A request that arrives while a cooldown is still live waits it out the same
  way before anything is sent.

**Keeping the stream alive.** Codex drops a stream that goes quiet for
`stream_idle_timeout_ms`, and it measures that between SSE *events*: verified
against Codex 0.153.4, keepalive comment lines every 2s still tripped an 8s
idle timeout, while an event type it does not recognise reset the timer, was
discarded, and left the answer intact. So the gateway emits one such event
(`response.gcodex_keepalive`) every `GCODEX_KEEPALIVE` seconds (default 60)
while it waits. Nothing of it reaches the transcript; it exists so a long wait
is not mistaken for a dead connection.

Three knobs bound the wait, all read by the gateway process at start:

| Variable | Default | Covers |
| --- | --- | --- |
| `GCODEX_MAX_PAUSE` | 300s | Longest single wait we impose ourselves. `0` restores upstream's uncapped ladder. |
| `GCODEX_COOLDOWN_WAIT` | 3600s | Total wait per turn, across every limit that turn hits. Keepalives hold the stream, so this is not bounded by the idle timeout. |
| `GCODEX_ACQUIRE_WAIT` | 900s | Wait before a stream has started. No headers have been sent yet, so this one is plain silence — Codex 0.153.4 was verified against a stub provider to accept 900s of it and still render the answer. |
| `GCODEX_TOKEN_MARGIN` | 300s | Token life a retry insists on. Below it the gateway refreshes before sending; upstream uses the same figure when handing out an account. |

A wait that cannot fit its budget — a `Retry-After` measured in hours, or a
turn that has already spent its hour — is reported as a 429 with `Retry-After`
rather than held open, and the turn ends the old way.

The pause is visible in the gateway log
(`~/.codex/antigravity-gateway-<port>.log`):

```
[*] gcodex: rate limited; waiting 121s before retrying this turn
```

Codex displays a commentary message when each cooldown starts:
`Rate limited. Please wait 300 seconds; gcodex will retry automatically.`
This covers both a cooldown already in progress and a limit encountered during
the turn. The duration is the next retry delay, not a guarantee that quota will
be available then. A turn can spend `GCODEX_COOLDOWN_WAIT` waiting; lower it if
you would rather be told sooner.

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
  retrying (see below). The wait is local: nothing reaches Google during it,
  and capping the backoff shortens the wait, never the number of requests —
  one per interval, at most twelve in the hour a turn may wait.

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

**`HTTP 401` on a turn that had been waiting a while.**
Fixed as of the token re-point described under [Rate limits](#rate-limits-429):
a paused turn now refreshes before each retry instead of presenting the token
it captured when it started. If you see one on an older build, the account is
not necessarily in trouble — check whether the turn had been waiting longer
than the token had left. Clear the auth stop it sets with the
`--clear-auth-stop` command above once the account itself is healthy.

**A turn sits there for minutes with no output.**
Most likely the gateway is waiting out a rate-limit cooldown, which is the
intended behaviour — it retries by itself, up to an hour per turn by default.
Each cooldown should display a `Rate limited. Please wait ...` message. If it
does not, apply the current gateway patch and restart the gateway.
`tail -f ~/.codex/antigravity-gateway-51122.log` and look for
`gcodex: rate limited; waiting`. Set `GCODEX_COOLDOWN_WAIT=0` before starting
the gateway if you would rather have the 429 back immediately, or lower it to
bound how long a turn may hang.

**General health check.**
Run `agy -p "hi"` first. If that returns a Gemini response, your account is
entitled and the problem is in the gateway/config, not your subscription. Also
useful: `codex-antigravity doctor`, `codex-antigravity status`, and
`codex-antigravity models list`.

---

## License

MIT — see [LICENSE](LICENSE).
