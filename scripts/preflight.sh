#!/bin/bash
set -u

export PATH="$HOME/.local/bin:/Applications/ChatGPT.app/Contents/Resources:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
APP_HOME="$HOME/Library/Application Support/Claude A-B GSB Console"
CONFIG_FILE="${PAIRWISE_CONFIG_FILE:-$APP_HOME/config.env}"
if [[ -f "$CONFIG_FILE" ]]; then
  set -a
  source "$CONFIG_FILE"
  set +a
fi

failures=0
check_command() {
  if command -v "$1" >/dev/null 2>&1; then
    printf 'OK   %-18s %s\n' "$1" "$(command -v "$1")"
  else
    printf 'FAIL %-18s 未安装或不在 PATH\n' "$1"
    failures=$((failures + 1))
  fi
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "FAIL operating-system   当前版本只支持 macOS"
  failures=$((failures + 1))
else
  echo "OK   operating-system   macOS"
fi

for command_name in python3 node npm docker screen osascript git gh codex; do
  check_command "$command_name"
done

if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    echo "OK   docker-daemon      正在运行"
  else
    echo "FAIL docker-daemon      请先启动 Docker Desktop"
    failures=$((failures + 1))
  fi
  if docker compose version >/dev/null 2>&1; then
    echo "OK   docker-compose     可用"
  else
    echo "FAIL docker-compose     Docker Compose 不可用"
    failures=$((failures + 1))
  fi
  image="${PAIRWISE_CLAUDE_IMAGE:-claude-eval-runtime:prepared-2.1.269}"
  if docker image inspect "$image" >/dev/null 2>&1; then
    echo "OK   claude-image       $image"
  else
    echo "FAIL claude-image       缺少 $image，请先导入或构建镜像"
    failures=$((failures + 1))
  fi
fi

if [[ -d "/Applications/Google Chrome.app" ]]; then
  echo "OK   google-chrome      /Applications/Google Chrome.app"
else
  echo "FAIL google-chrome      未安装到 /Applications"
  failures=$((failures + 1))
fi

if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then
    echo "OK   github-auth        已登录"
  else
    echo "FAIL github-auth        请运行 gh auth login"
    failures=$((failures + 1))
  fi
fi

if command -v codex >/dev/null 2>&1; then
  if codex login status >/dev/null 2>&1; then
    echo "OK   codex-auth         已登录"
  else
    echo "FAIL codex-auth         请先完成 Codex CLI 登录"
    failures=$((failures + 1))
  fi
fi

if /usr/bin/python3 - <<'PY'
import json
from pathlib import Path
try:
    data = json.loads((Path.home() / ".claude/settings.json").read_text(encoding="utf-8"))
    env = data.get("env", {}) if isinstance(data, dict) else {}
    raise SystemExit(0 if env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") else 1)
except (OSError, ValueError):
    raise SystemExit(1)
PY
then
  echo "OK   claude-auth        ~/.claude/settings.json 已配置"
else
  echo "FAIL claude-auth        ~/.claude/settings.json 缺少 ANTHROPIC_AUTH_TOKEN 或 ANTHROPIC_API_KEY"
  failures=$((failures + 1))
fi

if [[ $failures -ne 0 ]]; then
  echo "Preflight failed: $failures item(s) need attention."
  exit 1
fi
echo "Preflight passed."
