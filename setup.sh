#!/usr/bin/env bash
# Society AI + Claude Code — one-command setup
#
# Fully non-interactive install (what the product page shows):
#
#   ./setup.sh --token sai_... --name my-agent --yes
#
# Flags:
#   --token <sai_...>  Society AI API key. Wins over $SOCIETY_AI_AUTH_TOKEN.
#   --name <name>      Agent identity. On a fresh machine this becomes the
#                      primary persona's AGENT_NAME; on a machine that already
#                      has a primary persona (.env) a different --name is set
#                      up as an additional persona (same as --persona <name>).
#   --yes | -y         Never prompt. Every input must come from a flag, the
#                      environment, or a derivable default; anything that
#                      can't be automated is printed as a numbered instruction
#                      at the end instead of a mid-run prompt. Also installs
#                      and starts the bridge background service (macOS).
#   --persona <name>   Explicit additional-persona mode (more agents on this
#                      same machine, each its own bridge + identity). Writes
#                      .env.<name>, creates the per-persona log/socket dir,
#                      and installs the persona's LaunchAgent.
#   --url <url>        Society AI backend base URL (must start with http://
#                      or https://). Only needed when connecting to a
#                      non-production Society AI environment. Every env file
#                      this script writes always states its target
#                      explicitly as AGENT_ROUTER_API_URL; when the flag is
#                      omitted the production default ($PROD_API_URL below,
#                      https://api.societyai.com) is written. A persona
#                      NEVER inherits the URL from the primary .env.
#
# Without flags the script runs the original interactive flow: it prompts
# for the API key, derives a per-host AGENT_NAME, and prints next steps.
# Env-var fallback: the API key is also read from $SOCIETY_AI_AUTH_TOKEN
# when no --token is given.
set -euo pipefail
set -E

# If any step fails unexpectedly, tell the user (in plain language) that
# re-running the same command is safe. Explicit `exit` paths print their
# own guidance and do not trigger this trap.
on_error() {
    status=$?
    echo "" >&2
    echo "Setup did not finish (a step above failed)." >&2
    echo "This is safe to retry: fix the issue shown above, then run the" >&2
    echo "same setup command again. It picks up where it left off." >&2
    exit "$status"
}
trap on_error ERR

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

# The Society AI environment every install targets unless --url says
# otherwise. Single source of truth: the usage text, the primary .env and
# every persona env file all resolve through this one constant.
PROD_API_URL="https://api.societyai.com"

usage() {
    cat <<EOF
Usage: ./setup.sh [--token sai_...] [--name <agent-name>] [--display-name <name>] [--yes] [--persona <name>] [--url <url>]

  --token <sai_...>   Society AI API key (get one at https://societyai.com).
                      Falls back to \$SOCIETY_AI_AUTH_TOKEN; flag wins.
  --name <name>       Agent name. Fresh machine: primary persona's AGENT_NAME.
                      Existing primary: a different name creates an
                      additional persona (equivalent to --persona <name>).
  --display-name <n>  The agent's user-facing display name on Society AI
                      (e.g. "Kilo"). Written to the env file as
                      DISPLAY_NAME; shown in session banners and accepted
                      by switch_agent alongside the canonical name.
  --yes, -y           Non-interactive: never prompt, install + start the
                      bridge service. Requires a token via --token or env.
  --persona <name>    Set up an additional persona explicitly.
  --url <url>         Society AI backend base URL (http:// or https://).
                      Only needed for a non-production Society AI
                      environment; written into the env file as
                      AGENT_ROUTER_API_URL. Default: $PROD_API_URL
  -h, --help          Show this help.

One-command install:
  ./setup.sh --token sai_... --name my-agent --yes
EOF
}

# ── Flag parsing ───────────────────────────────────────────────────────────
PERSONA=""
CLI_TOKEN=""
CLI_NAME=""
CLI_DISPLAY_NAME=""
CLI_URL=""
ASSUME_YES=0

need_value() {
    if [ "$2" -lt 2 ]; then
        echo "Error: $1 requires a value" >&2
        exit 1
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --persona)   need_value "$1" $#; PERSONA="$2"; shift 2 ;;
        --persona=*) PERSONA="${1#*=}"; shift ;;
        --token)     need_value "$1" $#; CLI_TOKEN="$2"; shift 2 ;;
        --token=*)   CLI_TOKEN="${1#*=}"; shift ;;
        --name)      need_value "$1" $#; CLI_NAME="$2"; shift 2 ;;
        --name=*)    CLI_NAME="${1#*=}"; shift ;;
        --display-name)   need_value "$1" $#; CLI_DISPLAY_NAME="$2"; shift 2 ;;
        --display-name=*) CLI_DISPLAY_NAME="${1#*=}"; shift ;;
        --url)       need_value "$1" $#; CLI_URL="$2"; shift 2 ;;
        --url=*)     CLI_URL="${1#*=}"; shift ;;
        --yes|-y)    ASSUME_YES=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)
            echo "Error: unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

