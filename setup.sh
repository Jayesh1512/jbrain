#!/usr/bin/env bash
# Interactive installer for JBrain: picks Docker or native, optionally syncs every change to a GitHub repo.
#   ./setup.sh            ask everything
#   ./setup.sh --docker   force Docker      ./setup.sh --native   force native build
set -euo pipefail
cd "$(dirname "$0")"

if [ -t 1 ]; then
  B=$'\033[1m'; D=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; Z=$'\033[0m'
else
  B=; D=; R=; G=; Y=; C=; Z=
fi
ok()   { printf '  %s✔%s %s\n' "$G" "$Z" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$Z" "$*"; }
bad()  { printf '  %s✘%s %s\n' "$R" "$Z" "$*"; }
step() { printf '\n%s▌ %s%s\n' "$B$C" "$*" "$Z"; }
die()  { bad "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

ask() { # ask "question" [default] -> $REPLY
  local a; printf '  %s?%s %s%s ' "$C" "$Z" "$1" "${2:+ ${D}[$2]${Z}}"
  read -r a || a=""; REPLY="${a:-${2:-}}"
}
yesno() { # yesno "question" y|n -> status 0 for yes
  ask "$1" "$2"; case "$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')" in y|yes) return 0 ;; *) return 1 ;; esac
}
secret() { # hidden input -> $REPLY
  local a; printf '  %s?%s %s ' "$C" "$Z" "$1"; read -rs a || a=""; echo; REPLY="$a"
}
run() { # run "message" cmd... : spinner while a command runs, tail of log on failure
  local msg="$1" log pid i=0; shift; log=$(mktemp)
  "$@" >"$log" 2>&1 </dev/null & pid=$!
  if [ -t 1 ]; then
    local f=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
    while kill -0 "$pid" 2>/dev/null; do printf '\r  %s%s%s %s' "$C" "${f[i % 10]}" "$Z" "$msg"; i=$((i + 1)); sleep 0.1; done
    printf '\r\033[K'
  fi
  if wait "$pid"; then ok "$msg"; rm -f "$log"; else bad "$msg failed:"; tail -n 25 "$log" | sed 's/^/      /'; rm -f "$log"; exit 1; fi
}
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
rand_hex()  { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
health()    { curl -fsS -m 3 -H "Authorization: Bearer $JBRAIN_TOKEN" "http://127.0.0.1:$JBRAIN_PORT/health" 2>/dev/null; }

# git with the token as an HTTP header (never in argv or the remote URL)
authgit() {
  local origin; origin=$(printf '%s' "$JBRAIN_GIT_REMOTE" | sed -nE 's#^(https://)([^/@]*@)?([^/]+)/.*#\1\3/#p')
  if [ -n "${JBRAIN_GIT_TOKEN:-}" ] && [ -n "$origin" ]; then
    GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="http.${origin}.extraHeader" \
      GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$JBRAIN_GIT_TOKEN" | base64 | tr -d '\n')" git "$@"
  else
    GIT_TERMINAL_PROMPT=0 git "$@"
  fi
}

printf '\n%s' "$B$C"
cat <<'EOF'
   ┌──────────────────────────────────────────────┐
   │   JBrain setup                               │
   │   shared knowledge for your agents           │
   └──────────────────────────────────────────────┘
EOF
printf '%s' "$Z"

# ── 1. environment ────────────────────────────────────────────────────────────
step "Checking this machine"
MODE=${FORCE_MODE:-}
for arg in "$@"; do case "$arg" in --docker) MODE=docker ;; --native) MODE=native ;; -h|--help) sed -n 2,4p "$0"; exit 0 ;; esac; done

have git || die "git is required (knowledge history). Install git and re-run."
ok "git $(git --version | awk '{print $3}')"

DOCKER_OK=0
if ! have docker; then warn "Docker not found"
elif ! docker info >/dev/null 2>&1; then warn "Docker is installed but its daemon is not running"
elif ! docker compose version >/dev/null 2>&1; then warn "Docker found, but the 'docker compose' plugin is missing"
else DOCKER_OK=1; ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null) with compose"; fi

NATIVE_OK=0
if have cargo && have rustc; then
  minor=$(rustc --version | sed -E 's/^rustc 1\.([0-9]+).*/\1/')
  if [ "${minor:-0}" -ge 89 ] 2>/dev/null; then NATIVE_OK=1; ok "Rust $(rustc --version | awk '{print $2}')"
  else warn "Rust is too old ($(rustc --version | awk '{print $2}')); 1.89+ is needed for a native build"; fi
else warn "Rust toolchain not found"; fi

if [ -z "$MODE" ]; then
  if [ $DOCKER_OK = 1 ] && [ $NATIVE_OK = 1 ]; then
    step "How do you want to run it?"
    printf '    %s1%s Docker  %s(recommended: isolated, restarts on boot)%s\n    %s2%s Native  %s(builds with cargo, runs as a background process)%s\n' "$B" "$Z" "$D" "$Z" "$B" "$Z" "$D" "$Z"
    ask "Choose" 1; [ "$REPLY" = 2 ] && MODE=native || MODE=docker
  elif [ $DOCKER_OK = 1 ]; then MODE=docker; ok "Docker will be used"
  elif [ $NATIVE_OK = 1 ]; then MODE=native; ok "Docker is unavailable, so a native build will be used"
  else die "Neither Docker nor Rust 1.89+ is usable. Install Docker Desktop (https://docs.docker.com/get-docker/) or Rust (https://rustup.rs) and re-run."; fi
