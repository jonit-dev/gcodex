#!/usr/bin/env bash
# gcodex — Codex CLI harness on Gemini 3.8 Flash, paid by your Google AI Pro
# subscription rather than API tokens. Installed to ~/.local/bin by setup.sh.
#
#   gcodex              Gemini 3.8 Flash, medium effort
#   gcodex --high       high effort
#   gcodex --low        low effort
#   GCODEX_MODEL=id     any id from `codex-antigravity models list`
#
# Chain:
#   Codex harness -> OpenAI Responses protocol -> local gateway (:__PORT__)
#                 -> Google OAuth -> Gemini 3.8 Flash
#
# The provider is declared in ~/.codex/gcodex.config.toml and selected with
# `codex --profile gcodex`, which layers that file over ~/.codex/config.toml.
# Plain `codex` is untouched.
#
# BAN RISK, read once: the gateway authenticates as the Antigravity desktop
# client. Google has disabled Antigravity access for accounts seen doing this.
# Mitigations here: exactly one account, loopback-only binding, conservative
# retries. Do NOT add account rotation — that is the pattern that reads as abuse.

set -euo pipefail

PROFILE="gcodex"
CONFIG="${CODEX_HOME:-$HOME/.codex}/${PROFILE}.config.toml"
CREDS="$HOME/.codex/antigravity-credentials.json"
PORT="__PORT__"
MODEL="${GCODEX_MODEL:-gemini-3.8-flash}"

[[ -r "$CONFIG" ]] || { echo "gcodex: missing $CONFIG — run setup.sh" >&2; exit 1; }

# Effort shorthand and model selection are consumed here so a caller-supplied
# model is never passed straight to codex. gcodex only serves Antigravity
# models; a foreign id like gpt-5.6-luna makes Google return HTTP 404. Any -m
# / --model is captured, then validated against the gateway catalog below.
rest=()
while (( $# )); do
  case "$1" in
    --) rest+=("$@"); break ;;
    --high) MODEL="gemini-3.8-flash-high" ;;
    --low)  MODEL="gemini-3.8-flash-low" ;;
    --med|--medium) MODEL="gemini-3.8-flash" ;;
    -m|--model)
      [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || { echo "gcodex: $1 requires a model id" >&2; exit 2; }
      shift; MODEL="$1" ;;
    -m=*|--model=*)
      MODEL="${1#*=}"
      [[ -n "$MODEL" ]] || { echo "gcodex: model id must not be empty" >&2; exit 2; } ;;
    *) rest+=("$1") ;;
  esac
  shift
done

# Help, version and shell completion describe the CLI rather than talk to a
# model. Starting the gateway for them means `gcodex --help` fails on a machine
# that has not logged in yet, which is exactly the machine reading the help.
describes_cli=0
[[ "${rest[0]:-}" == "completion" ]] && describes_cli=1
for argument in "${rest[@]+"${rest[@]}"}"; do
  case "$argument" in
    --) break ;;
    -h|--help|-V|--version) describes_cli=1; break ;;
  esac
done
if (( describes_cli )); then
  exec codex --profile "$PROFILE" "${rest[@]+"${rest[@]}"}"
fi

if [[ ! -r "$CREDS" && -z "${ANTIGRAVITY_CLIENT_ID:-}" ]]; then
  echo "gcodex: no OAuth client credentials. Run setup.sh, then: codex-antigravity login" >&2
  exit 1
fi

# Bring the gateway up on demand, loopback only, and leave it running.
if ! codex-antigravity status --port "$PORT" 2>/dev/null | grep -q "reachable: yes"; then
  echo "gcodex: starting gateway on 127.0.0.1:$PORT ..." >&2
  codex-antigravity start --port "$PORT" --host 127.0.0.1 --background >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    codex-antigravity status --port "$PORT" 2>/dev/null | grep -q "reachable: yes" && break
    sleep 0.5
  done
fi
codex-antigravity status --port "$PORT" 2>/dev/null | grep -q "reachable: yes" \
  || { echo "gcodex: gateway did not come up. See ~/.codex/antigravity-gateway-${PORT}.log" >&2; exit 1; }

# Validate the model against the gateway catalog. A foreign id (gpt-*, o1,
# luna, sol, ...) is not served by Antigravity and returns HTTP 404 mid-stream,
# so fall back to the Gemini default with a clear warning instead.
catalog="$(codex-antigravity models list)" || { echo "gcodex: cannot read model catalog" >&2; exit 1; }
models="$(printf '%s\n' "$catalog" | sed -n 's/^- \([^:]*\):.*/\1/p')"
if ! grep -Fxq -- "$MODEL" <<< "$models"; then
  echo "gcodex: '$MODEL' is not an Antigravity model (would 404). Using gemini-3.8-flash." >&2
  echo "  Available: $(paste -sd' ' - <<< "$models")" >&2
  MODEL="gemini-3.8-flash"
  grep -Fxq -- "$MODEL" <<< "$models" || { echo "gcodex: default model missing; run setup.sh" >&2; exit 1; }
fi

