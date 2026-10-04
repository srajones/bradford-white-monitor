#!/bin/sh
# bwwatch guided installer: walks you through installing the Bradford White Wave fault watcher and
# checks at every step that it really worked.
#
#   cd /opt/bwheater && ./install.sh              install (or pick up where an earlier run stopped)
#   ./install.sh --reconfigure                    run the guided setup again (alerts, time zone, ...)
#   ./install.sh --check                          is it working? (changes nothing)
#   ./install.sh --uninstall                      remove it (asks before it touches your data)
#   ./install.sh --help
#
# WHERE THINGS GO. Everything this script creates stays inside the folder it lives in (normally
# /opt/bwheater):  .env (your settings), data/ (database, backups, Wave sign-in), install.log, and
# short-lived scratch files named .install.out and .env.new. It writes nothing anywhere else on
# this server: not /etc, not /tmp, not your home folder, no systemd units, no cron jobs, no links in
# /usr/local. The only things outside the folder are Docker's own: the image "bwwatch:local" and the
# container "bwwatch" (Docker keeps both under /var/lib/docker). It never touches any other
# container, image, volume or network, and it never installs software on your behalf.
#
# The program itself (the guided questions, the checks, the watcher) runs inside the Docker
# container; this script only starts it and reports.

set -u
umask 077

