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

**Status.** Verified 2026-09-06 against `codex-antigravity-auth` 2.2.0 and
Gemini 3.8 Flash: single-turn prompts, multi-turn tool loops, and `apply_patch`
file edits all work with the gateway patch below applied.

Built on [`codex-antigravity-auth`](https://pypi.org/project/codex-antigravity-auth/).
Not affiliated with, endorsed by, or supported by Google or OpenAI.

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

`patch-gateway.py` fixes it: it installs a small `thought_signatures.py` cache
into the gateway package, records each signature against the `call_id` the
gateway hands to Codex, and re-attaches it when the request history is rebuilt.

```sh
python3 patch-gateway.py              # apply (idempotent; setup.sh runs it)
python3 patch-gateway.py --check      # status
python3 patch-gateway.py --with-dumps # + dump request/error bodies when GCODEX_DUMP=1
python3 patch-gateway.py --revert     # restore pristine files
```

Restart the gateway afterwards. **`uv tool upgrade codex-antigravity-auth`
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

## Ban risk

Read this once. The gateway authenticates as the **Antigravity desktop client**
— it is an *unofficial* path to Gemini through your subscription, not a
sanctioned API. Google has disabled Antigravity access for accounts observed
doing this.

Mitigations baked into gcodex:

- **Single account.** `setup.sh` and the launcher assume one login; never use
  `codex-antigravity login --count`.
- **Loopback only.** The gateway binds `127.0.0.1` — nothing is exposed off your
  machine.
- **Conservative retries.** The profile sets low `request_max_retries` /
  `stream_max_retries` and a long idle timeout. Hammering the endpoint on errors
  is the fastest way to look like abuse — don't add retry loops or rotation.

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
`python3 patch-gateway.py --check`, apply it, and restart the gateway. If the
gateway was restarted mid-conversation, that one conversation can still 400 once
because the signature cache is in-memory — start a new one.

**General health check.**
Run `agy -p "hi"` first. If that returns a Gemini response, your account is
entitled and the problem is in the gateway/config, not your subscription. Also
useful: `codex-antigravity doctor`, `codex-antigravity status`, and
`codex-antigravity models list`.

---

## License

MIT — see [LICENSE](LICENSE).