# Same shape the WS hub accepts for agent names.
valid_name() {
    printf '%s' "$1" | grep -Eq '^[a-z0-9][a-z0-9._-]{0,62}$'
}

trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

if [ -n "$CLI_NAME" ] && ! valid_name "$CLI_NAME"; then
    echo "Error: invalid agent name: '${CLI_NAME}'" >&2
    echo "(lowercase alphanumerics plus - _ . , max 63 chars)" >&2
    exit 1
fi

# Display name is free text but lands in a shell-sourced env file
# (bridge_launcher.sh sources it), so strip quotes/newlines and cap the
# length; the value itself is written double-quoted.
if [ -n "$CLI_DISPLAY_NAME" ]; then
    CLI_DISPLAY_NAME="$(printf '%s' "$CLI_DISPLAY_NAME" | tr -d '"`$\\\n\r' | cut -c1-80)"
    CLI_DISPLAY_NAME="$(trim "$CLI_DISPLAY_NAME")"
fi
if [ -n "$PERSONA" ] && ! valid_name "$PERSONA"; then
    echo "Error: invalid persona name: '${PERSONA}'" >&2
    echo "(lowercase alphanumerics plus - _ . , max 63 chars)" >&2
    exit 1
fi
if [ -n "$PERSONA" ] && [ -n "$CLI_NAME" ] && [ "$PERSONA" != "$CLI_NAME" ]; then
    echo "Error: --persona '$PERSONA' and --name '$CLI_NAME' conflict." >&2
    echo "Pass just one of them (they mean the same identity)." >&2
    exit 1
fi

CLI_URL="$(trim "$CLI_URL")"
if [ -n "$CLI_URL" ]; then
    case "$CLI_URL" in
        http://*|https://*) ;;
        *)
            echo "Error: --url must be a web address that starts with http:// or https://" >&2
            echo "Got: '$CLI_URL'" >&2
            echo "Example: --url https://staging.societyai.com" >&2
            exit 1
            ;;
    esac
fi

# Token precedence: --token flag > $SOCIETY_AI_AUTH_TOKEN env.
TOKEN="$(trim "${CLI_TOKEN:-${SOCIETY_AI_AUTH_TOKEN:-}}")"

# ── Never-clobber guard ────────────────────────────────────────────────────
# A .env with no AGENT_NAME line means this folder holds a setup this script
# does not recognize (older layout, hand-edited file, or a different tool's
# .env). Proceeding could overwrite a live agent's identity and token, so
# stop before touching anything. An explicit --persona run is still fine:
# it only ever writes .env.<persona>, never .env.
if [ -z "$PERSONA" ] && [ -f ".env" ] && ! grep -qE '^AGENT_NAME=' .env; then
    echo "This folder already contains a Society AI setup that this script" >&2
    echo "does not recognize. Nothing was changed." >&2
    echo "" >&2
    echo "To add another agent on this machine, run again with --persona <name>." >&2
    echo "Or run the setup command from a fresh folder to start a new install." >&2
    exit 1
fi

# Before overwriting an EXISTING env file, keep a timestamped copy so the
# previous identity and token are always recoverable.
backup_env_file() {
    local file="$1"
    [ -f "$file" ] || return 0
    local backup
    backup="$file.bak-$(date +%s)"
    cp "$file" "$backup"
    chmod 600 "$backup"
    echo "  Kept a backup of the previous $file at $backup"
}

# In-place reconfigure of an existing env file: replaces only the API key
# and/or AGENT_ROUTER_API_URL lines, leaving every other setting (AGENT_NAME,
# WORK_DIR, EXTRA_DIRS, ...) exactly as it was.
update_env_file() {
    python3 - "$1" "$2" "$3" "${4:-}" <<'PYEOF'
import os, re, sys
path, token, url = sys.argv[1], sys.argv[2], sys.argv[3]
display = sys.argv[4] if len(sys.argv) > 4 else ""
with open(path) as f:
    content = f.read()

def set_line(text, key, value):
    line = f"{key}={value}"
    if re.search(rf"^{key}=", text, re.M):
        return re.sub(rf"^{key}=.*$", lambda m: line, text, count=1, flags=re.M)
    return text.rstrip("\n") + "\n" + line + "\n"

if token:
    content = set_line(content, "SOCIETY_AI_AUTH_TOKEN", token)
    print(f"  Updated the API key in {path}")
if url:
    content = set_line(content, "AGENT_ROUTER_API_URL", url)
    print(f"  Set AGENT_ROUTER_API_URL={url} in {path}")
if display:
    content = set_line(content, "DISPLAY_NAME", f'"{display}"')
    print(f"  Set DISPLAY_NAME={display} in {path}")
with open(path, "w") as f:
    f.write(content)
try:
    os.chmod(path, 0o600)
except OSError:
    pass
PYEOF
}

# Read AGENT_ROUTER_API_URL out of ONE specific env file. Only ever called
# with the file that belongs to the identity being set up: a persona reads
# its own .env.<persona> and never the primary .env, so a persona's target
# environment can never drift with the primary's.
env_file_api_url() {
    local file="$1" value=""
    if [ -f "$file" ]; then
        value="$(grep -E '^AGENT_ROUTER_API_URL=' "$file" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    fi
    trim "$value"
}

# ── Bridge connection verification (shared by primary + persona) ───────────
# Defined up here (no side effects) so the persona path, which exits before
# the primary path's step 7, can run the same check.

# Watch the bridge logs for the connection verdict. Reads ONLY bytes
# written after the offsets captured before the service (re)started, so
# lines left over from a previous run can never produce a false verdict.
# Prints exactly one of: connected | auth-failed | timeout
# Reads globals: BRIDGE_OUT_LOG, BRIDGE_ERR_LOG, OUT_OFFSET, ERR_OFFSET.
verify_bridge_connected() {
    local waited=0 new_lines
    while :; do
        new_lines="$( { tail -c +"$((OUT_OFFSET + 1))" "$BRIDGE_OUT_LOG" 2>/dev/null
                        tail -c +"$((ERR_OFFSET + 1))" "$BRIDGE_ERR_LOG" 2>/dev/null; } || true )"
        # Auth rejection is definitive: the bridge logs it and exits.
        # (Emitted by bridge.py's exchange_api_key_for_jwt on HTTP 401/403.)
        if printf '%s\n' "$new_lines" | grep -q "Auth failed exchanging API key"; then
            echo "auth-failed"
            return 0
        fi
        # Definitive success: the hub accepted this agent's registration.
        # (bridge.py logs 'Registered as <agent_id>' on the ack.)
        if printf '%s\n' "$new_lines" | grep -q "Registered as "; then
            echo "connected"
            return 0
        fi
        if [ "$waited" -ge 60 ]; then
            echo "timeout"
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
}

# Snapshot the byte sizes of the two bridge logs BEFORE the service starts,
# so verify_bridge_connected only trusts lines written by this run.
# $1 = stdout log, $2 = stderr log. Sets the four globals above.
snapshot_bridge_logs() {
    BRIDGE_OUT_LOG="$1"
    BRIDGE_ERR_LOG="$2"
    OUT_OFFSET=$({ wc -c < "$BRIDGE_OUT_LOG"; } 2>/dev/null || echo 0)
    ERR_OFFSET=$({ wc -c < "$BRIDGE_ERR_LOG"; } 2>/dev/null || echo 0)
    OUT_OFFSET=$((OUT_OFFSET + 0))
    ERR_OFFSET=$((ERR_OFFSET + 0))
}

# The plain-language "your key was not accepted" explanation, shared by the
# primary and persona paths. $1 = the API URL this install tried,
# $2 = path to the error log.
print_auth_failed_help() {
    echo "  Your connection key was not accepted."
    echo ""
    echo "  This usually happens for one of two reasons:"
    echo "    1. The key was already used or has since been regenerated,"
    echo "       so it is no longer valid."
    echo "    2. The key belongs to a different Society AI environment"
    echo "       than the one this setup connects to."
    echo ""
    echo "  It tried to connect to $1."
    echo ""
    echo "  To fix it: go back to your browser, open your agent's page,"
    echo "  click Regenerate token, and run the NEW command it gives you."
    echo ""
    echo "  Details are in the log: $2"
}

# ── --name → persona mapping ───────────────────────────────────────────────
# Identities on one machine: the FIRST is the primary persona (.env); every
# further named identity is an additional persona (.env.<name>). So --name
# maps onto the primary on a fresh machine, and onto the persona flow when a
# primary already exists under a different name. Re-running with the
# primary's own name stays in the primary flow (idempotent).
if [ -z "$PERSONA" ] && [ -n "$CLI_NAME" ] && [ -f ".env" ]; then
    EXISTING_NAME="$(grep -E '^AGENT_NAME=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
    if [ "$EXISTING_NAME" != "$CLI_NAME" ]; then
        PERSONA="$CLI_NAME"
    fi
fi

# ── Additional-persona mode ────────────────────────────────────────────────
if [ -n "$PERSONA" ]; then
    if [ ! -f ".env" ] || [ ! -x "venv/bin/python" ]; then
        echo "Error: set up the default persona first. Additional personas" >&2
        echo "reuse its venv, MCP registration, hooks, and CLAUDE.md. Run:" >&2
        echo "  ./setup.sh                      (interactive)" >&2
        echo "  ./setup.sh --token sai_... --yes  (non-interactive)" >&2
        exit 1
    fi

    ENV_FILE=".env.$PERSONA"

    # Which Society AI environment this persona talks to. --url wins;
    # otherwise the persona's OWN env file if it already has a line; else
    # production. The primary .env is deliberately never consulted: a
    # persona set up with a production key must not silently inherit a
    # local or staging URL from whatever the primary happens to use.
    PERSONA_API_URL="$CLI_URL"
    if [ -z "$PERSONA_API_URL" ]; then
        PERSONA_API_URL="$(env_file_api_url "$ENV_FILE")"
    fi
    if [ -z "$PERSONA_API_URL" ]; then
        PERSONA_API_URL="$PROD_API_URL"
    fi
    echo ""
    echo "  Setting up persona '$PERSONA'."
    echo "  Environment: $PERSONA_API_URL"
    echo ""

    if [ -f "$ENV_FILE" ]; then
        if [ -n "$CLI_TOKEN" ] || [ -n "$CLI_URL" ]; then
            # The re-run explicitly provides new settings for this same
            # persona: update the existing file in place, backup first.
            if [ -n "$CLI_TOKEN" ]; then
                case "$TOKEN" in
                    sai_*) ;;
                    *)
                        echo "  Error: API key must start with 'sai_'." >&2
                        exit 1
                        ;;
                esac
            fi
            backup_env_file "$ENV_FILE"
            # Always write the resolved URL, so the file states its target
            # explicitly instead of relying on an implicit default.
            update_env_file "$ENV_FILE" "${CLI_TOKEN:+$TOKEN}" "$PERSONA_API_URL" "$CLI_DISPLAY_NAME"
        else
            echo "  $ENV_FILE already exists. Leaving it alone."
            echo "  To reconfigure, delete it and re-run."
        fi
    else
        API_KEY="$TOKEN"
        if [ -z "$API_KEY" ]; then
            if [ "$ASSUME_YES" = 1 ]; then
                echo "Error: --yes given but no API key available." >&2
                echo "Pass --token sai_... (or export SOCIETY_AI_AUTH_TOKEN)." >&2
                echo "Create the agent + key at: https://societyai.com" >&2
                exit 1
            fi
            echo ""
            echo "  Persona '$PERSONA' needs its own Society AI API key."
            echo "  Create the agent + key at: https://societyai.com"
            echo ""
            read -rp "  Enter the API key for $PERSONA (sai_...): " API_KEY || true
            API_KEY="$(trim "$API_KEY")"
        fi
        case "$API_KEY" in
            sai_*) ;;
            *)
                echo "  Error: API key must start with 'sai_'." >&2
                exit 1
                ;;
        esac

        # API URL is PERSONA_API_URL, resolved above from --url or the
        # production default. Always written, never inherited from .env.
        umask 077
        {
            echo "# Persona '$PERSONA' - created by setup.sh"
            echo "SOCIETY_AI_AUTH_TOKEN=$API_KEY"
            echo "AGENT_NAME=$PERSONA"
            [ -n "$CLI_DISPLAY_NAME" ] && echo "DISPLAY_NAME=\"$CLI_DISPLAY_NAME\""
            echo "AGENT_ROUTER_API_URL=$PERSONA_API_URL"
            echo "SOCIETY_AI_BRIDGE_SOCKET=$HOME/.cache/society-ai/$PERSONA/bridge.sock"
        } > "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        echo "  Wrote $ENV_FILE (mode 0600)"
        echo "  AGENT_ROUTER_API_URL set to $PERSONA_API_URL"
    fi

    mkdir -p "$HOME/.cache/society-ai/$PERSONA"

    print_persona_summary() {
        echo "  - Its bridge runs as LaunchAgent io.societyai.claude-code-bridge.$PERSONA"
        echo "  - Sessions it spawns authenticate as '$PERSONA' (env expansion in"
        echo "    the shared MCP registration; interactive terminal sessions still"
        echo "    default to the primary persona)."
        echo "  - Optional: edit $ENV_FILE to set WORK_DIR / EXTRA_DIRS for this"
        echo "    persona's file scope, then: ./service.sh restart $PERSONA"
    }

    if [ "$ASSUME_YES" = 1 ] && [ "$(uname)" = "Darwin" ]; then
        snapshot_bridge_logs \
            "$HOME/.cache/society-ai/$PERSONA/bridge.log" \
            "$HOME/.cache/society-ai/$PERSONA/bridge.err.log"
        ./service.sh install "$PERSONA"
        echo ""
        echo "  Checking that persona '$PERSONA' can connect to Society AI..."
        echo "  (this can take up to a minute)"
        VERDICT="$(verify_bridge_connected)"
        if [ "$VERDICT" = "connected" ]; then
            echo ""
            echo "========================================"
            echo "  Done!"
            echo "========================================"
            echo ""
            echo "  Persona '$PERSONA' is now running in the background, and it"
            echo "  will start again automatically every time you log in to"
            echo "  this computer."
            echo ""
            echo "  Environment: $PERSONA_API_URL"
            echo ""
            echo "  You can close this window."
            echo ""
            echo "  Go back to your browser: the Agent Factory page will show"
            echo "  '$PERSONA' as connected in a few seconds."
            echo ""
            print_persona_summary
            echo ""
            echo "  Logs live in:        $BRIDGE_OUT_LOG"
            echo "  To stop this agent:  ./service.sh stop $PERSONA   (run from this folder)"
            echo "  All agents on this computer:  ./status.sh"
            echo ""
            echo "========================================"
        elif [ "$VERDICT" = "auth-failed" ]; then
            echo ""
            echo "========================================"
            echo "  Connection failed"
            echo "========================================"
            echo ""
            echo "  Persona '$PERSONA' could not sign in to Society AI."
            echo ""
            print_auth_failed_help "$PERSONA_API_URL" "$BRIDGE_ERR_LOG"
            echo ""
            echo "  If that key belongs to a different environment, re-run this"
            echo "  setup with --url <that environment's address>."
            echo ""
            exit 1
        else
            echo ""
            echo "========================================"
            echo "  Almost done"
            echo "========================================"
            echo ""
            echo "  Persona '$PERSONA' is set up, but its connection to Society AI"
            echo "  could not be confirmed within a minute."
            echo ""
            echo "  Environment: $PERSONA_API_URL"
            echo ""
            echo "  It may still connect on its own. Your agent's page in the"
            echo "  browser will show it as connected once it does."
            echo ""
            echo "  To watch what the agent is doing, run:"
            echo "      tail -f $BRIDGE_ERR_LOG"
            echo ""
            echo "  Re-running the same setup command is always safe."
            echo ""
            print_persona_summary
            echo ""
        fi
    else
        ./service.sh install "$PERSONA"
        echo ""
        echo "Persona '$PERSONA' is set up."
        echo "  - Environment: $PERSONA_API_URL"
        print_persona_summary
    fi
    exit 0
fi

# Per-host default agent name, so two users on the same org don't collide
# on the WS hub (which rejects duplicate connections). Used by the intro
# below and by step 3 when no --name was given.
derive_host_agent_name() {
    local host_short user_short
    host_short="$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/^-*//;s/-*$//' || true)"
    user_short="$(id -un 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/^-*//;s/-*$//' || true)"
    if [ -n "$user_short" ] && [ -n "$host_short" ]; then
        printf 'claude-code-%s-%s' "$user_short" "$host_short"
    elif [ -n "$host_short" ]; then
        printf 'claude-code-%s' "$host_short"
    else
        printf 'claude-code'
    fi
}

# The name this setup will connect as (for the intro and the final block):
# --name wins; otherwise an existing .env's AGENT_NAME; otherwise the
# per-host default.
AGENT_DISPLAY_NAME="$CLI_NAME"
if [ -z "$AGENT_DISPLAY_NAME" ] && [ -f ".env" ]; then
    AGENT_DISPLAY_NAME="$(grep -E '^AGENT_NAME=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
fi
if [ -z "$AGENT_DISPLAY_NAME" ]; then
    AGENT_DISPLAY_NAME="$(derive_host_agent_name)"
fi

# Which Society AI environment the primary persona talks to. --url wins;
# otherwise this folder's own .env when it already names one; else
# production. Resolved once here so the intro can show it and step 3 can
# write it out explicitly.
API_URL_RESOLVED="$CLI_URL"
if [ -z "$API_URL_RESOLVED" ]; then
    API_URL_RESOLVED="$(env_file_api_url .env)"
fi
if [ -z "$API_URL_RESOLVED" ]; then
    API_URL_RESOLVED="$PROD_API_URL"
fi

echo "========================================"
echo "  Society AI + Claude Code Setup"
echo "========================================"
echo ""

if [ "$ASSUME_YES" = 1 ]; then
    echo "  Connecting your Claude Code to Society AI as agent '$AGENT_DISPLAY_NAME'."
    echo "  Environment: $API_URL_RESOLVED"
    echo "  This takes about 2 minutes. It is safe to leave this window open"
    echo "  until it says Done."
    echo ""
fi

# 1. Check prerequisites
echo "[1/7] Checking that Python and Claude Code are installed..."

if ! command -v python3 &>/dev/null; then
    echo "" >&2
    echo "  Python 3 is not installed. Python is a free tool this agent" >&2
    echo "  needs in order to run." >&2
    echo "" >&2
    echo "  On a Mac, install it by running this command and following the" >&2
    echo "  popup window:" >&2
    echo "" >&2
    echo "      xcode-select --install" >&2
    echo "" >&2
    echo "  (Or download it from https://python.org for any system.)" >&2
    echo "" >&2
    echo "  When the install finishes, run this setup command again." >&2
    exit 1
fi

if ! command -v claude &>/dev/null; then
    echo "" >&2
    echo "  Claude Code is not installed. Claude Code is Anthropic's AI" >&2
    echo "  coding assistant that will power your agent. Install it first:" >&2
    echo "" >&2
    echo "      npm install -g @anthropic-ai/claude-code" >&2
    echo "" >&2
    echo "  (More ways to install: https://claude.com/claude-code)" >&2
    echo "" >&2
    echo "  Then run this setup command again." >&2
    exit 1
fi

echo "  Python ....... OK"
echo "  Claude Code .. OK"

# 2. Create virtual environment
echo ""
echo "[2/7] Setting up a private workspace for your agent..."
echo "      (a Python virtual environment in venv/, plus dependencies)"
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "  Created venv/"
else
    echo "  venv/ already exists"
fi
# shellcheck source=/dev/null
source venv/bin/activate
pip install -q -r requirements.txt
echo "  Dependencies installed"

# Channel server deps (Node) — only needed for SESSION_MODE. Best-effort:
# the bridge runs fine without them in the default spawn mode.
if [ -d "channel" ] && command -v npm &>/dev/null; then
    if [ ! -d "channel/node_modules" ]; then
        (cd channel && npm install --silent >/dev/null 2>&1) \
            && echo "  Channel server deps installed (Node)" \
            || echo "  Note: channel deps not installed (SESSION_MODE only)"
    fi
fi

# 3. Get API key + agent name
echo ""
echo "[3/7] Saving your connection key..."
echo "      (your Society AI API key, stored in the .env file in this folder)"
if [ -f ".env" ]; then
    if [ -n "$CLI_TOKEN" ] || [ -n "$CLI_URL" ]; then
        # Same identity, explicitly new settings: update the existing .env
        # in place. A timestamped backup is kept first, and AGENT_NAME plus
        # every other setting in the file stay untouched.
        if [ -n "$CLI_TOKEN" ]; then
            case "$TOKEN" in
                sai_*) ;;
                *)
                    echo "  Error: API key must start with 'sai_'. Got: ${TOKEN:0:8}..." >&2
                    echo "  Get your key at https://societyai.com" >&2
                    exit 1
                    ;;
            esac
        fi
        backup_env_file ".env"
        # Always write the resolved URL, so .env states its target
        # explicitly instead of relying on an implicit default. Without
        # --url this is whatever the file already said (a no-op), or the
        # production default when it said nothing.
        update_env_file ".env" "${CLI_TOKEN:+$TOKEN}" "$API_URL_RESOLVED" "$CLI_DISPLAY_NAME"
    else
        echo "  A connection key is already saved (.env exists). Leaving it alone."
        echo "  To reconfigure, delete .env and re-run ./setup.sh"
    fi
else
    API_KEY="$TOKEN"
    if [ -z "$API_KEY" ]; then
        if [ "$ASSUME_YES" = 1 ]; then
            echo "  Error: --yes given but no API key available." >&2
            echo "  Pass --token sai_... (or export SOCIETY_AI_AUTH_TOKEN)." >&2
            echo "  Get your key at https://societyai.com" >&2
            exit 1
        fi
        echo ""
        echo "  You need a Society AI API key to connect."
        echo "  Get one at: https://societyai.com"
        echo ""
        # Read the key (suppress -r warning if not a real terminal)
        read -rp "  Enter your API key (sai_...): " API_KEY || true
        API_KEY="$(trim "$API_KEY")"
    fi

    if [ -z "$API_KEY" ]; then
        echo "  Error: API key is required to continue. Re-run ./setup.sh and paste your sai_... key." >&2
        exit 1
    fi
    case "$API_KEY" in
        sai_*) ;;
        *)
            echo "  Error: API key must start with 'sai_'. Got: ${API_KEY:0:8}..." >&2
            echo "  Get your key at https://societyai.com" >&2
            exit 1
            ;;
    esac

    # Agent name: --name wins; otherwise default AGENT_NAME to the per-host
    # value derived above.
    if [ -n "$CLI_NAME" ]; then
        DEFAULT_AGENT_NAME="$CLI_NAME"
    else
        DEFAULT_AGENT_NAME="$(derive_host_agent_name)"
    fi

    # Write .env safely via Python to avoid sed/shell injection.
    # The URL passed here is always the resolved one (--url or the
    # production default), never empty: a fresh .env states its target.
    python3 - "$API_KEY" "$DEFAULT_AGENT_NAME" "$API_URL_RESOLVED" "$CLI_DISPLAY_NAME" <<'PYEOF'
import os, sys, shutil, re
key, agent_name, url = sys.argv[1], sys.argv[2], sys.argv[3]
display = sys.argv[4] if len(sys.argv) > 4 else ""
shutil.copy(".env.example", ".env")
with open(".env") as f:
    content = f.read()
content = content.replace("sai_your_api_key_here", key)
# Inject AGENT_NAME default if user didn't already uncomment it
if not re.search(r"^AGENT_NAME=", content, re.M):
    content = re.sub(r"# AGENT_NAME=claude-code", f"AGENT_NAME={agent_name}", content)
# The agent's user-facing display name (banners, switch_agent matching).
if display and not re.search(r"^DISPLAY_NAME=", content, re.M):
    content = re.sub(
        r"^(AGENT_NAME=.*)$",
        lambda m: m.group(1) + f'\nDISPLAY_NAME="{display}"',
        content, count=1, flags=re.M,
    )
# The target Society AI backend, always written explicitly so the file is
# self-describing (production default unless --url said otherwise).
if url:
    line = f"AGENT_ROUTER_API_URL={url}"
    if re.search(r"^AGENT_ROUTER_API_URL=", content, re.M):
        content = re.sub(r"^AGENT_ROUTER_API_URL=.*$", lambda m: line, content, count=1, flags=re.M)
    elif re.search(r"^# AGENT_ROUTER_API_URL=", content, re.M):
        content = re.sub(r"^# AGENT_ROUTER_API_URL=.*$", lambda m: line, content, count=1, flags=re.M)
    else:
        content = content.rstrip("\n") + "\n" + line + "\n"
with open(".env", "w") as f:
    f.write(content)
# .env holds the API key — restrict to owner-only.
try:
    os.chmod(".env", 0o600)
except OSError:
    pass
print(f"  API key saved to .env (mode 0600)")
print(f"  AGENT_NAME set to {agent_name}")
if url:
    print(f"  AGENT_ROUTER_API_URL set to {url}")
PYEOF
fi

# 4. Configure Claude Code MCP server
echo ""
echo "[4/7] Connecting your agent to Claude Code..."
echo "      (registers the Society AI MCP server in Claude Code's settings)"

# Load env vars for the config script (set -a exports them)
set -a
# shellcheck source=/dev/null
source .env 2>/dev/null || true
set +a

# Verify the env we just sourced is usable BEFORE writing settings.json
if [ -z "${SOCIETY_AI_AUTH_TOKEN:-}" ]; then
    echo "  Error: SOCIETY_AI_AUTH_TOKEN is empty after sourcing .env." >&2
    echo "  Open .env and set it, then re-run ./setup.sh" >&2
    exit 1
fi

# Use a dedicated Python script to safely handle paths with special characters
python3 "$REPO_DIR/configure_claude.py"

# 5. Remove the Society AI section from ~/.claude/CLAUDE.md if a previous
# setup installed one. The platform protocol now travels with dispatches
# (first message of each fresh session), so machines stay free of platform
# references and the protocol stays centrally versioned. Only the
# marker-wrapped block this installer wrote is touched; everything else
# in the user's CLAUDE.md is preserved.
echo ""
echo "[5/7] Cleaning up settings from older versions..."
echo "      (removes the legacy Society AI section from ~/.claude/CLAUDE.md)"

python3 - << 'PYEOF'
import os

BEGIN_MARKER = "<!-- BEGIN: society-ai-claude-code-agent -->"
END_MARKER = "<!-- END: society-ai-claude-code-agent -->"

path = os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md")
try:
    with open(path, encoding="utf-8") as f:
        content = f.read()
except FileNotFoundError:
    print("  Nothing to remove (no ~/.claude/CLAUDE.md)")
    raise SystemExit(0)

start = content.find(BEGIN_MARKER)
end = content.find(END_MARKER)
if start == -1 or end == -1:
    print("  Nothing to remove (no installed section found)")
    raise SystemExit(0)

cleaned = (content[:start] + content[end + len(END_MARKER):]).strip()
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(cleaned + "\n" if cleaned else "")
os.replace(tmp, path)
print("  Removed the legacy Society AI section (protocol now arrives with dispatches)")
PYEOF

# 6. Install Society AI hooks (SessionStart + Stop) so Claude Code gets
# ambient awareness of open tasks and inbox without needing to remember
# to ask. Idempotent (replaces in place). Skip on SKIP_HOOKS=1 for users
# who manage ~/.claude/settings.json by hand.
echo ""
echo "[6/7] Giving Claude Code awareness of your Society AI tasks..."
echo "      (installs the SessionStart and Stop hooks)"

if [ "${SKIP_HOOKS:-}" = "1" ]; then
    echo "  Skipped (SKIP_HOOKS=1)"
else
    python3 "$REPO_DIR/configure_hooks.py"
fi

# 7. Start the bridge — automatic in --yes mode, printed as next steps
# otherwise. In --yes mode the service start is followed by a connection
# check: the outro only says "Done!" once the bridge log shows the agent
# actually registered with the Society AI hub.
# (verify_bridge_connected and its helpers are defined near the top, so the
# persona path can run the same check.)

echo ""
if [ "$ASSUME_YES" = 1 ]; then
    echo "[7/7] Starting your agent in the background..."
    echo "      (installs the bridge as a service that runs at login)"
    if [ "$(uname)" = "Darwin" ]; then
        mkdir -p "$HOME/.cache/society-ai"
        snapshot_bridge_logs \
            "$HOME/.cache/society-ai/bridge.log" \
            "$HOME/.cache/society-ai/bridge.err.log"
        ./service.sh install
        echo ""
        echo "  Checking that your agent can connect to Society AI..."
        echo "  (this can take up to a minute)"
        VERDICT="$(verify_bridge_connected)"
        if [ "$VERDICT" = "connected" ]; then
            echo ""
            echo "========================================"
            echo "  Done!"
            echo "========================================"
            echo ""
            echo "  Your agent '$AGENT_DISPLAY_NAME' is now running in the"
            echo "  background, and it will start again automatically every"
            echo "  time you log in to this computer."
            echo ""
            echo "  Environment: $API_URL_RESOLVED"
            echo ""
            echo "  You can close this window."
            echo ""
            echo "  Go back to your browser: the Agent Factory page will show"
            echo "  your agent as connected in a few seconds."
            echo ""
            echo "  Logs live in:        ~/.cache/society-ai/bridge.log"
            echo "  To stop the agent:   ./service.sh stop   (run from this folder)"
            echo "  All agents on this computer:  ./status.sh"
            echo ""
            echo "========================================"
        elif [ "$VERDICT" = "auth-failed" ]; then
            echo ""
            echo "========================================"
            echo "  Connection failed"
            echo "========================================"
            echo ""
            print_auth_failed_help "$API_URL_RESOLVED" "$BRIDGE_ERR_LOG"
            echo ""
            exit 1
        else
            echo ""
            echo "========================================"
            echo "  Almost done"
            echo "========================================"
            echo ""
            echo "  Setup finished, but your agent's connection to Society AI"
            echo "  could not be confirmed within a minute."
            echo ""
            echo "  Environment: $API_URL_RESOLVED"
            echo ""
            echo "  It may still connect on its own. Your agent's page in the"
            echo "  browser will show it as connected once it does."
            echo ""
            echo "  To watch what the agent is doing, run:"
            echo "      tail -f $BRIDGE_ERR_LOG"
            echo ""
            echo "  Re-running the same setup command is always safe."
            echo ""
        fi
    else
        echo "  Background-service install is macOS-only (launchd)."
        echo "  Everything else is configured. Two ways to run the bridge:"
        echo ""
        echo "  1. Foreground (quick test):"
        echo "       source .env && source venv/bin/activate && python bridge.py"
        echo "  2. As a systemd user service (recommended): copy the template"
        echo "     from the README's 'Linux users' section into"
        echo "     ~/.config/systemd/user/claude-code-bridge.service, then run:"
        echo "       systemctl --user enable --now claude-code-bridge"
        echo "  3. Then go to https://societyai.com and chat with your agent."
    fi
else
    echo "[7/7] Setup complete!"
    echo ""
    echo "========================================"
    echo "  Next steps:"
    echo "========================================"
    echo ""
    echo "  1. Start the bridge (keeps Claude Code connected to Society AI):"
    echo ""
    echo "     source .env && source venv/bin/activate && python bridge.py"
    echo ""
    echo "     Or install it as a background service (recommended):"
    echo ""
    echo "     ./service.sh install"
    echo ""
    echo "  2. Chat with your Claude Code agent from societyai.com"
    echo ""
    echo "  3. Or use Society AI tools directly in Claude Code:"
    echo ""
    echo "     claude"
    echo "     > list my tasks"
    echo ""
    echo "========================================"
fi