case $0 in
  */*) SELF_DIR=${0%/*} ;;
  *)   SELF_DIR=. ;;
esac
DIR=$(cd "$SELF_DIR" 2>/dev/null && pwd -P) || { printf 'install.sh: cannot find the folder it is in\n' >&2; exit 1; }
DIR_LOGICAL=$(cd "$SELF_DIR" 2>/dev/null && pwd)
cd "$DIR" || exit 1

EXPECTED_DIR=/opt/bwheater
PROJECT=bwwatch
CONTAINER=bwwatch
IMAGE=bwwatch:local
LOG=$DIR/install.log
OUT=$DIR/.install.out
NEW_ENV=$DIR/.env.new
STAMP=$(date +%Y%m%d-%H%M%S)

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_OK=$(printf '\033[32m'); C_WARN=$(printf '\033[33m'); C_FAIL=$(printf '\033[31;1m')
  C_BOLD=$(printf '\033[1;36m'); C_OFF=$(printf '\033[0m')
else
  C_OK=''; C_WARN=''; C_FAIL=''; C_BOLD=''; C_OFF=''
fi

# ---------------------------------------------------------------------------------- output
LOGGING=0   # switched on after the folder is confirmed; --check and --help never write the log
log() {
  if [ "$LOGGING" = 1 ] && [ -w "$DIR" ]; then
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG" 2>/dev/null
  fi
  return 0
}
say()  { printf '%s\n' "$*"; }
blank() { printf '\n'; }
hint() { printf '       %s\n' "$*"; }
ok()   { printf '%s[ OK ]%s %s\n' "$C_OK" "$C_OFF" "$*"; log "OK   $*"; }
warn() { printf '%s[WARN]%s %s\n' "$C_WARN" "$C_OFF" "$*"; log "WARN $*"; }
fail() { printf '%s[FAIL]%s %s\n' "$C_FAIL" "$C_OFF" "$*"; log "FAIL $*"; }
heading() {
  printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_OFF"
  printf '%s\n' "$*" | sed 's/./-/g'
  log "== $*"
}
indent() { sed 's/^/         /'; }

usage() {
  cat <<EOF
bwwatch installer

Usage: ./install.sh [option]

  (no option)     install bwwatch, or pick up where an earlier run stopped
  --reconfigure   run the guided setup again (alerts, time zone, fault request, ...)
  --check         check that an installed bwwatch is working; changes nothing
  --uninstall     remove bwwatch (it asks before touching your recorded data)
  --help          show this text

Everything stays inside this folder ($DIR): .env, data/, install.log.
Outside it there is only Docker's own storage: the image "$IMAGE" and the container "$CONTAINER".
EOF
}

# ---------------------------------------------------------------------------------- questions
STOPPED_SERVICE=0
BG_PID=''

cancelled() {
  blank
  say "Stopped. Run ./install.sh again whenever you are ready."
  exit 130
}

# ask_yes_no "Question?" y|n   returns 0 for yes and 1 for no; the end of the input cancels the install
ask_yes_no() {
  _q=$1; _d=$2
  while :; do
    if [ "$_d" = y ]; then _h='Y/n'; else _h='y/N'; fi
    printf '%s [%s] ' "$_q" "$_h"
    if ! IFS= read -r _a; then cancelled; fi
    case $(printf '%s' "$_a" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz') in
      '')    if [ "$_d" = y ]; then return 0; fi; return 1 ;;
      y|yes) return 0 ;;
      n|no)  return 1 ;;
      *)     say "Please answer y or n." ;;
    esac
  done
}

# ---------------------------------------------------------------------------------- docker helpers
# Always the same project, file and folder, whatever the shell's COMPOSE_* variables say.
dc() { docker compose -p "$PROJECT" -f "$DIR/docker-compose.yml" --project-directory "$DIR" "$@"; }

container_exists()  { docker inspect -f '{{.Id}}' "$CONTAINER" >/dev/null 2>&1; }
container_running() { [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = true ]; }
container_owner()   { docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$CONTAINER" 2>/dev/null; }
container_is_ours() {
  _o=$(container_owner)
  [ "$_o" = "$DIR" ] || [ "$_o" = "$DIR_LOGICAL" ]
}

# shellcheck disable=SC2329  # run by the trap below
cleanup() {
  _rc=$?
  if [ -n "$BG_PID" ]; then kill "$BG_PID" 2>/dev/null; fi
  rm -f "$OUT" "$NEW_ENV" 2>/dev/null
  if [ "$STOPPED_SERVICE" = 1 ]; then
    STOPPED_SERVICE=0
    say "Starting bwwatch again (it was paused for the setup)..."
    dc start >/dev/null 2>&1 && say "bwwatch is running again."
  fi
  return "$_rc"
}
trap cleanup EXIT
trap 'blank; say "Interrupted."; exit 130' INT TERM HUP

# ---------------------------------------------------------------------------------- Part 1: this server
check_folder() {
  if [ ! -w "$DIR" ]; then
    fail "You are not allowed to write to this folder: $DIR"
    hint "Either run the installer as root:   sudo ./install.sh"
    hint "or make the folder yours first:     sudo chown -R \"\$USER\" \"$DIR\""
    return 1
  fi
  for _f in docker-compose.yml Dockerfile .env.example bwwatch/__main__.py; do
    if [ ! -e "$DIR/$_f" ]; then
      fail "This folder is incomplete: $_f is missing."
      hint "Get a fresh copy of the project (git clone / git pull) and run ./install.sh from inside it."
      return 1
    fi
  done
  if [ "$DIR" = "$EXPECTED_DIR" ] || [ "$DIR_LOGICAL" = "$EXPECTED_DIR" ]; then
    ok "Installing in $DIR; everything stays inside this folder."
  else
    warn "You planned $EXPECTED_DIR, but this folder is $DIR."
    hint "Everything will stay inside $DIR (nothing is written anywhere else)."
    if ! ask_yes_no "Install here anyway?" n; then cancelled; fi
  fi
  return 0
}

# Docker's own install page for this distribution (a read of /etc/os-release, nothing more).
docker_docs() {
  # shellcheck source=/dev/null
  _id=$( (. /etc/os-release 2>/dev/null; printf '%s' "${ID:-}") 2>/dev/null)
  case $_id in
    debian|ubuntu|fedora|raspbian) printf 'https://docs.docker.com/engine/install/%s/' "$_id" ;;
    *) printf 'https://docs.docker.com/engine/install/' ;;
  esac
}

check_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    fail "Docker is not installed (the 'docker' command was not found)."
    hint "Install Docker Engine together with the Compose plugin, following Docker's own steps for your system:"
    hint "    $(docker_docs)"
    hint "(This installer never installs software for you.)"
    return 1
  fi
  _out=$(docker info 2>&1); _rc=$?
  if [ "$_rc" -ne 0 ]; then
    case $_out in
      *ermission\ denied*)
        fail "Docker is installed, but you are not allowed to use it."
        hint "Run the installer as root:   sudo ./install.sh"
        hint "(Or add your user to the 'docker' group and log in again; that gives the user root-level"
        hint " power over this server, so it is your call.)" ;;
      *"Cannot connect"*|*"Is the docker daemon running"*|*"error during connect"*)
        fail "Docker is installed, but it is not running."
        hint "Start it:   sudo systemctl start docker" ;;
      *)
        fail "Docker did not answer properly:"
        printf '%s\n' "$_out" | head -n 5 | indent ;;
    esac
    return 1
  fi
  ok "Docker is running (version $(docker version --format '{{.Server.Version}}' 2>/dev/null || printf '?'))."

  case ${DOCKER_HOST:-} in
    ''|unix://*) ;;
    *)
      fail "DOCKER_HOST points at another machine ($DOCKER_HOST)."
      hint "bwwatch keeps its data in a folder next to this script, so Docker must run on THIS machine."
      hint "Run  unset DOCKER_HOST  and try again."
      return 1 ;;
  esac

  _v=$(docker compose version --short 2>/dev/null) || _v=''
  if [ -z "$_v" ]; then
    fail "Docker Compose v2 is missing (the 'docker compose' command does not work)."
    hint "Install the Compose plugin (package 'docker-compose-plugin'); Docker's own steps for your system are here:"
    hint "    $(docker_docs)"
    hint "(The old standalone 'docker-compose' is not supported.)"
    return 1
  fi
  _v=${_v#v}
  case $_v in
    0.*|1.*) fail "Docker Compose $_v is too old; version 2 or newer is needed."; return 1 ;;
  esac
  ok "Docker Compose $_v."
  return 0
}

ensure_env_file() {
  # Compose refuses to run anything unless .env exists; start from the documented template.
  if [ -f "$DIR/.env" ]; then
    chmod 600 "$DIR/.env" 2>/dev/null
  else
    cp "$DIR/.env.example" "$DIR/.env" && chmod 600 "$DIR/.env"
    ok "Created .env from the template (the guided setup fills it in)."
  fi
  if [ ! -d "$DIR/data" ]; then
    mkdir -p "$DIR/data" && chmod 700 "$DIR/data" 2>/dev/null
  fi
  return 0
}

check_compose_file() {
  if _out=$(dc config -q 2>&1); then
    ok "The Compose file is valid."
    return 0
  fi
  case $_out in
    *.env*|*"env file"*|*dotenv*|*"env_file"*)
      warn "Docker could not read your .env file:"
      printf '%s\n' "$_out" | head -n 4 | indent
      hint "A hand-edited line is usually the cause. The guided setup can start again from a fresh file."
      if ask_yes_no "Set this .env aside as .env.bak-$STAMP and continue with a fresh one?" y; then
        mv -f "$DIR/.env" "$DIR/.env.bak-$STAMP" && cp "$DIR/.env.example" "$DIR/.env" && chmod 600 "$DIR/.env" "$DIR/.env.bak-$STAMP"
        if _out=$(dc config -q 2>&1); then
          ok "Continuing with a fresh .env (your old one is kept as .env.bak-$STAMP)."
          return 0
        fi
      else
        cancelled
      fi ;;
  esac
  fail "Docker could not read docker-compose.yml:"
  printf '%s\n' "$_out" | head -n 8 | indent
  hint "If it mentions an unknown key, your Docker Compose is probably too old; update the Compose plugin."
  return 1
}

check_conflict() {
  if container_exists && ! container_is_ours; then
    _o=$(container_owner)
    fail "A container named \"$CONTAINER\" already exists and belongs to another folder${_o:+ ($_o)}."
    hint "That may be an earlier copy of bwwatch. This installer will not touch it."
    hint "Stop it from its own folder (docker compose down), or remove it yourself, then run this again:"
    hint "    docker rm -f $CONTAINER      (only if you are sure it is not something else of yours)"
    return 1
  fi
  return 0
}

check_disk() {
  _free=$(df -Pk "$DIR" 2>/dev/null | awk 'NR==2 {print $4}')
  if [ -n "$_free" ] && [ "$_free" -lt 204800 ] 2>/dev/null; then
    warn "Only $((_free / 1024)) MB free in $DIR (the data folder needs very little, but it is low)."
  fi
  _root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)
  if [ -n "$_root" ]; then
    _free=$(df -Pk "$_root" 2>/dev/null | awk 'NR==2 {print $4}')
    if [ -n "$_free" ] && [ "$_free" -lt 716800 ] 2>/dev/null; then
      warn "Only $((_free / 1024)) MB free for Docker's storage ($_root); the image needs about 300 MB while building."
    elif [ -n "$_free" ]; then
      ok "Enough disk space ($((_free / 1024)) MB free for Docker's storage)."
    fi
  fi
  return 0
}

check_boot() {
  if command -v systemctl >/dev/null 2>&1; then
    _state=$(systemctl is-enabled docker 2>/dev/null)
    case $_state in
      enabled|static|alias|indirect|enabled-runtime|'') ;;
      *)
        warn "Docker is not set to start when this server boots (systemctl says: $_state)."
        hint "bwwatch restarts by itself, but only if Docker is running. To make that so (your choice):"
        hint "    sudo systemctl enable docker" ;;
    esac
  fi
  return 0
}

part1() {
  heading "Part 1 of 4: Checking this server"
  check_folder || return 1
  LOGGING=1
  log "install started in $DIR"
  check_docker || return 1
  ensure_env_file || return 1
  check_compose_file || return 1
  check_conflict || return 1
  check_disk
  check_boot
  return 0
}

# ---------------------------------------------------------------------------------- Part 2: build
part2() {
  heading "Part 2 of 4: Building the program"
  say "Docker builds a small image from the files in this folder. It is almost all the Python base"
  say "image, which is downloaded the first time, so give it a minute or two."
  printf 'Building'
  dc build >"$OUT" 2>&1 &
  BG_PID=$!
  _t=$(date +%s)
  while kill -0 "$BG_PID" 2>/dev/null; do
    sleep 0.3 2>/dev/null || sleep 1
    _n=$(date +%s)
    if [ "$_n" != "$_t" ]; then printf '.'; _t=$_n; fi
  done
  wait "$BG_PID"; _rc=$?
  BG_PID=''
  printf '\n'
  cat "$OUT" >>"$LOG" 2>/dev/null
  if [ "$_rc" -ne 0 ]; then
    fail "The build failed. The last lines of Docker's output:"
    tail -n 25 "$OUT" | indent
    hint "Usual causes: no internet or DNS on this server (the Python base image could not be downloaded),"
    hint "Docker Hub's download limit, or a full disk. Fix that and run ./install.sh again."
    hint "The whole output is in $LOG"
    return 1
  fi
  ok "Built the image $IMAGE."
  return 0
}

# ---------------------------------------------------------------------------------- Part 3: guided setup
BACKUP=''

run_wizard() {
  if [ -t 0 ] && [ -t 1 ]; then
    dc run --rm bwwatch setup
  else
    dc run --rm -T bwwatch setup
  fi
}

install_new_env() {
  BACKUP=''
  if [ -f "$DIR/.env" ] && ! cmp -s "$DIR/.env" "$DIR/.env.example"; then
    BACKUP=$DIR/.env.bak-$STAMP
    cp "$DIR/.env" "$BACKUP" && chmod 600 "$BACKUP"
    ok "Your previous settings were kept as ${BACKUP##*/}."
  fi
  mv -f "$NEW_ENV" "$DIR/.env" && chmod 600 "$DIR/.env"
}