elif [ "$MODE" = docker ] && [ $DOCKER_OK = 0 ]; then die "--docker requested but Docker is not usable"
elif [ "$MODE" = native ] && [ $NATIVE_OK = 0 ]; then die "--native requested but Rust 1.89+ is missing"; fi

# ── 2. configuration ──────────────────────────────────────────────────────────
REUSE=0
if [ -f .env ]; then
  step "Existing configuration"
  warn ".env already exists"
  yesno "Keep it and just (re)start JBrain?" y && REUSE=1
fi

if [ $REUSE = 1 ]; then
  set -a; . ./.env; set +a
  : "${JBRAIN_TOKEN:?.env has no JBRAIN_TOKEN}"; JBRAIN_PORT=${JBRAIN_PORT:-7331}
  JBRAIN_GIT_REMOTE=${JBRAIN_GIT_REMOTE:-}; JBRAIN_GIT_TOKEN=${JBRAIN_GIT_TOKEN:-}
else
  step "Basics"
  while :; do
    ask "Port" 7331; JBRAIN_PORT=$REPLY
    case "$JBRAIN_PORT" in ''|*[!0-9]*) bad "Enter a number"; continue ;; esac
    port_busy "$JBRAIN_PORT" && { warn "Port $JBRAIN_PORT is in use (is JBrain already running?)"; continue; }
    break
  done
  JBRAIN_TOKEN=$(rand_hex); ok "Generated an access token (stored in .env)"
  DATA=""
  if [ $MODE = native ]; then ask "Where should the knowledge be stored?" "$PWD/jbrain-data"; DATA=$REPLY; fi

  step "GitHub sync (optional)"
  printf '  %sEvery change is committed to Git. You can also push each one to a GitHub repo as a backup.%s\n' "$D" "$Z"
  JBRAIN_GIT_REMOTE=""; JBRAIN_GIT_TOKEN=""
  if yesno "Push every update to a GitHub repository?" n; then
    ask "Repository (owner/name or full URL)"; r=$REPLY
    [ -n "$r" ] || die "No repository given"
    case "$r" in
      *://*|/*|git@*|./*) JBRAIN_GIT_REMOTE=$r ;;
      */*)                JBRAIN_GIT_REMOTE="https://github.com/${r%.git}.git" ;;
      *)                  die "Use owner/name or a full URL" ;;
    esac
    case "$JBRAIN_GIT_REMOTE" in
      https://*)
        ghuser=""; have gh && ghuser=$(gh api user -q .login 2>/dev/null || true)
        if [ -n "$ghuser" ] && yesno "Use your gh login ($ghuser)? Its token can access ALL your repos" n; then
          JBRAIN_GIT_TOKEN=$(gh auth token)
        else
          printf '  %sBest: a fine-grained token limited to this one repo (Contents: read and write).%s\n' "$D" "$Z"
          secret "Token (hidden; empty = use this machine's saved git credentials)"; JBRAIN_GIT_TOKEN=$REPLY
        fi
        [ $MODE = docker ] && [ -z "$JBRAIN_GIT_TOKEN" ] && warn "A container cannot see your saved credentials; pushes will fail without a token" ;;
      *) [ $MODE = docker ] && warn "ssh and local-path remotes are not reachable from the container; use an https URL and a token" ;;
    esac
    if authgit ls-remote --heads "$JBRAIN_GIT_REMOTE" >/tmp/jbrain-ls.$$ 2>/dev/null; then
      ok "Reached $JBRAIN_GIT_REMOTE"
      if [ -s /tmp/jbrain-ls.$$ ]; then
        warn "That repository already has branches. The first push is rejected unless its main branch already holds this brain's history."
        yesno "Continue with this repository?" n || die "Re-run with an empty repository."
      fi
    else
      bad "Could not read $JBRAIN_GIT_REMOTE (missing repo, or the token cannot see it)"
      slug=$(printf '%s' "$JBRAIN_GIT_REMOTE" | sed -nE 's#^https://github\.com/(.+)\.git$#\1#p')
      if [ -n "$slug" ] && have gh && yesno "Create the private repository $slug with gh?" y; then
        gh repo create "$slug" --private >/dev/null && ok "Created $slug" || die "gh could not create $slug"
      else
        yesno "Keep this remote anyway? (pushes retry in the background until it works)" n || die "Fix the repository and re-run."
      fi
    fi
    rm -f /tmp/jbrain-ls.$$
  else
    ok "Skipping GitHub sync. You can enable it later by editing .env and restarting."
  fi

  ( umask 077
    {
      echo "JBRAIN_TOKEN=$JBRAIN_TOKEN"
      echo "JBRAIN_PORT=$JBRAIN_PORT"
      echo "JBRAIN_URL=http://127.0.0.1:$JBRAIN_PORT"
      [ -n "$DATA" ] && echo "JBRAIN_DATA_DIR='$DATA'"
      echo "JBRAIN_GIT_REMOTE='$JBRAIN_GIT_REMOTE'"
      echo "JBRAIN_GIT_TOKEN='$JBRAIN_GIT_TOKEN'"
    } > .env )
  ok "Saved .env (readable only by you)"
fi

# ── 3. install ────────────────────────────────────────────────────────────────
step "Installing ($MODE)"
if [ $MODE = docker ]; then
  run "Building the image and starting the container" docker compose up -d --build
else
  run "Building JBrain with cargo (first build takes a few minutes)" cargo build --release --locked
  set -a; . ./.env; set +a
  DATA=${JBRAIN_DATA_DIR:-$PWD/jbrain-data}; mkdir -p "$DATA"
  if [ -f "$DATA/jbrain.pid" ] && kill -0 "$(cat "$DATA/jbrain.pid")" 2>/dev/null; then
    kill "$(cat "$DATA/jbrain.pid")"; sleep 1; ok "Stopped the previous instance"
  fi
  JBRAIN_DATA_DIR=$DATA nohup ./target/release/jbrain serve >>"$DATA/jbrain.log" 2>&1 </dev/null &
  echo $! > "$DATA/jbrain.pid"; ok "Started in the background (pid $(cat "$DATA/jbrain.pid"), log $DATA/jbrain.log)"
fi

printf '  %s…%s waiting for the service' "$C" "$Z"
ready=0
for _ in $(seq 1 90); do
  if health | grep -q '"status":"ready"'; then ready=1; break; fi
  sleep 1
done
printf '\r\033[K'
[ $ready = 1 ] || { bad "JBrain did not become ready in 90s."; [ $MODE = docker ] && echo "      docker compose logs --tail 30 jbrain" || echo "      tail $DATA/jbrain.log"; exit 1; }
ok "JBrain is ready at http://127.0.0.1:$JBRAIN_PORT"

# ── 4. prove the sync works ───────────────────────────────────────────────────
if [ -n "${JBRAIN_GIT_REMOTE:-}" ]; then
  step "Checking GitHub sync"
  if yesno "Write a welcome note now to confirm it reaches the repo?" y; then
    body='{"requestId":"setup-'$(rand_hex)'","actor":"setup/1","action":"put","id":"welcome","expectedRevision":null,"markdown":"---\ntype: Note\ntitle: Welcome\n---\n# Welcome\n\nJBrain is running and syncing.\n"}'
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 -H "Authorization: Bearer $JBRAIN_TOKEN" -H 'Content-Type: application/json' -d "$body" "http://127.0.0.1:$JBRAIN_PORT/v1/operations" || true)
    if [ "$code" = 200 ] || [ "$code" = 409 ]; then
      synced=0
      for _ in $(seq 1 30); do
        h=$(health || true)
        if printf '%s' "$h" | grep -q '"pushedCommit":"' && printf '%s' "$h" | grep -q '"lastError":null'; then synced=1; break; fi
        sleep 1
      done
      if [ $synced = 1 ]; then ok "Pushed to $JBRAIN_GIT_REMOTE"
      else bad "Not pushed yet. Last status:"; printf '%s\n' "$(health | sed -n 's/.*\("sync":{[^}]*}\).*/\1/p')" | sed 's/^/      /'
           warn "JBrain keeps retrying in the background; writes are never blocked by it."; fi
    else bad "The test write returned HTTP $code"; fi
  fi
