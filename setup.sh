#!/usr/bin/env bash
# Interactive installer for JBrain: picks Docker or native, optionally syncs every change to a GitHub repo.
#   ./setup.sh            ask everything
#   ./setup.sh --docker   force Docker      ./setup.sh --native   force native build
set -euo pipefail
cd "$(dirname "$0")"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; D=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; Z=$'\033[0m'; FANCY=1
else
  B=; D=; R=; G=; Y=; C=; Z=; FANCY=0
fi
TTY=0; [ -t 0 ] && [ -t 1 ] && TTY=1
trap 'printf "\033[?25h"' EXIT          # always give the cursor back
trap 'printf "\n"; bad "Cancelled."; exit 130' INT

ok()   { printf '  %s✔%s %s\n' "$G" "$Z" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$Z" "$*"; }
bad()  { printf '  %s✘%s %s\n' "$R" "$Z" "$*"; }
die()  { bad "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
rgb()  { [ $FANCY = 1 ] && printf '\033[38;5;%sm' "$1" || true; }

TOTAL=4
phase() { # phase N "title" : progress bar + heading
  local n=$1 i bar=""
  for ((i = 1; i <= TOTAL; i++)); do [ $i -le "$n" ] && bar="$bar$(rgb 45)━━$Z" || bar="$bar${D}──$Z"; done
  printf '\n  %s %s%s%d/%d%s  %s%s%s\n' "$bar" "$D" "$B" "$n" "$TOTAL" "$Z" "$B" "$2" "$Z"
}

ask() { # ask "question" [default] -> $REPLY
  local a; printf '  %s❯%s %s%s ' "$C" "$Z" "$1" "${2:+ ${D}($2)${Z}}"
  read -r a || a=""; REPLY="${a:-${2:-}}"
}
yesno() { # yesno "question" y|n -> status 0 for yes
  local a hint="y/N"; [ "$2" = y ] && hint="Y/n"
  printf '  %s❯%s %s %s(%s)%s ' "$C" "$Z" "$1" "$D" "$hint" "$Z"; read -r a || a=""
  case "$(printf '%s' "${a:-$2}" | tr '[:upper:]' '[:lower:]')" in y|yes) return 0 ;; *) return 1 ;; esac
}
secret() { # hidden input -> $REPLY
  local a; printf '  %s❯%s %s ' "$C" "$Z" "$1"; read -rs a || a=""; echo; REPLY="$a"
}
choose() { # choose opt1 opt2 ... -> $CHOICE (0-based). Arrow keys / j k / digits on a terminal, a number otherwise.
  local n=$# i sel=0 key; local -a opts=("$@")
  if [ $TTY = 0 ]; then
    for ((i = 0; i < n; i++)); do printf '    %s%d%s %s\n' "$B" $((i + 1)) "$Z" "${opts[i]}"; done
    ask "Choose" 1; CHOICE=$((${REPLY:-1} - 1)); { [ "$CHOICE" -ge 0 ] && [ "$CHOICE" -lt "$n" ]; } 2>/dev/null || CHOICE=0
    return
  fi
  draw() {
    for ((i = 0; i < n; i++)); do
      if [ $i = $sel ]; then printf '\033[2K  %s❯ %s%s\n' "$B$(rgb 45)" "${opts[i]}" "$Z"; else printf '\033[2K    %s%s%s\n' "$D" "${opts[i]}" "$Z"; fi
    done
  }
  printf '\033[?25l'; printf '  %s↑/↓ to move, Enter to select%s\n' "$D" "$Z"; draw
  while :; do
    IFS= read -rsn1 key || key=""
    case "$key" in
      $'\033') read -rsn2 -t 1 key || key=""; case "$key" in '[A') sel=$(((sel + n - 1) % n)) ;; '[B') sel=$(((sel + 1) % n)) ;; esac ;;
      k) sel=$(((sel + n - 1) % n)) ;;
      j) sel=$(((sel + 1) % n)) ;;
      [1-9]) [ "$key" -le "$n" ] && sel=$((key - 1)) ;;
      "") break ;;
    esac
    printf '\033[%dA' "$n"; draw
  done
  printf '\033[?25h'; CHOICE=$sel
}
run() { # run "message" cmd... : spinner + elapsed time, tail of the log on failure
  local msg="$1" log pid i=0 t0=$SECONDS; shift; log=$(mktemp)
  "$@" >"$log" 2>&1 </dev/null & pid=$!
  if [ $TTY = 1 ]; then
    local -a f=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏); printf '\033[?25l'
    while kill -0 "$pid" 2>/dev/null; do
      printf '\r  %s%s%s %s %s%ds%s' "$(rgb $((39 + i % 10)))" "${f[i % 10]}" "$Z" "$msg" "$D" $((SECONDS - t0)) "$Z"; i=$((i + 1)); sleep 0.1
    done
    printf '\r\033[K\033[?25h'
  fi
  if wait "$pid"; then ok "$msg $D($((SECONDS - t0))s)$Z"; rm -f "$log"; else bad "$msg failed:"; tail -n 25 "$log" | sed 's/^/      /'; rm -f "$log"; exit 1; fi
}
row() { printf '  %s %s%-9s%s %s\n' "$1" "$D" "$2" "$Z" "$3"; }   # row <icon> <label> <text>
card() { # card line... : rounded box around plain-text lines
  local w=0 l; for l in "$@"; do [ ${#l} -gt $w ] && w=${#l}; done
  printf '\n  %s╭' "$(rgb 45)"; printf '─%.0s' $(seq 1 $((w + 2))); printf '╮%s\n' "$Z"
  for l in "$@"; do printf '  %s│%s %-*s %s│%s\n' "$(rgb 45)" "$Z" "$w" "$l" "$(rgb 45)" "$Z"; done
  printf '  %s╰' "$(rgb 45)"; printf '─%.0s' $(seq 1 $((w + 2))); printf '╯%s\n' "$Z"
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

banner() {
  local -a art=(
    '     ██╗██████╗ ██████╗  █████╗ ██╗███╗   ██╗'
    '     ██║██╔══██╗██╔══██╗██╔══██╗██║████╗  ██║'
    '     ██║██████╔╝██████╔╝███████║██║██╔██╗ ██║'
    '██   ██║██╔══██╗██╔══██╗██╔══██║██║██║╚██╗██║'
    '╚█████╔╝██████╔╝██║  ██║██║  ██║██║██║ ╚████║'
    ' ╚════╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝'
  ); local -a col=(27 33 39 45 51 50) i
  printf '\n'
  for i in 0 1 2 3 4 5; do printf '  %s%s%s\n' "$(rgb "${col[i]}")" "${art[i]}" "$Z"; done
  printf '  %s%sshared knowledge for your agents%s  %s·  setup%s\n' "$D" "$B" "$Z" "$D" "$Z"
}

banner

# ── 1. environment ────────────────────────────────────────────────────────────
phase 1 "Checking this machine"
MODE=${FORCE_MODE:-}
for arg in "$@"; do case "$arg" in --docker) MODE=docker ;; --native) MODE=native ;; -h|--help) sed -n 2,4p "$0"; exit 0 ;; esac; done