restore_env() {
  if [ -n "$BACKUP" ] && [ -f "$BACKUP" ]; then
    mv -f "$BACKUP" "$DIR/.env"
  else
    cp "$DIR/.env.example" "$DIR/.env"
  fi
  chmod 600 "$DIR/.env"
}

part3() {
  heading "Part 3 of 4: Guided setup"
  say "Six short steps: test your alerts, sign in to Wave once, and choose a few options."
  say "Nothing is installed until the end, and Ctrl-C stops safely at any point."
  if container_running; then
    say "bwwatch is running; pausing it while you change settings (it comes back automatically)."
    if dc stop >/dev/null 2>&1; then STOPPED_SERVICE=1; else warn "Could not pause the running service; continuing."; fi
  fi
  blank
  run_wizard; _rc=$?
  blank
  case $_rc in
    0) ;;
    130) cancelled ;;
    *)
      fail "The guided setup did not finish (status $_rc). Nothing was installed."
      hint "Read the message above, fix that, and run ./install.sh again."
      hint "A Wave sign-in you already completed is kept, so you will not need to repeat it."
      return 1 ;;
  esac

  if ! dc run --rm -T bwwatch setup --print-env >"$NEW_ENV" 2>>"$LOG" </dev/null || [ ! -s "$NEW_ENV" ]; then
    fail "The setup finished but its settings file could not be read back."
    hint "Run ./install.sh again; if it repeats, see $LOG"
    return 1
  fi
  ok "The guided setup is complete."

  install_new_env || { fail "Could not write $DIR/.env"; return 1; }
  if _out=$(dc run --rm -T bwwatch setup --verify 2>&1 </dev/null); then
    ok "$_out"
  else
    fail "The saved settings did not reach the program unchanged:"
    printf '%s\n' "$_out" | indent
    restore_env
    say "Your earlier .env was put back. Nothing was started."
    hint "Run ./install.sh again. If it repeats, a value probably holds an unusual character; use a simpler one."
    return 1
  fi
  dc run --rm -T bwwatch setup --cleanup >/dev/null 2>&1 </dev/null
  return 0
}

