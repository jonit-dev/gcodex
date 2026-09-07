#!/usr/bin/env bash
# gcodex setup — run the Codex CLI harness against Gemini 3.x Flash, billed to
# your Google AI Pro subscription instead of API tokens.
#
# This script ships NO credentials. It extracts Google's Antigravity OAuth
# client from the copy of `agy` you already installed, writes the Codex profile
# + launcher, and registers the model catalog. You still log in with your own
# Google account.
#
# Usage:
#   ./setup.sh                 # install everything, then tells you to log in
#   ./setup.sh --uninstall     # remove what this script installed
#
# Requirements: agy (Antigravity CLI), codex (OpenAI Codex CLI), uv, python3.

set -euo pipefail

GCODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
BIN_DIR="${GCODEX_BIN_DIR:-$HOME/.local/bin}"
PROFILE="gcodex"
CONFIG="$GCODEX_HOME/${PROFILE}.config.toml"
# Upstream stores OAuth data in ~/.codex even with a custom Codex profile home.
CREDS="$HOME/.codex/antigravity-credentials.json"
LAUNCHER="$BIN_DIR/gcodex"
GATEWAY_PORT="${GCODEX_PORT:-51122}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[1;36m[gcodex]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[gcodex]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[gcodex]\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
uninstall() {
  say "Uninstalling..."
  rm -f "$LAUNCHER" "$CONFIG" "$GCODEX_HOME/gcodex-model-catalog.py" \
        "$GCODEX_HOME/gcodex-skills-policy.py" \
        "$GCODEX_HOME/gcodex-cli-compat.py"
  python3 "$HERE/patch-gateway.py" --revert 2>/dev/null || true
  warn "Left in place (delete by hand if you want them gone):"
  warn "  $GCODEX_HOME/gcodex.skills   (your skill keep-list — hand-curated)"
  warn "  $CREDS            (Google OAuth client — extracted, not yours to keep or share)"
  warn "  $HOME/.codex/antigravity-accounts.json  (your login tokens)"
  warn "  gateway catalog entries: codex-antigravity models remove gemini-3.8-flash ..."
  say  "Done."
  exit 0
}
[[ "${1:-}" == "--uninstall" ]] && uninstall

# --------------------------------------------------------------------------
# 0. Preflight
command -v python3 >/dev/null || die "python3 not found"
command -v codex   >/dev/null || die "codex CLI not found — install OpenAI Codex first"
command -v uv      >/dev/null || warn "uv not found — needed only if the gateway isn't installed yet"

AGY_BIN="$(command -v agy || true)"
[[ -n "$AGY_BIN" && -r "$AGY_BIN" ]] || die "agy (Antigravity CLI) not found on PATH. Install it: curl -fsSL https://antigravity.google/cli/install.sh | bash"
say "Found agy at $AGY_BIN"

# 1. Gateway (codex-antigravity-auth) present?
if ! command -v codex-antigravity >/dev/null; then
  say "Installing the gateway (codex-antigravity-auth) via uv..."
  command -v uv >/dev/null || die "uv is required to install the gateway"
  uv tool install codex-antigravity-auth
fi
say "Gateway: $(command -v codex-antigravity)"

# 1b. Patch the gateway so Gemini 3.x multi-turn tool calls work.
#     Gemini returns an opaque thoughtSignature on every functionCall part and
#     requires it echoed back on the next turn. Upstream 2.2.0 drops it, so the
#     second turn after any tool call fails with HTTP 400 INVALID_ARGUMENT
#     ("Function call is missing a thought_signature in functionCall parts").
say "Patching the gateway for Gemini thought signatures..."
python3 "$HERE/patch-gateway.py" || die "gateway patch failed — see patch-gateway.py --check"
warn "Re-run ./setup.sh (or python3 patch-gateway.py) after 'uv tool upgrade codex-antigravity-auth'."

# --------------------------------------------------------------------------
# 2. Extract Google's Antigravity OAuth client from the local agy binary.
#    We never ship these; each user derives them from their own install.
say "Extracting the Antigravity OAuth client from your agy binary..."

pick_creds() {
  python3 - "$AGY_BIN" "$GCODEX_HOME" <<'PY'
import re, sys, glob, os
agy, codex_home = sys.argv[1], sys.argv[2]
blob = open(agy, "rb").read()
ids = sorted(set(m.decode() for m in re.findall(
    rb'\d{10,13}-[0-9a-z]{32}\.apps\.googleusercontent\.com', blob)))
secrets = sorted(set(m.decode() for m in re.findall(rb'GOCSPX-[A-Za-z0-9_-]{28}', blob)))
if not ids or not secrets:
    print("ERR no credentials found in agy binary", file=sys.stderr); sys.exit(2)

# Prefer the client the real Antigravity IDE actually logs in with, discovered
# from its auth.log if present. That is the consumer/AI-Pro client; the other
# client in the binary yields SUBSCRIPTION_REQUIRED.
preferred = None
for log in glob.glob(os.path.expanduser("~/.config/Antigravity IDE/logs/**/auth.log"), recursive=True):
    try: txt = open(log, "r", errors="ignore").read()
    except OSError: continue
    m = re.search(r'client_id=(\d{10,13}-[0-9a-z]{32}\.apps\.googleusercontent\.com)', txt)
    if m: preferred = m.group(1); break

client_id = preferred or ids[0]
# Pair the secret: this build pairs the IDE consumer client with the first
# secret. If login returns invalid_client, the other secret is the fallback.
client_secret = secrets[0]
alt_secret = secrets[1] if len(secrets) > 1 else ""
alt_ids = [i for i in ids if i != client_id]

import json
print(json.dumps({
    "client_id": client_id,
    "client_secret": client_secret,
    "_source": "Extracted from your local agy binary at setup time (not shipped in this repo). Google's Antigravity IDE consumer OAuth client.",
    "_fallbacks": {"other_secret": alt_secret, "other_client_ids": alt_ids},
}, indent=2))
PY
}