[[ "$MODEL" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || { echo "gcodex: invalid model id" >&2; exit 2; }

CATALOG_HELPER="$(dirname "$CONFIG")/gcodex-model-catalog.py"
[[ -r "$CATALOG_HELPER" ]] || { echo "gcodex: missing model catalog helper; run setup.sh" >&2; exit 1; }
catalog_override="$(python3 "$CATALOG_HELPER" "$PORT" "$(dirname "$CONFIG")/gcodex.models.json")" || exit 1

# ---------------------------------------------------------------------------
# Trim the per-request prompt.
#
# A profile cannot drop what the base config already declares: `codex --profile`
# layers files, so every skill and every MCP server in ~/.codex/config.toml is
# still sent on EVERY model call. Measured on this setup, one request carried
# 81 KB of skill descriptions and 21 KB of MCP tool schemas against 12 KB of
# actual task and coding tools -- ~87% overhead, re-sent 12-15 times per task.
#
# Gemini is billed per request through a subscription with a weekly cap, so that
# overhead is the difference between finishing a task and exhausting the quota.
# Both trims are reversible per invocation:
#
#   GCODEX_SKILLS=1  keep the full skill catalog
#   GCODEX_MCP=1     keep the MCP servers declared in the base config
#
# Server names are discovered from the config, never hardcoded, so this adapts
# to whatever the user has installed.
# ---------------------------------------------------------------------------
trim=()
if [[ "${GCODEX_SKILLS:-0}" != "1" ]]; then
  # Drop the catalog from the prompt, but leave every skill installed AND
  # enabled so `$name` in the composer still finds it. The obvious trim --
  # skills.config with enabled=false for everything outside the keep-list --
  # costs the same prompt bytes and also deletes those skills from the `$`
  # picker, so a user cannot reach them by hand either. include_instructions
  # keeps them one keystroke away. (skills.enabled=false is accepted and
  # changes nothing; a squeezed max_context_tokens just prints "Exceeded
  # skills context budget" on every run.)
  trim+=(-c "skills.include_instructions=false")
  # The keep-list is then re-advertised to the model by appending it to the
  # profile's own developer_instructions, so the handful of skills you rely on
  # stay discoverable without the other two hundred riding along.
  SKILLS_KEEP="$(dirname "$CONFIG")/gcodex.skills"
  SKILLS_POLICY="$(dirname "$CONFIG")/gcodex-skills-policy.py"
  if [[ -r "$SKILLS_KEEP" && -r "$SKILLS_POLICY" ]]; then
    skills_override="$(python3 "$SKILLS_POLICY" "$SKILLS_KEEP" --config "$CONFIG" 2>/dev/null || true)"
    if [[ -n "$skills_override" ]]; then
      trim+=(-c "$skills_override")
    fi
  fi
fi
if [[ "${GCODEX_MCP:-0}" != "1" ]]; then
  while IFS= read -r server; do
    [[ -n "$server" ]] || continue
    trim+=(-c "mcp_servers.${server}.enabled=false")
  done < <(python3 - "$HOME/.codex/config.toml" <<'PYEOF'
import re, sys
try:
    text = open(sys.argv[1], encoding='utf-8', errors='replace').read()
except OSError:
    raise SystemExit(0)
seen = []
# Match [mcp_servers.NAME] and [mcp_servers.NAME.anything]; take NAME only.
for name in re.findall(r'^\[mcp_servers\.([A-Za-z0-9_-]+)', text, re.M):
    if name not in seen:
        seen.append(name)
print('\n'.join(seen))
PYEOF
)
fi

# ---------------------------------------------------------------------------
# Recover the two command lines the installed Codex CLI rejects for reasons
# that have nothing to do with which model is behind the harness:
#
#   gcodex exec -i shot.png 'what is this'
#       --image is variadic, so clap swallows the prompt as another filename
#       and the run dies on "No prompt provided via stdin".
#
#   gcodex review --uncommitted 'review only the coverage change'
#       codex refuses the scope selector together with a prompt, and the two
#       are not interchangeable: a bare `review PROMPT` is handed no diff at
#       all, so neither half can simply be dropped.
#
# The helper rewrites both, printing NUL-separated fields -- an optional -c
# override first, then the arguments -- and exits non-zero when the line needs
# no rewriting, in which case it is forwarded exactly as typed.
# ---------------------------------------------------------------------------
COMPAT="$(dirname "$CONFIG")/gcodex-cli-compat.py"
if [[ -r "$COMPAT" ]]; then
  compat=()
  while IFS= read -r -d '' field; do
    compat+=("$field")
  done < <(python3 "$COMPAT" --config "$CONFIG" --instructions "${skills_override:-}" \
             -- "${rest[@]+"${rest[@]}"}" || true)
  if (( ${#compat[@]} )); then
    if [[ -n "${compat[0]}" ]]; then
      trim+=(-c "${compat[0]}")
    fi
    rest=("${compat[@]:1}")
  fi
fi

exec codex --profile "$PROFILE" -c "$catalog_override" -c "model=\"$MODEL\"" \
     "${trim[@]}" "${rest[@]}"