# ---------------------------------------------------------------------------------- Part 4: start and test
part4() {
  heading "Part 4 of 4: Starting it and testing it"
  _existing=0
  if container_exists; then _existing=1; fi
  SINCE=$(date +%s)
  if [ "$_existing" = 1 ]; then
    say "Restarting bwwatch with the new settings..."
    dc up -d --force-recreate bwwatch >"$OUT" 2>&1
  else
    say "Starting bwwatch..."
    dc up -d bwwatch >"$OUT" 2>&1
  fi
  _rc=$?
  cat "$OUT" >>"$LOG" 2>/dev/null
  if [ "$_rc" -ne 0 ]; then
    fail "Docker could not start bwwatch:"
    tail -n 15 "$OUT" | indent
    return 1
  fi
  STOPPED_SERVICE=0
  ok "bwwatch is running and set to restart by itself after a crash or reboot."

  say "Now waiting for its first check of your water heater (up to about 2.5 minutes)..."
  dc exec -T bwwatch bwwatch verify --wait 150 --since "$SINCE" >"$OUT" </dev/null; _rc=$?
  blank
  if [ ! -s "$OUT" ]; then
    fail "Could not run the health check inside the container. Its latest log lines:"
    dc logs --tail 20 bwwatch 2>&1 | indent
    hint "More:  $DIR/bwctl logs"
    return 1
  fi
  cat "$OUT"
  cat "$OUT" >>"$LOG" 2>/dev/null
  blank
  if [ "$_rc" -ne 0 ]; then
    fail "The health check found a problem (see the [FAIL] lines and the arrows above)."
    hint "bwwatch is still running. Fix the problem, then check again with:  ./install.sh --check"
    return 1
  fi
  if grep -q '\[WARN\]' "$OUT" 2>/dev/null; then
    ok "No problems found (the [WARN] lines above are notes, not failures)."
  else
    ok "Every check passed."
  fi
  FAULT_UNSET=0
  if grep -q 'Fault history.*not configured' "$OUT" 2>/dev/null; then FAULT_UNSET=1; fi

  blank
  if grep -q 'Startup alert.*was delivered' "$OUT" 2>/dev/null; then
    say "bwwatch has just sent \"bwwatch started\" to your alert channel(s)."
    if ! ask_yes_no "Did the \"bwwatch started\" message arrive?" y; then
      fail "Not confirmed: alerts are the whole point, so this is not finished."
      hint "Send one now and watch your device:   ./bwctl test-notify"
      hint "Then look at   ./bwctl logs   and fix the channel with   ./install.sh --reconfigure"
      return 1
    fi
  else
    say "No \"bwwatch started\" message was due this time, so there is nothing more to confirm: the alert"
    say "channels were already tested, one by one, during the guided setup."
  fi
  return 0
}

