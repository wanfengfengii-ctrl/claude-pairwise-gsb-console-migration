#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS:/Applications/ChatGPT.app/Contents/Resources:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
APP_HOME="$HOME/Library/Application Support/Claude A-B GSB Console"
CONFIG_FILE="${PAIRWISE_CONFIG_FILE:-$APP_HOME/config.env}"
if [[ -f "$CONFIG_FILE" ]]; then
  set -a
  # This is a local, user-owned shell configuration created by the installer.
  source "$CONFIG_FILE"
  set +a
fi
github_owner="${PAIRWISE_GITHUB_OWNER:-}"
if [[ -z "$github_owner" ]] && command -v gh >/dev/null 2>&1; then
  github_owner="$(gh api user --jq .login 2>/dev/null || true)"
fi
git_author_email="${PAIRWISE_GIT_AUTHOR_EMAIL:-}"
if [[ -z "$git_author_email" && -n "$github_owner" ]] && command -v gh >/dev/null 2>&1; then
  github_id="$(gh api user --jq .id 2>/dev/null || true)"
  if [[ -n "$github_id" ]]; then
    git_author_email="${github_id}+${github_owner}@users.noreply.github.com"
  fi
fi
export PAIRWISE_DATA_DIR="${PAIRWISE_DATA_DIR:-$HOME/Library/Application Support/Claude A-B GSB Console/.data}"
export PAIRWISE_PROJECTS_DIR="${PAIRWISE_PROJECTS_DIR:-$HOME/Library/Application Support/Claude A-B GSB Console/projects}"
export PAIRWISE_PORT="${PAIRWISE_PORT:-8865}"
export PAIRWISE_CODEX_MODEL="${PAIRWISE_CODEX_MODEL:-gpt-5.6-sol}"
export PAIRWISE_CODEX_EFFORT="${PAIRWISE_CODEX_EFFORT:-medium}"
export PAIRWISE_CODEX_BUG_EFFORT="${PAIRWISE_CODEX_BUG_EFFORT:-high}"
export PAIRWISE_CLAUDE_MODEL="${PAIRWISE_CLAUDE_MODEL:-auto_model/urm}"
export PAIRWISE_CLAUDE_IMAGE="${PAIRWISE_CLAUDE_IMAGE:-claude-eval-runtime:prepared-2.1.269}"
export PAIRWISE_GITHUB_OWNER="$github_owner"
export PAIRWISE_GIT_AUTHOR_NAME="${PAIRWISE_GIT_AUTHOR_NAME:-刘昱}"
export PAIRWISE_GIT_AUTHOR_EMAIL="$git_author_email"
export PAIRWISE_GITHUB_VISIBILITY="${PAIRWISE_GITHUB_VISIBILITY:-public}"
export PAIRWISE_MAX_PARALLEL="${PAIRWISE_MAX_PARALLEL:-3}"
export PAIRWISE_TASK_GENERATION_PARALLEL="${PAIRWISE_TASK_GENERATION_PARALLEL:-6}"
cd "$ROOT"
exec /usr/bin/python3 -m pairwise_console "$@"
