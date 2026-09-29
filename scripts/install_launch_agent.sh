#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.local.claude-pairwise-gsb-console"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/Claude A-B GSB Console"
APP_HOME="$HOME/Library/Application Support/Claude A-B GSB Console"
APP_ROOT="$APP_HOME/app"
CONFIG_FILE="$APP_HOME/config.env"
DATA_DIR="$APP_HOME/.data"
mkdir -p "$(dirname "$PLIST")" "$LOG_DIR" "$APP_ROOT" "$DATA_DIR/backups"
if [[ ! -f "$CONFIG_FILE" ]]; then
  github_owner="$(gh api user --jq .login 2>/dev/null || true)"
  github_id="$(gh api user --jq .id 2>/dev/null || true)"
  git_author_email=""
  if [[ -n "$github_owner" && -n "$github_id" ]]; then
    git_author_email="${github_id}+${github_owner}@users.noreply.github.com"
  fi
  {
    printf "PAIRWISE_GITHUB_OWNER='%s'\n" "$github_owner"
    printf "PAIRWISE_GIT_AUTHOR_NAME='%s'\n" "刘昱"
    printf "PAIRWISE_GIT_AUTHOR_EMAIL='%s'\n" "$git_author_email"
    printf "PAIRWISE_GITHUB_VISIBILITY='%s'\n" "public"
    printf "PAIRWISE_CLAUDE_BASE_IMAGE='%s'\n" "claude-eval-runtime:claude-2.1.269"
    printf "PAIRWISE_CLAUDE_IMAGE='%s'\n" "claude-eval-runtime:prepared-2.1.269"
    printf "PAIRWISE_MAX_PARALLEL='%s'\n" "3"
    printf "PAIRWISE_MAX_TERMINALS='%s'\n" "3"
    printf "PAIRWISE_TASK_GENERATION_PARALLEL='%s'\n" "6"
  } > "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
  echo "Created $CONFIG_FILE"
fi
if grep -Eq "^PAIRWISE_CLAUDE_IMAGE=['\"]?claude-eval-runtime:claude-2\.1\.269['\"]?$" "$CONFIG_FILE"; then
  /usr/bin/python3 - "$CONFIG_FILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
line = "PAIRWISE_CLAUDE_BASE_IMAGE='claude-eval-runtime:claude-2.1.269'\n"
if "PAIRWISE_CLAUDE_BASE_IMAGE=" not in text:
    text += ("" if text.endswith("\n") else "\n") + line
text = text.replace("PAIRWISE_CLAUDE_IMAGE='claude-eval-runtime:claude-2.1.269'",
                    "PAIRWISE_CLAUDE_IMAGE='claude-eval-runtime:prepared-2.1.269'")
text = text.replace('PAIRWISE_CLAUDE_IMAGE="claude-eval-runtime:claude-2.1.269"',
                    'PAIRWISE_CLAUDE_IMAGE="claude-eval-runtime:prepared-2.1.269"')
text = text.replace("PAIRWISE_CLAUDE_IMAGE=claude-eval-runtime:claude-2.1.269",
                    "PAIRWISE_CLAUDE_IMAGE=claude-eval-runtime:prepared-2.1.269")
path.write_text(text, encoding="utf-8")
PY
fi
set -a
source "$CONFIG_FILE"
set +a
"$ROOT/scripts/build_claude_runtime.sh"
PAIRWISE_CONFIG_FILE="$CONFIG_FILE" "$ROOT/scripts/preflight.sh"
if [[ -f "$DATA_DIR/pairwise.db" ]]; then
  backup="$DATA_DIR/backups/pairwise-before-upgrade-$(date +%Y%m%d-%H%M%S).db"
  /usr/bin/python3 - "$DATA_DIR/pairwise.db" "$backup" <<'PY'
import sqlite3, sys
source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)
source.close()
target.close()
PY
  echo "Database backup: $backup"
fi
# LaunchAgents cannot reliably traverse a user Documents folder when macOS
# privacy controls are enabled. Install an isolated runtime copy under Library.
/usr/bin/rsync -a --delete \
  --exclude '.git' --exclude '.data' --exclude 'projects' --exclude '__pycache__' --exclude 'node_modules' \
  "$ROOT/" "$APP_ROOT/"
cd "$APP_ROOT"
npm ci --omit=dev
npx playwright install ffmpeg
chmod +x "$APP_ROOT/scripts/start.sh"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$APP_ROOT/scripts/start.sh</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/server.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/server-error.log</string>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
for _ in 1 2 3 4 5; do
  if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    break
  fi
  sleep 1
done
launchctl print "gui/$(id -u)/$LABEL" >/dev/null
launchctl enable "gui/$(id -u)/$LABEL"
echo "Installed $LABEL at http://127.0.0.1:8865"
