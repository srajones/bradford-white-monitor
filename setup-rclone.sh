#!/bin/sh
# setup-rclone.sh: Hand-held, interactive setup for optional Dropbox cloud backups via rclone.
#
# bwwatch keeps crash-safe SQLite database backups and reports locally in data/.
# This script is 100% OPTIONAL and helps mirror those backups to your Dropbox account.

set -u

# find the real folder even when reached through a symbolic link
_self=$0
while [ -h "$_self" ]; do
  _dir=$(cd "$(dirname "$_self")" && pwd -P)
  _link=$(readlink "$_self")
  case $_link in
    /*) _self=$_link ;;
    *)  _self=$_dir/$_link ;;
  esac
done
DIR=$(cd "$(dirname "$_self")" 2>/dev/null && pwd -P) || DIR="/opt/bwheater"
BWCTL="$DIR/bwctl"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_OK=$(printf '\033[32m')
  C_WARN=$(printf '\033[33m')
  C_FAIL=$(printf '\033[31;1m')
  C_BOLD=$(printf '\033[1;36m')
  C_OFF=$(printf '\033[0m')
else
  C_OK=''; C_WARN=''; C_FAIL=''; C_BOLD=''; C_OFF=''
fi

say()  { printf '%s\n' "$*"; }
blank() { printf '\n'; }
ok()   { printf '%s[ OK ]%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s[WARN]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
fail() { printf '%s[FAIL]%s %s\n' "$C_FAIL" "$C_OFF" "$*"; }

ask_yes_no() {
  _prompt=$1; _default=${2:-y}
  while :; do
    if [ "$_default" = y ]; then _hint='Y/n'; else _hint='y/N'; fi
    printf '%s [%s] ' "$_prompt" "$_hint"
    if ! IFS= read -r _ans; then exit 130; fi
    case $(printf '%s' "$_ans" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz') in
      '')    if [ "$_default" = y ]; then return 0; fi; return 1 ;;
      y|yes) return 0 ;;
      n|no)  return 1 ;;
      *)     say "Please answer y or n." ;;
    esac
  done
}

blank
say "========================================================================"
printf '%s      OPTIONAL: CLOUD BACKUP SETUP (DROPBOX VIA RCLONE)%s\n' "$C_BOLD" "$C_OFF"
say "========================================================================"
say " bwwatch already maintains automatic crash-safe backups and CSV exports"
say " locally inside:  $DIR/data/"
say ""
say " Setting up Dropbox sync is 100% OPTIONAL. If you want off-site copies"
say " of your fault logs, water heater readings, and verified database"
say " backups, this guided script will walk you through setting it up."
say "========================================================================"
blank

# -----------------------------------------------------------------------------
# Step 1: Check if rclone is installed
# -----------------------------------------------------------------------------
printf '%sStep 1: Checking for rclone...%s\n' "$C_BOLD" "$C_OFF"

if command -v rclone >/dev/null 2>&1; then
  ok "rclone is already installed ($(rclone --version 2>/dev/null | head -n 1))."
else
  warn "rclone is not installed on this system."
  say "rclone is a secure, open-source command line program used to sync"
  say "files to cloud storage providers like Dropbox."
  blank
  if ask_yes_no "Would you like to install rclone now using the official installer?"; then
    say "Running: curl https://rclone.org/install.sh | sudo bash"
    if command -v sudo >/dev/null 2>&1; then
      curl -s https://rclone.org/install.sh | sudo bash
    else
      curl -s https://rclone.org/install.sh | bash
    fi
    if command -v rclone >/dev/null 2>&1; then
      ok "rclone was installed successfully!"
    else
      fail "Could not install rclone automatically. Please install it manually:"
      say "  curl https://rclone.org/install.sh | sudo bash"
      exit 1
    fi
  else
    say "Skipping rclone installation. You can install it later and re-run this script."
    exit 0
  fi
fi
blank

# -----------------------------------------------------------------------------
# Step 2: Check for Dropbox remote configuration
# -----------------------------------------------------------------------------
printf '%sStep 2: Checking Dropbox remote in rclone...%s\n' "$C_BOLD" "$C_OFF"

has_dropbox_remote() {
  rclone listremotes 2>/dev/null | grep -q '^dropbox:'
}

if has_dropbox_remote; then
  ok "An rclone remote named 'dropbox:' is already configured."
else
  warn "No remote named 'dropbox:' was found in rclone."
  blank
  say "------------------------------------------------------------------------"
  say " HOW TO CONNECT RCLONE TO DROPBOX"
  say "------------------------------------------------------------------------"
  say "Dropbox uses OAuth2 web authentication to grant access."
  say ""
  say "Option A: If this is a remote VPS (headless / no web browser):"
  say "  1. On your personal computer (Mac, Windows, or Linux with a browser):"
  say "       rclone authorize \"dropbox\""
  say "     A web browser will open. Log into Dropbox and click 'Allow'."
  say "  2. Your computer terminal will print an authorization token starting with:"
  say "       {\"access_token\":\"...\"}"
  say "  3. Copy that entire token."
  say "  4. Then, run 'rclone config' on this server:"
  say "       - Type 'n' for New remote"
  say "       - Name: dropbox"
  say "       - Storage: dropbox"
  say "       - client_id & client_secret: press Enter (leave blank)"
  say "       - Edit advanced config? 'n'"
  say "       - Already have an auth token / Use web browser? 'n'"
  say "       - Paste the token from step 2"
  say "       - Confirm with 'y', then 'q' to quit."
  say ""
  say "Option B: If you are running locally or can launch rclone config now:"
  say "  You can launch the interactive rclone setup right now."
  say "------------------------------------------------------------------------"
  blank
  if ask_yes_no "Would you like to run 'rclone config' right now to configure 'dropbox'?"; then
    rclone config
  fi

  if ! has_dropbox_remote; then
    warn "The 'dropbox:' remote has not been configured yet."
    say "When you are ready, run 'rclone config' to create the remote named 'dropbox:'"
    say "and re-run:  $DIR/setup-rclone.sh"
    exit 0
  fi
  ok "Dropbox remote successfully configured!"
fi
blank

# -----------------------------------------------------------------------------
# Step 3: Test Dropbox connection
# -----------------------------------------------------------------------------
printf '%sStep 3: Testing connection to Dropbox...%s\n' "$C_BOLD" "$C_OFF"
say "Listing top-level folders in dropbox: ..."
if rclone lsd dropbox: >/dev/null 2>&1; then
  ok "Connection to Dropbox succeeded!"
  say "Your bwwatch syncs will be stored under:"
  say "  Dropbox -> WaterHeater/backups/   (verified SQLite database backups)"
  say "  Dropbox -> WaterHeater/reports/   (report.md, faults.csv, readings.csv, energy.csv)"
else
  fail "Could not connect to dropbox:. Please check your credentials with: rclone config"
  exit 1
fi
blank

# -----------------------------------------------------------------------------
# Step 4: Perform initial sync
# -----------------------------------------------------------------------------
printf '%sStep 4: Initial test sync...%s\n' "$C_BOLD" "$C_OFF"
if ask_yes_no "Would you like to run an initial sync right now with './bwctl sync'?"; then
  blank
  "$BWCTL" sync
  blank
  ok "Initial sync complete! Check your Dropbox under WaterHeater/ to see your files."
else
  say "Skipping initial sync. You can run it anytime with:  $DIR/bwctl sync"
fi
blank

# -----------------------------------------------------------------------------
# Step 5: Optional automated daily cron job
# -----------------------------------------------------------------------------
printf '%sStep 5: Automated daily sync (Optional cron job)...%s\n' "$C_BOLD" "$C_OFF"
CRON_CMD="$DIR/bwctl sync >/dev/null 2>&1"

if crontab -l 2>/dev/null | grep -Fq "$DIR/bwctl sync"; then
  ok "A daily cron job for bwctl sync is already scheduled in crontab."
else
  say "Would you like to schedule an automatic daily sync (every morning at 6:00 AM)?"
  say "This adds the following line to your crontab:"
  say "  0 6 * * * $CRON_CMD"
  blank
  if ask_yes_no "Add automated daily sync to crontab?" n; then
    ( crontab -l 2>/dev/null || true; printf '0 6 * * * %s\n' "$CRON_CMD" ) | crontab -
    ok "Daily cron job added to crontab!"
  else
    say "No cron job added. You can sync on demand whenever you want with:  $DIR/bwctl sync"
  fi
fi

blank
say "========================================================================"
printf '%s              DROPBOX BACKUP SETUP COMPLETE!%s\n' "$C_OK" "$C_OFF"
say "========================================================================"
say " Everyday commands:"
say "   $DIR/bwctl sync          Generate fresh reports and sync to Dropbox"
say "   $DIR/bwcheat             Interactive menu (option 8 syncs to Dropbox)"
say "========================================================================"
blank