finish() {
  blank
  say "================================================================"
  say " bwwatch is installed and working."
  say "================================================================"
  say "It checks your water heater about once an hour and alerts you to anything new."
  say
  say "Handy commands (run them from $DIR):"
  say "  ./bwctl status                 one-screen summary"
  say "  ./bwctl faults                 the faults it has logged"
  say "  ./bwctl logs                   what it is doing"
  say "  ./bwctl test-notify            send yourself a test alert"
  say "  ./install.sh --check           run the health check again"
  say "  ./install.sh --reconfigure     change alerts, time zone, the Notifications request"
  say
  say "Everything it created is in $DIR:"
  say "  .env          your settings (private)"
  say "  data/         the database, backups and the Wave sign-in (private; back it up)"
  say "  install.log   what this installer did"
  say "Outside it there is only Docker's own storage: the image $IMAGE and the container $CONTAINER."
  if [ "${FAULT_UNSET:-0}" = 1 ]; then
    say
    warn "One thing is still open: the Notifications request (where \"Fault 10\" appears) is not set."
    hint "Until you add it, bwwatch tells you about settings changes and fault-like status flags only."
    hint "README, section \"Finding the fault request\"; then  ./install.sh --reconfigure"
  fi
  say
  say "It is a monitoring aid, not a safety device."
}