fi

# ── 5. done ───────────────────────────────────────────────────────────────────
step "Connect your agents"
URL="http://127.0.0.1:$JBRAIN_PORT"
printf '  Claude Code:\n    %sclaude mcp add --transport http jbrain %s/mcp --header "Authorization: Bearer %s"%s\n' "$B" "$URL" "$JBRAIN_TOKEN" "$Z"
printf '  Other MCP clients: URL %s%s/mcp%s with header %sAuthorization: Bearer <JBRAIN_TOKEN from .env>%s\n' "$B" "$URL" "$Z" "$B" "$Z"
printf '  CLI:  %sset -a; . ./.env; set +a; ./target/release/jbrain list%s  %s(native build only)%s\n' "$B" "$Z" "$D" "$Z"
step "Manage"
if [ $MODE = docker ]; then
  printf '  logs  %sdocker compose logs -f jbrain%s\n  stop  %sdocker compose down%s   %s(data lives in the jbrain-data volume)%s\n' "$B" "$Z" "$B" "$Z" "$D" "$Z"
else
  printf '  logs  %stail -f %s/jbrain.log%s\n  stop  %skill $(cat %s/jbrain.pid)%s   %s(does not restart on reboot)%s\n' "$B" "$DATA" "$Z" "$B" "$DATA" "$Z" "$D" "$Z"
fi
printf '\n  %sDone.%s Your token is in %s.env%s, keep that file private.\n\n' "$G$B" "$Z" "$B" "$Z"