mkdir -p "$GCODEX_HOME" "$(dirname "$CREDS")"
if [[ -e "$CREDS" || -L "$CREDS" ]]; then
  python3 "$HERE/install-client.py" --check "$CREDS" || die "invalid credentials at $CREDS; move the file aside and rerun setup"
  say "Credentials already present at $CREDS — leaving as-is."
else
  pick_creds | python3 "$HERE/install-client.py" "$CREDS" || die "credential extraction failed"
  say "Wrote $CREDS (chmod 600)."
fi

# --------------------------------------------------------------------------
# 3. Register the Gemini 3.8 Flash catalog entries (idempotent).
say "Registering Gemini 3.8 Flash in the gateway catalog..."
add_model() { codex-antigravity models add "$1" --backend-id "$2" --display-name "$3" \
  --family gemini --context-window 1048576 --default-reasoning-level "$4" "${@:5}" >/dev/null 2>&1 || true; }
add_model gemini-3.8-flash      gemini-3.8-flash-medium "Gemini 3.8 Flash (Medium)" medium --alias gemini-3.8-flash-medium
add_model gemini-3.8-flash-high gemini-3.8-flash-high   "Gemini 3.8 Flash (High)"   high
add_model gemini-3.8-flash-low  gemini-3.8-flash-low    "Gemini 3.8 Flash (Low)"    low

# --------------------------------------------------------------------------
# 4. Install the Codex profile and the launcher.
say "Installing Codex profile -> $CONFIG"
cp "$HERE/model-catalog.py" "$GCODEX_HOME/gcodex-model-catalog.py"
cp "$HERE/skills-policy.py" "$GCODEX_HOME/gcodex-skills-policy.py"
cp "$HERE/cli-compat.py" "$GCODEX_HOME/gcodex-cli-compat.py"
sed "s|__PORT__|$GATEWAY_PORT|g" "$HERE/templates/gcodex.config.toml" > "$CONFIG"

# Skill keep-list. Codex sends every installed skill's description on every
# model call; on a per-request subscription quota that is the largest single
# cost in the request. The launcher drops that catalog and re-advertises only
# the skills named here. Nothing is ever disabled: every skill stays installed,
# enabled and reachable with `$name` in the composer, so an empty keep-list
# means "the model discovers none of them", not "you lost them". Never
# overwrite a list the user has already curated.
SKILLS_KEEP="$GCODEX_HOME/gcodex.skills"
if [[ ! -e "$SKILLS_KEEP" ]]; then
  say "Seeding empty skill keep-list -> $SKILLS_KEEP"
  cat > "$SKILLS_KEEP" <<'SKILLS_EOF'
# gcodex skill keep-list — one skill name per line, '#' starts a comment.
#
# Codex ships every installed skill's name and description in the prompt on
# EVERY model call. gcodex drops that catalog and re-advertises only the skills
# named here. Plain `codex` is untouched.
#
# Nothing is disabled: every installed skill is still reachable by typing
# `$name` in the composer, whether or not it is listed here.
#
# Empty file = the model is told about no skills (smallest prompt).
# GCODEX_SKILLS=1 restores the full catalog for a single run.
SKILLS_EOF
  chmod 600 "$SKILLS_KEEP"
else
  say "Keeping existing skill keep-list at $SKILLS_KEEP"
fi

say "Installing launcher -> $LAUNCHER"
mkdir -p "$BIN_DIR"
sed "s|__PORT__|$GATEWAY_PORT|g" "$HERE/templates/gcodex.launcher.sh" > "$LAUNCHER"
chmod +x "$LAUNCHER"

# --------------------------------------------------------------------------
say "Setup complete."
echo
say "Next: log in with YOUR Google account (the one with AI Pro):"
echo  "    codex-antigravity login"
say "Then run it:"
echo  "    gcodex            # medium    gcodex --high    gcodex --low"
echo
warn "One account only — do not use 'login --count'. Account rotation is what"
warn "reads as abuse. See README for the ban-risk notes."
case ":$PATH:" in *":$BIN_DIR:"*) : ;; *) warn "Add $BIN_DIR to your PATH.";; esac