# ---------------------------------------------------------------------------------- modes
banner() {
  say "================================================================"
  say " bwwatch installer"
  say "================================================================"
  say "This sets up the Bradford White Wave fault watcher in Docker, step by step."
  say "It takes about 5 minutes. You will need your phone (to receive alerts) and a"
  say "web browser (to sign in to Wave once)."
  say
  say "Everything it creates stays in $DIR. Nothing else on this server is touched."
}

mode_install() {
  RECONFIGURE=${1:-0}
  banner
  part1 || exit 1
  if [ "$RECONFIGURE" = 0 ] && container_exists && ! cmp -s "$DIR/.env" "$DIR/.env.example"; then
    blank
    say "bwwatch is already installed here."
    if [ -t 0 ]; then
      say "  1) Check that it is working"
      say "  2) Run the guided setup again (change alerts, time zone, ...)"
      say "  3) Quit"
      printf 'Your choice [1]: '
      if ! IFS= read -r _choice; then cancelled; fi
      case $_choice in
        ''|1) mode_check; exit $? ;;
        2) RECONFIGURE=1 ;;
        *) exit 0 ;;
      esac
    else
      mode_check; exit $?
    fi
  fi
  part2 || exit 1
  part3 || exit 1
  part4 || exit 1
  finish
  exit 0
}

mode_check() {
  heading "Checking bwwatch"
  check_docker || return 1
  if ! container_exists; then
    fail "bwwatch is not installed here (no container named \"$CONTAINER\")."
    hint "Install it with:  ./install.sh"
    return 1
  fi
  check_conflict || return 1
  if ! container_running; then
    fail "bwwatch is installed but not running."
    hint "Start it:  ./bwctl start      then look at:  ./bwctl logs"
    return 1
  fi
  ok "The container is running."
  _report=$(dc exec -T bwwatch bwwatch verify </dev/null); _rc=$?
  if [ -z "$_report" ]; then
    fail "Could not run the health check inside the container."
    hint "Look at:  ./bwctl logs"
    return 1
  fi
  printf '%s\n' "$_report"
  blank
  if [ "$_rc" -eq 0 ]; then
    ok "Everything checks out."
    return 0
  fi
  fail "Something needs attention (see the [FAIL] lines and the arrows above)."
  return 1
}

