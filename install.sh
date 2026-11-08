#!/bin/bash
# Bears play-by-play -> iMessage: one-command installer for macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/rodneywhite999/bears-play-by-play/main/install.sh | bash
#
# Re-run it any time to update. Uninstall with:
#   curl -fsSL https://raw.githubusercontent.com/rodneywhite999/bears-play-by-play/main/install.sh | bash -s -- --uninstall
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/rodneywhite999/bears-play-by-play/main"
LABEL="com.bears-pbp.daemon"
APP_DIR="$HOME/Library/Application Support/bears-pbp"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/bears-pbp.log"
UID_NUM="$(id -u)"

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || die "This installer is for macOS (it sends iMessages through the Messages app)."

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  rm -rf "$APP_DIR"
  say "Uninstalled. (Log left at $LOG)"
  exit 0
fi

# ---------- who to text ----------
RECIPIENT="${1:-${IMESSAGE_TO:-}}"
if [ -z "$RECIPIENT" ] && [ -f "$APP_DIR/recipient" ]; then
  RECIPIENT="$(cat "$APP_DIR/recipient")"
  say "Using saved recipient: $RECIPIENT  (pass a new one as an argument to change it)"
fi
if [ -z "$RECIPIENT" ]; then
  printf '\nWhat phone number (with +1) or Apple ID email should get the iMessages?\n> '
  read -r RECIPIENT < /dev/tty
fi
RECIPIENT="$(echo "$RECIPIENT" | tr -d ' ()-')"
[ -n "$RECIPIENT" ] || die "No recipient given."

# ---------- find a working Python 3.9+ ----------
PY=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$cand" ] || continue
  if [ "$cand" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then
    continue  # just Apple's stub; would pop up an installer dialog
  fi
  if "$cand" -c 'import sys, zoneinfo, ssl; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  say "Python 3 isn't installed yet. A macOS dialog will open to install Apple's Command Line Tools."
  xcode-select --install 2>/dev/null || true
  die "Click 'Install' in that dialog, wait for it to finish (~5-10 min), then run this same command again."
fi
say "Using Python: $PY ($("$PY" --version 2>&1))"

# ---------- download the bot ----------
mkdir -p "$APP_DIR" "$(dirname "$PLIST")" "$(dirname "$LOG")"
say "Downloading the bot..."
curl -fsSL "$REPO_RAW/bears_pbp.py?$(date +%s)" -o "$APP_DIR/bears_pbp.py.new"
"$PY" -m py_compile "$APP_DIR/bears_pbp.py.new" || die "Downloaded file is broken; try again."
mv "$APP_DIR/bears_pbp.py.new" "$APP_DIR/bears_pbp.py"
echo "$RECIPIENT" > "$APP_DIR/recipient"

# ---------- test message from Terminal (grants Terminal -> Messages permission) ----------
say "Sending a test iMessage to $RECIPIENT ..."
echo "    If macOS asks to let Terminal control \"Messages\", click OK / Allow."
if ! IMESSAGE_TO="$RECIPIENT" "$PY" "$APP_DIR/bears_pbp.py" --test --senders imessage; then
  die "Test message failed. Open the Messages app, make sure you're signed in to iMessage, then re-run."
fi

# ---------- background service ----------
say "Installing the background service (starts automatically, survives restarts)..."
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <!-- caffeinate keeps the Mac from sleeping (on power) while the bot runs -->
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>-s</string>
    <string>$PY</string>
    <string>$APP_DIR/bears_pbp.py</string>
    <string>--daemon</string>
    <string>--senders</string>
    <string>imessage</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>IMESSAGE_TO</key><string>$RECIPIENT</string>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

: > "$LOG"
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true

say "Checking that the background service can send iMessages..."
echo "    If macOS asks to let \"python3\" (or \"caffeinate\") control \"Messages\", click OK / Allow."
ok=""
for _ in $(seq 1 60); do
  if grep -q "SENT via\|failed to deliver" "$LOG" 2>/dev/null; then
    break
  fi
  sleep 2
done
if grep -q "failed to deliver" "$LOG" 2>/dev/null || ! grep -q "SENT via" "$LOG" 2>/dev/null; then
  tail -5 "$LOG"
  echo
  echo "The background service couldn't send. Fix: System Settings > Privacy & Security > Automation,"
  echo "turn ON \"Messages\" under python3 / caffeinate, then run this installer again."
  exit 1
fi
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then ok=1; fi
[ -n "$ok" ] || die "Background service didn't start. Log: $LOG"

cat <<DONE

✅ All set! You should have 2 iMessages: a test message and "bot is running".

What happens now:
  • ~75 min before each Bears game you'll get "Bears play-by-play is ON".
  • Then every play, live, until the final score.

Keep this Mac ON, PLUGGED IN, LOGGED IN, and (for a laptop) with the LID OPEN on game days.
It's fine if the screen turns off / locks.

Log file:   $LOG
Update:     run the same install command again
Uninstall:  curl -fsSL $REPO_RAW/install.sh | bash -s -- --uninstall
DONE
