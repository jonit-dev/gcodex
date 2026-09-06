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
CREDS="${CODEX_HOME:-$HOME/.codex}/antigravity-credentials.json"
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
    --high) MODEL="gemini-3.8-flash-high" ;;
    --low)  MODEL="gemini-3.8-flash-low" ;;
    --med|--medium) MODEL="gemini-3.8-flash" ;;
    -m|--model) shift; MODEL="${1:-$MODEL}" ;;
    -m=*|--model=*) MODEL="${1#*=}" ;;
    *) rest+=("$1") ;;
  esac
  shift
done

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
if ! codex-antigravity models list 2>/dev/null | grep -q "^- ${MODEL}:"; then
  echo "gcodex: '$MODEL' is not an Antigravity model (would 404). Using gemini-3.8-flash." >&2
  echo "  Available: $(codex-antigravity models list 2>/dev/null | sed -n 's/^- \([^:]*\):.*/\1/p' | paste -sd' ' -)" >&2
  MODEL="gemini-3.8-flash"
fi

exec codex --profile "$PROFILE" -c "model=\"$MODEL\"" "${rest[@]}"