have git || die "git is required (knowledge history). Install git and re-run."
GITV=$(git --version | awk '{print $3}')
row "${G}✔$Z" git "$GITV"

DOCKER_OK=0
if ! have docker; then row "${Y}–$Z" Docker "not found"
elif ! docker info >/dev/null 2>&1; then row "${Y}–$Z" Docker "installed, but the daemon is not running"
elif ! docker compose version >/dev/null 2>&1; then row "${Y}–$Z" Docker "found, but the 'docker compose' plugin is missing"
else DOCKER_OK=1; row "${G}✔$Z" Docker "$(docker version --format '{{.Server.Version}}' 2>/dev/null) with compose"; fi

NATIVE_OK=0
if have cargo && have rustc; then
  minor=$(rustc --version | sed -E 's/^rustc 1\.([0-9]+).*/\1/')
  if [ "${minor:-0}" -ge 89 ] 2>/dev/null; then NATIVE_OK=1; row "${G}✔$Z" Rust "$(rustc --version | awk '{print $2}')"
  else row "${Y}–$Z" Rust "$(rustc --version | awk '{print $2}') is too old; 1.89+ is needed for a native build"; fi
else row "${Y}–$Z" Rust "not found"; fi

if [ -z "$MODE" ]; then
  if [ $DOCKER_OK = 1 ] && [ $NATIVE_OK = 1 ]; then
    printf '\n  %sHow do you want to run it?%s\n' "$B" "$Z"
    choose "Docker   · isolated, restarts on boot (recommended)" "Native   · cargo build, background process"
    [ "$CHOICE" = 1 ] && MODE=native || MODE=docker
  elif [ $DOCKER_OK = 1 ]; then MODE=docker; printf '\n'; ok "Docker will be used"
  elif [ $NATIVE_OK = 1 ]; then MODE=native; printf '\n'; ok "Docker is unavailable, so a native build will be used"
  else die "Neither Docker nor Rust 1.89+ is usable. Install Docker Desktop (https://docs.docker.com/get-docker/) or Rust (https://rustup.rs) and re-run."; fi