mode_uninstall() {
  heading "Uninstalling bwwatch"
  check_docker || exit 1
  check_conflict || exit 1
  say "This will:"
  say "  - stop and remove the container \"$CONTAINER\""
  say "  - remove the image \"$IMAGE\""
  say "It will NOT touch anything else on this server: no other containers, images, volumes or"
  say "networks, and not the Python base image. Your settings (.env) and recorded data ($DIR/data:"
  say "the fault history, backups and Wave sign-in) are kept unless you choose below."
  blank
  if ! ask_yes_no "Remove the container and image?" n; then say "Nothing was changed."; exit 0; fi
  LOGGING=1
  log "uninstall confirmed"

  _wipe=0
  blank
  say "Do you also want to ERASE what bwwatch recorded and saved (data/, .env, old .env copies,"
  say "install.log)? This cannot be undone. Type DELETE (capital letters) to erase it, or just"
  say "press Enter to keep it."
  printf '> '
  if ! IFS= read -r _answer; then cancelled; fi
  if [ "$_answer" = DELETE ]; then _wipe=1; fi

  # Compose reads .env even to stop a container, so .env goes last. The service is stopped before its data
  # is erased: on its way out it writes status.json and folds the database log into the database again.
  if [ ! -f "$DIR/.env" ]; then cp "$DIR/.env.example" "$DIR/.env"; fi
  if dc down >"$OUT" 2>&1; then ok "Removed the container."; else warn "Docker said:"; tail -n 5 "$OUT" | indent; fi
  if [ "$_wipe" = 1 ]; then
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then
      # the files in data/ belong to the container's own user, so the container removes them
      if dc run --rm -T --no-deps --entrypoint find bwwatch /data -mindepth 1 -delete </dev/null >/dev/null 2>&1; then
        ok "Erased everything in data/."
      else
        warn "Could not erase data/ from inside the container; remove it yourself:  sudo rm -rf \"$DIR/data\""
      fi
    elif find "$DIR/data" -mindepth 1 -delete 2>/dev/null; then
      ok "Erased everything in data/."
    else
      warn "Could not erase data/; remove it yourself:  sudo rm -rf \"$DIR/data\""
    fi
  fi
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    if docker image rm "$IMAGE" >"$OUT" 2>&1; then ok "Removed the image $IMAGE."; else warn "Could not remove the image:"; tail -n 3 "$OUT" | indent; fi
  fi
  if [ "$_wipe" = 1 ]; then
    rm -f "$DIR/.env" "$DIR"/.env.bak-* "$DIR"/.env.new
    ok "Erased .env and its old copies."
    rm -f "$LOG"
  fi

  blank
  say "Done. What is left:"
  if [ "$_wipe" = 1 ]; then
    say "  - only the program files in $DIR"
  else
    say "  - $DIR/.env and $DIR/data (your settings and recorded data), plus the program files"
  fi
  say "  - Docker's cached Python base image (python:3.12-slim-bookworm), which other things may use."
  say "    Remove it, if nothing else needs it, with:  docker image rm python:3.12-slim-bookworm"
  say "To remove the rest of bwwatch, delete the folder yourself:  rm -rf $DIR"
  exit 0
}

# ---------------------------------------------------------------------------------- main
case ${1:-} in
  '')            mode_install 0 ;;
  --reconfigure) mode_install 1 ;;
  --check)       mode_check; exit $? ;;
  --uninstall)   mode_uninstall ;;
  -h|--help|help) usage; exit 0 ;;
  *)             printf 'install.sh: unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
esac