elif [ "$MODE" = docker ] && [ $DOCKER_OK = 0 ]; then die "--docker requested but Docker is not usable"
elif [ "$MODE" = native ] && [ $NATIVE_OK = 0 ]; then die "--native requested but Rust 1.89+ is missing"; fi

# ── 2. configuration ──────────────────────────────────────────────────────────
REUSE=0
if [ -f .env ]; then
  phase 2 "Configure"
  warn ".env already exists"
  yesno "Keep it and just (re)start JBrain?" y && REUSE=1
fi

if [ $REUSE = 1 ]; then
  set -a; . ./.env; set +a
  : "${JBRAIN_TOKEN:?.env has no JBRAIN_TOKEN}"; JBRAIN_PORT=${JBRAIN_PORT:-7331}
  JBRAIN_GIT_REMOTE=${JBRAIN_GIT_REMOTE:-}; JBRAIN_GIT_TOKEN=${JBRAIN_GIT_TOKEN:-}
else
  phase 2 "Configure"
  printf '\n  %sBasics%s\n' "$B" "$Z"
  while :; do
    ask "Port" 7331; JBRAIN_PORT=$REPLY
    case "$JBRAIN_PORT" in ''|*[!0-9]*) bad "Enter a number"; continue ;; esac
    port_busy "$JBRAIN_PORT" && { warn "Port $JBRAIN_PORT is in use (is JBrain already running?)"; continue; }
    break
  done
  JBRAIN_TOKEN=$(rand_hex); ok "Generated an access token (stored in .env)"
  DATA=""
  if [ $MODE = native ]; then ask "Where should the knowledge be stored?" "$PWD/jbrain-data"; DATA=$REPLY; fi

  printf '\n  %sGitHub sync%s %s(optional)%s\n' "$B" "$Z" "$D" "$Z"
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
phase 3 "Install · $MODE"
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
  printf '\n  %sGitHub sync%s\n' "$B" "$Z"
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
phase 4 "Connect your agents"
URL="http://127.0.0.1:$JBRAIN_PORT"
if [ -n "${JBRAIN_GIT_REMOTE:-}" ]; then SYNC="every change -> $(printf '%s' "$JBRAIN_GIT_REMOTE" | sed -E 's#//[^/@]*@#//#')"; else SYNC="off"; fi
if [ $MODE = docker ]; then WHERE="Docker (volume: jbrain-data)"; else WHERE="native, data in $DATA"; fi
card "JBrain is running" "" "URL     $URL" "Runs    $WHERE" "Sync    $SYNC" "Token   saved in .env (keep it private)"
printf '\n  %sClaude Code%s\n    %sclaude mcp add --transport http jbrain %s/mcp --header "Authorization: Bearer %s"%s\n' "$B" "$Z" "$(rgb 45)" "$URL" "$JBRAIN_TOKEN" "$Z"
printf '\n  %sOther MCP clients%s\n    URL %s/mcp   header  Authorization: Bearer <JBRAIN_TOKEN in .env>\n' "$B" "$Z" "$URL"
printf '\n  %sManage%s\n' "$B" "$Z"
if [ $MODE = docker ]; then
  printf '    logs  docker compose logs -f jbrain\n    stop  docker compose down   %s(data stays in the volume)%s\n' "$D" "$Z"
else
  printf '    logs  tail -f %s/jbrain.log\n    stop  kill $(cat %s/jbrain.pid)   %s(does not restart on reboot)%s\n    cli   set -a; . ./.env; set +a; ./target/release/jbrain list\n' "$DATA" "$DATA" "$D" "$Z"
fi
printf '\n'
