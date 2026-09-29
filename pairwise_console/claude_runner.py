import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .commands import redact, run_command
from .config import Config, OLD_APP_DIR
from .db import Database, now_iso


CONTAINER_TRACE_PATH = "/home/node/.claude/projects"


class ClaudeRunner:
    """Claude is used only for the two A/B development arms."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.runtime_dir = config.data_dir / "claude-runs"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._terminal_paste_lock = threading.Lock()
        self._trace_snapshot_locks_guard = threading.Lock()
        self._trace_snapshot_locks: Dict[str, threading.Lock] = {}

    @staticmethod
    def canonical_prompt(prompt: str) -> str:
        """Return the prompt representation recorded by Claude's native TUI.

        The TUI normalizes line endings and removes blank paragraph rows before
        it writes the first user event.  Persisting and sending the same form
        keeps the database prompt byte-for-byte comparable with that event.
        """
        text = str(prompt).replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n[ \t]*\n+", "\n", text)
        return text.rstrip("\n")

    def preflight(self) -> Dict[str, Any]:
        image_name = str(self.db.setting("claude_image", self.config.claude_image))
        model = str(self.db.setting("claude_model", self.config.claude_model))
        checks: Dict[str, Any] = {"ok": True, "image": image_name, "model": model}
        for binary in ("docker", "screen", "osascript"):
            path = shutil.which(binary)
            checks[binary] = {"ok": bool(path), "path": path or ""}
            checks["ok"] = checks["ok"] and bool(path)
        if checks["docker"]["ok"]:
            image = run_command([checks["docker"]["path"], "image", "inspect", image_name, "--format", "{{.Id}}"], check=False, timeout=30)
            checks["image_id"] = image.stdout.strip() if image.returncode == 0 else ""
            checks["image_ok"] = image.returncode == 0
            checks["ok"] = checks["ok"] and image.returncode == 0
        else:
            checks["image_id"] = ""
            checks["image_ok"] = False
        return checks

    def prepare_arm(self, pair: Dict[str, Any], arm: str, workspace: Path) -> Dict[str, Any]:
        if arm not in ("A", "B"):
            raise ValueError("arm must be A or B")
        arm_id = "%s-%s" % (pair["id"], arm.lower())
        container = "pairwise-%s-%s" % (pair["id"].replace("pair-", "")[:16], arm.lower())
        screen = "pairwise-%s-%s" % (pair["id"].replace("pair-", "")[:16], arm)
        stamp = now_iso()
        current = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm=?", (pair["id"], arm))
        # A Pair's assignment is immutable. In particular, an API retry or
        # repository preparation retry must not silently pick up a later
        # global model setting or change only one side of the comparison.
        model = str(
            pair.get("model_a" if arm == "A" else "model_b")
            or (current or {}).get("model")
            or self.db.setting("claude_model", self.config.claude_model)
        )
        image = str(self.db.setting("claude_image", self.config.claude_image))
        values = (arm_id, pair["id"], arm, arm, str(workspace), container, screen, model,
                  image, "queued", stamp, stamp)
        if not current:
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", values,
            )
        elif not current.get("prompt_sent_at"):
            # Repository preparation can be retried before the prompt is sent.
            # Keep the canonical A/B clones separate and point Claude at a
            # disposable workspace that is empty when the container starts.
            self.db.execute(
                """UPDATE arm_runs SET workspace_path=?,container_name=?,screen_name=?,model=?,image=?,
                   status='queued',error='',updated_at=? WHERE id=?""",
                (str(workspace), container, screen, model, image, stamp, current["id"]),
            )
        return self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm=?", (pair["id"], arm)) or {}

    def reset_unsent_arm(self, arm_run: Dict[str, Any]) -> None:
        """Reset launch debris only when no task prompt has entered the session."""
        if arm_run.get("prompt_sent_at"):
            raise RuntimeError("该 Arm 已发送题面，不能按未启动任务重置")
        container = arm_run["container_name"]
        if run_command(["docker", "inspect", container], check=False, timeout=20).returncode == 0:
            run_command(["docker", "rm", "-f", container], check=False, timeout=60)
        if self._screen_running(arm_run["screen_name"]):
            run_command(["screen", "-S", arm_run["screen_name"], "-X", "quit"], check=False, timeout=20)
        root = self.runtime_dir / arm_run["id"]
        self._close_terminal_window(root / "terminal-window.json", arm_run["screen_name"])
        workspace = Path(arm_run["workspace_path"]).resolve()
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        for stale in (root / "terminal.log", root / "exit-status", root / "permission-status"):
            stale.unlink(missing_ok=True)
        self.db.execute(
            """UPDATE arm_runs SET status='queued',image_id='',session_id='',prompt_id='',trace_path='',
               commit_sha='',result='',warning_at=NULL,error='',updated_at=? WHERE id=?""",
            (now_iso(), arm_run["id"]),
        )

    def archive_failed_attempt(self, arm_run: Dict[str, Any], error: str,
                               prepare_retry: bool = True,
                               count_development_failure: bool = True,
                               count_error_retry: bool = True) -> Dict[str, Any]:
        """Preserve one failed attempt and optionally prepare a fresh session.

        The old container is removed only after its trace copy has been checked.
        If export fails the stopped container and its workspace are retained as
        evidence, while a retry receives new names and a new empty workspace.
        """
        arm_run = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_run["id"],)) or arm_run
        attempt_no = max(1, int(arm_run.get("attempt_no") or 1))
        archive = self.config.data_dir / "claude-attempts" / (
            "%s-attempt-%d-%s" % (arm_run["id"], attempt_no, uuid.uuid4().hex[:8])
        )
        archive.mkdir(parents=True, exist_ok=True)
        container = arm_run["container_name"]
        root = self.runtime_dir / arm_run["id"]
        stop_error = ""
        try:
            self._graceful_stop(arm_run)
        except Exception as exc:
            stop_error = redact(str(exc))
        container_exists = self._container_exists(container)
        trace_exported = False
        trace_error = ""
        if container_exists:
            traces = archive / "traces"
            traces.mkdir(parents=True, exist_ok=True)
            copied = self._copy_traces(container, traces)
            if copied.returncode == 0:
                try:
                    self._verify_trace_export(traces, arm_run, require_complete=False)
                    trace_exported = True
                except RuntimeError as exc:
                    trace_error = str(exc)
            else:
                trace_error = redact(copied.stderr or copied.stdout or "轨迹导出失败")
            if trace_exported:
                removed = run_command(["docker", "rm", container], check=False, timeout=60)
                if removed.returncode != 0:
                    trace_error = redact(removed.stderr or removed.stdout or "容器删除失败")
        else:
            previous = root / "traces"
            if previous.is_dir():
                shutil.copytree(previous, archive / "traces", dirs_exist_ok=True)
                try:
                    self._verify_trace_export(archive / "traces", arm_run, require_complete=False)
                    trace_exported = True
                except RuntimeError as exc:
                    trace_error = str(exc)
        if self._screen_running(arm_run["screen_name"]):
            run_command(["screen", "-S", arm_run["screen_name"], "-X", "quit"], check=False, timeout=20)
        self._close_terminal_window(root / "terminal-window.json", arm_run["screen_name"])
        for name in ("terminal.log", "exit-status", "permission-status", "prompt.txt"):
            source = root / name
            if source.is_file():
                shutil.copy2(source, archive / name)
            source.unlink(missing_ok=True)
        (archive / "error.txt").write_text(redact(error)[-4000:] + "\n", encoding="utf-8")
        workspace = Path(arm_run["workspace_path"]).resolve()
        archived_workspace = archive / "workspace"
        workspace_archived = False
        if workspace.is_dir():
            try:
                shutil.copytree(workspace, archived_workspace, dirs_exist_ok=True, symlinks=True)
                workspace_archived = True
            except OSError as exc:
                (archive / "workspace-export-error.txt").write_text(redact(str(exc)), encoding="utf-8")
        next_attempt = attempt_no + 1 if count_development_failure else attempt_no
        next_workspace = workspace
        next_container = container
        next_screen = arm_run["screen_name"]
        if prepare_retry:
            suffix = "r%d-%s" % (next_attempt, uuid.uuid4().hex[:6])
            next_workspace = workspace.parent / ("%s-%s" % (arm_run["arm"], suffix))
            next_workspace.mkdir(parents=True, exist_ok=False)
            base = "pairwise-%s-%s" % (arm_run["pair_id"].replace("pair-", "")[:12], arm_run["arm"].lower())
            next_container = "%s-%s" % (base, suffix)
            next_screen = "%s-%s" % (base, suffix)
        status = "queued" if prepare_retry else "failed"
        finished_at = None if prepare_retry else now_iso()
        error_retry_increment = 1 if count_error_retry else 0
        self.db.execute(
            """UPDATE arm_runs SET status=?,workspace_path=?,container_name=?,screen_name=?,
               image_id='',session_id='',prompt_id='',trace_path='',
               commit_sha='',result='',warning_at=NULL,error='',prompt_sent_at=NULL,finished_at=NULL,
               attempt_no=?,error_retry_count=error_retry_count+?,updated_at=? WHERE id=?""",
            (status, str(next_workspace), next_container, next_screen,
             next_attempt if prepare_retry else attempt_no, error_retry_increment, now_iso(), arm_run["id"]),
        )
        if not prepare_retry:
            self.db.execute(
                "UPDATE arm_runs SET finished_at=?,error=? WHERE id=?",
                (finished_at, redact(error)[-3000:], arm_run["id"]),
            )
        self.db.audit("claude.failed_attempt_archived", "arm_run", arm_run["id"], {
            "attempt": attempt_no, "archive": str(archive), "error": redact(error)[-1000:],
            "trace_exported": trace_exported, "trace_error": trace_error, "stop_error": stop_error,
            "container_retained": bool(container_exists and self._container_exists(container)),
            "workspace_archived": workspace_archived, "retry_prepared": prepare_retry,
            "counts_toward_development_attempts": count_development_failure,
            "counts_toward_error_retries": count_error_retry,
        })
        return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_run["id"],)) or {}

    def materialize_repository(self, arm_run: Dict[str, Any], source: Path, expected_sha: str) -> None:
        """Import an exact branch snapshot after Claude accepts the empty mount."""
        destination = Path(arm_run["workspace_path"]).resolve()
        source = source.resolve()
        if not self._container_running(arm_run["container_name"]):
            raise RuntimeError("Claude 容器尚未运行，不能导入仓库")
        if not source.is_dir() or not (source / ".git").is_dir():
            raise RuntimeError("A/B 源仓库不存在：%s" % source)
        if any(destination.iterdir()):
            raise RuntimeError("Claude 运行工作区在仓库导入前不是空目录")
        source_sha = run_command(["git", "rev-parse", "HEAD"], cwd=source, timeout=30).stdout.strip()
        source_branch = run_command(["git", "branch", "--show-current"], cwd=source, timeout=30).stdout.strip()
        source_status = run_command(["git", "status", "--porcelain"], cwd=source, timeout=30).stdout.strip()
        if source_sha != expected_sha or source_branch != arm_run["arm"] or source_status:
            raise RuntimeError("A/B 源仓库未保持指定分支的清洁基线")
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
        imported_sha = run_command(["git", "rev-parse", "HEAD"], cwd=destination, timeout=30).stdout.strip()
        imported_branch = run_command(["git", "branch", "--show-current"], cwd=destination, timeout=30).stdout.strip()
        imported_status = run_command(["git", "status", "--porcelain"], cwd=destination, timeout=30).stdout.strip()
        if imported_sha != expected_sha or imported_branch != arm_run["arm"] or imported_status:
            raise RuntimeError("导入后的 A/B 工作区未通过分支与基线校验")
        self.db.audit("claude.repository_materialized", "arm_run", arm_run["id"], {
            "arm": arm_run["arm"], "baseline_sha": imported_sha,
        })

    def launch(self, arm_run: Dict[str, Any]) -> None:
        arm_id = arm_run["id"]
        root = self.runtime_dir / arm_id
        root.mkdir(parents=True, exist_ok=True)
        launcher = root / "launch-container.command"
        screenrc = root / "screenrc"
        log = root / "terminal.log"
        exit_status = root / "exit-status"
        terminal_meta = root / "terminal-window.json"
        workspace = Path(arm_run["workspace_path"]).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        if any(workspace.iterdir()):
            raise RuntimeError("Claude 首次启动工作区必须为空")
        settings = Path.home() / ".claude" / "settings.json"
        image_id = run_command(["docker", "image", "inspect", arm_run["image"], "--format", "{{.Id}}"], timeout=30).stdout.strip()
        if run_command(["docker", "inspect", arm_run["container_name"]], check=False, timeout=20).returncode == 0:
            raise RuntimeError("容器名称已被占用：%s" % arm_run["container_name"])
        if self._screen_running(arm_run["screen_name"]):
            raise RuntimeError("Screen 会话名称已被占用：%s" % arm_run["screen_name"])
        script = """#!/bin/zsh
set -u
container_name=%s
workspace=%s
image=%s
model=%s
settings_file=%s
exit_status=%s
api_key="${apikey:-${ANTHROPIC_AUTH_TOKEN:-${ANTHROPIC_API_KEY:-}}}"
unset apikey ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY
if [[ -z "$api_key" && -f "$settings_file" ]]; then
  api_key="$(/usr/bin/python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); e=d.get("env", {}) if isinstance(d,dict) else {}; print(e.get("ANTHROPIC_AUTH_TOKEN") or e.get("ANTHROPIC_API_KEY") or "", end="")' "$settings_file" 2>/dev/null)"
fi
if [[ -z "$api_key" ]]; then
  read -r -s "api_key?请输入本次任务的 API Key（输入不显示）："
  printf '\n'
fi
docker run -it --init --restart=no --cap-drop ALL --security-opt no-new-privileges --name "$container_name" --mount "type=bind,src=$workspace,dst=/workspace" -e "apikey=$api_key" -e "ANTHROPIC_MODEL=$model" "$image"
code=$?
unset api_key
printf '%%s\n' "$code" > "$exit_status"
exit "$code"
""" % tuple(shlex.quote(str(v)) for v in (
            arm_run["container_name"], workspace, arm_run["image"], arm_run["model"], settings, exit_status
        ))
        launcher.write_text(script, encoding="utf-8")
        launcher.chmod(0o700)
        screenrc.write_text(
            'deflog on\nlogfile "%s"\nlogfile flush 1\ndefscrollback 10000\n'
            'defencoding UTF-8\ndefslowpaste 5\n'
            % str(log).replace('"', '\\"'),
            encoding="utf-8",
        )
        run_command(["screen", "-U", "-c", str(screenrc), "-dmS", arm_run["screen_name"], "/bin/zsh", str(launcher)], timeout=30)
        self._open_terminal(arm_run["screen_name"], terminal_meta)
        self.db.execute(
            "UPDATE arm_runs SET status='running',image_id=?,updated_at=? WHERE id=?",
            (image_id, now_iso(), arm_id),
        )
        self.db.audit("claude.arm_started", "arm_run", arm_id, {"arm": arm_run["arm"], "image_id": image_id})

    @staticmethod
    def _screen_prompt_chunks(prompt: str, max_bytes: int = 12):
        chunks = []
        current = ""
        for character in str(prompt or ""):
            candidate = current + character
            if current and len(candidate.encode("utf-8")) > max_bytes:
                chunks.append(current)
                current = character
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks

    def send_prompt(self, arm_run: Dict[str, Any], prompt: str) -> None:
        if not self._screen_running(arm_run["screen_name"]):
            raise RuntimeError("Claude 开发终端已经关闭")
        root = self.runtime_dir / arm_run["id"]
        prompt_path = root / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        # Screen can split a large UTF-8 read-buffer paste before the closing
        # bracketed-paste marker reaches Claude's TUI. Send lossless code-point
        # chunks inside one bracketed paste and submit exactly once. This is
        # one logical prompt, not a sequence of follow-up messages.
        chunks = self._screen_prompt_chunks(prompt)
        with self._terminal_paste_lock:
            commands = ["\x1b[200~", *chunks, "\x1b[201~"]
            submitted = None
            for chunk in commands:
                submitted = run_command([
                    "screen", "-U", "-S", arm_run["screen_name"], "-p", "0", "-X", "stuff", chunk,
                ], check=False, timeout=20)
                if submitted.returncode != 0:
                    break
                time.sleep(0.01)
            if submitted and submitted.returncode == 0:
                time.sleep(1)
                submitted = run_command([
                    "screen", "-U", "-S", arm_run["screen_name"], "-p", "0", "-X", "stuff", "\r",
                ], check=False, timeout=20)
        if not submitted or submitted.returncode != 0:
            detail = redact((submitted.stderr or submitted.stdout) if submitted else "Screen 未执行")
            raise RuntimeError("Screen 分块题面输入或提交失败：%s" % detail)
        deadline = time.monotonic() + 20
        state: Dict[str, Any] = {}
        while True:
            state = self.trace_state(arm_run, prompt)
            if state.get("path") or time.monotonic() >= deadline:
                break
            time.sleep(2)
        if not state.get("path"):
            raise RuntimeError("Terminal 题面提交后未形成可验证轨迹")
        if state.get("prompt_matches") is False or state.get("api_error") == "轨迹首轮 User Prompt 与数据库原题面不一致":
            raise RuntimeError("轨迹首轮 User Prompt 与数据库原题面不一致")
        stamp = now_iso()
        self.db.execute(
            """UPDATE arm_runs SET status='developing',prompt_sent_at=?,session_id=?,prompt_id=?,
               error='',updated_at=? WHERE id=?""",
            (stamp, str(state.get("session_id") or ""), str(state.get("prompt_id") or ""),
             stamp, arm_run["id"]),
        )

    def wait_until_ready(self, arm_run: Dict[str, Any], timeout: int = 3600) -> None:
        """Wait for Docker and accept Claude's one-time bypass permission prompt."""
        root = self.runtime_dir / arm_run["id"]
        log = root / "terminal.log"
        permission = root / "permission-status"
        deadline = time.monotonic() + timeout
        screen_start_grace = min(deadline, time.monotonic() + 15)
        while time.monotonic() < deadline:
            if self._container_running(arm_run["container_name"]):
                break
            # screen -dmS may return before its socket and Docker child become
            # visible. Do not archive a healthy launch during that short gap.
            if (not self._screen_running(arm_run["screen_name"])
                    and time.monotonic() >= screen_start_grace):
                raise RuntimeError("终端启动已结束，但 Claude 容器没有运行")
            time.sleep(2)
        else:
            raise RuntimeError("等待 Claude 容器启动超时")
        deadline = time.monotonic() + 60
        accepted_once = False
        while time.monotonic() < deadline:
            if not self._container_running(arm_run["container_name"]):
                raise RuntimeError("Claude 容器在权限确认前已停止")
            try:
                output = log.read_text(encoding="utf-8", errors="ignore")[-30000:]
            except OSError:
                output = ""
            visible = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).casefold()
            compact = re.sub(r"\s+", "", visible)
            if "bypasspermissionson" in compact:
                permission.write_text("accepted\n", encoding="utf-8")
                return
            if not accepted_once and all(token.casefold() in visible for token in ("Bypass", "Permissions", "Yes,", "accept")):
                run_command(["screen", "-S", arm_run["screen_name"], "-p", "0", "-X", "stuff", "\x1b[B\r"])
                accepted_once = True
            time.sleep(0.5)
        raise RuntimeError("Claude 权限确认后未进入对话主界面")

    def export_and_stop(self, arm_run: Dict[str, Any]) -> Path:
        arm_run = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_run["id"],)) or arm_run
        root = self.runtime_dir / arm_run["id"]
        trace_dir = root / "traces"
        staging = root / (".traces-export-%s" % uuid.uuid4().hex[:8])
        staging.mkdir(parents=True, exist_ok=False)
        self._graceful_stop(arm_run)
        copied = self._copy_traces(arm_run["container_name"], staging)
        if copied.returncode != 0:
            raise RuntimeError(redact(copied.stderr or copied.stdout or "轨迹导出失败"))
        self._verify_trace_export(staging, arm_run, require_complete=True)
        if trace_dir.exists():
            shutil.rmtree(trace_dir)
        staging.rename(trace_dir)
        removed = run_command(["docker", "rm", arm_run["container_name"]], check=False, timeout=60)
        if removed.returncode != 0:
            detail = redact(removed.stderr or removed.stdout or "轨迹已校验，但容器删除失败")
            removal_race = "removal of container" in detail.casefold() and "already in progress" in detail.casefold()
            missing_container = "no such container" in detail.casefold()
            if removal_race:
                deadline = time.monotonic() + 15
                while self._container_exists(arm_run["container_name"]) and time.monotonic() < deadline:
                    time.sleep(0.5)
            if not missing_container and self._container_exists(arm_run["container_name"]):
                raise RuntimeError(detail)
        if self._screen_running(arm_run["screen_name"]):
            run_command(["screen", "-S", arm_run["screen_name"], "-X", "quit"], check=False, timeout=20)
        self._close_terminal_window(root / "terminal-window.json", arm_run["screen_name"])
        self.db.execute(
            "UPDATE arm_runs SET status='exported',trace_path=?,finished_at=?,updated_at=? WHERE id=?",
            (str(trace_dir), now_iso(), now_iso(), arm_run["id"]),
        )
        return trace_dir

    def runtime_alive(self, arm_run: Dict[str, Any]) -> bool:
        return self._container_running(arm_run["container_name"]) or self._screen_running(arm_run["screen_name"])

    def stalled_gateway_timeout(self, arm_run: Dict[str, Any], trace_path: str,
                                idle_seconds: int = 120) -> str:
        """Detect a 504 retry spinner that never becomes a trace event.

        Claude Code can keep repainting an internal retry countdown while its
        JSONL trace remains parked after the preceding tool result. Give real
        commands a generous ten-minute quiet window, and only classify the
        session when the recent terminal tail contains an explicit 504/gateway
        timeout and the terminal is still actively repainting.
        """
        raw_trace_path = str(trace_path or "").strip()
        if not raw_trace_path:
            return ""
        try:
            trace = Path(raw_trace_path)
            if not trace.is_file():
                return ""
            trace_age = time.time() - trace.stat().st_mtime
        except (OSError, ValueError):
            return ""
        grace = max(600, max(1, int(idle_seconds)) * 5)
        if trace_age < grace:
            return ""
        terminal = self.runtime_dir / str(arm_run.get("id") or "") / "terminal.log"
        try:
            terminal_age = time.time() - terminal.stat().st_mtime
            if terminal_age > max(30, int(idle_seconds)):
                return ""
            with terminal.open("rb") as source:
                size = terminal.stat().st_size
                source.seek(max(0, size - 250000))
                tail = source.read().decode("utf-8", errors="replace").casefold()
        except OSError:
            return ""
        gateway_timeout = (
            "gateway time-out" in tail
            or "gateway timeout" in tail
            or bool(re.search(r"(?:^|\D)504(?:\D|$)", tail))
        )
        retrying = "retrying" in tail or "api error" in tail
        if gateway_timeout and retrying:
            return "API Error: 504 Gateway Timeout（Claude 内部重试界面超过 %d 秒无新轨迹）" % int(trace_age)
        return ""

    def _graceful_stop(self, arm_run: Dict[str, Any]) -> None:
        container = arm_run["container_name"]
        if not self._container_exists(container) or not self._container_running(container):
            return
        screen = arm_run["screen_name"]
        if self._screen_running(screen):
            for _ in range(2):
                run_command(["screen", "-S", screen, "-p", "0", "-X", "stuff", "\x04"], check=False, timeout=20)
                time.sleep(0.5)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and self._container_running(container):
                time.sleep(0.5)
        if self._container_running(container):
            stopped = run_command(["docker", "stop", "--time", "15", container], check=False, timeout=30)
            if stopped.returncode != 0:
                raise RuntimeError(redact(stopped.stderr or stopped.stdout or "容器无法正常停止"))

    @staticmethod
    def _copy_traces(container: str, destination: Path):
        result = None
        for _ in range(3):
            result = run_command(
                ["docker", "cp", "%s:%s/." % (container, CONTAINER_TRACE_PATH), str(destination)],
                check=False, timeout=180,
            )
            if result.returncode == 0:
                return result
            time.sleep(1)
        return result

    @staticmethod
    def _prompt_matches(expected: str, actual: str) -> bool:
        """Match prompt text after the newline changes made by terminal paste."""
        expected_text = str(expected or "").rstrip("\r\n")
        actual_text = str(actual or "").rstrip("\r\n")
        if expected_text == actual_text:
            return True

        # GNU Screen pastes a paragraph break as CRLF while ordinary line
        # breaks can become a lone CR, and the Claude TUI removes empty lines
        # before persisting the user event. Compare every non-empty line
        # exactly so transport normalization cannot strand a completed run,
        # while still rejecting any changed prompt text or line ordering.
        def normalize_terminal_lines(value: str) -> str:
            value = value.replace("\r\n", "\n").replace("\r", "\n")
            return "\n".join(line for line in value.split("\n") if line != "")

        if normalize_terminal_lines(expected_text) == normalize_terminal_lines(actual_text):
            return True
        marker = "[PAIRWISE_ARTIFACT_REPAIR]"
        if marker not in expected_text or marker not in actual_text:
            return False

        def normalize_repair(value: str) -> str:
            value = value.replace("\r\n", "\n").replace("\r", "\n")
            before, _, after = value.partition(marker)
            return before.rstrip() + "\n" + marker + "\n" + after.lstrip().rstrip("\n")

        return normalize_repair(expected_text) == normalize_repair(actual_text)

    @staticmethod
    def _verify_trace_export(trace_dir: Path, arm_run: Dict[str, Any], require_complete: bool) -> Path:
        expected_session = str(arm_run.get("session_id") or "")
        expected_prompt = str(arm_run.get("prompt_id") or "")
        prompt_file = trace_dir.parent / "prompt.txt"
        expected_text = ""
        try:
            expected_text = prompt_file.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            pass
        candidates = [path for path in trace_dir.rglob("*.jsonl") if path.is_file() and path.stat().st_size > 0]
        if not candidates:
            raise RuntimeError("轨迹导出后没有非空 JSONL，已保留容器")
        for path in candidates:
            if expected_session and path.stem != expected_session:
                continue
            matched_prompt = not expected_prompt and not expected_text
            completed = False
            try:
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "user":
                        message = event.get("message") if isinstance(event.get("message"), dict) else {}
                        content = message.get("content")
                        if ((expected_prompt and str(event.get("promptId") or "") == expected_prompt) or
                                (expected_text and isinstance(content, str)
                                 and ClaudeRunner._prompt_matches(expected_text, content))):
                            matched_prompt = True
                    if event.get("type") == "last-prompt" or (
                            event.get("type") == "system" and event.get("subtype") == "turn_duration"):
                        completed = True
            except OSError:
                continue
            if matched_prompt and (completed or not require_complete):
                return path
        raise RuntimeError("轨迹与当前 SessionID/PromptID 不匹配或尚未完整收尾，已保留容器")

    def trace_state(self, arm_run: Dict[str, Any], prompt: str) -> Dict[str, Any]:
        """Read one Arm trace without racing another monitor's snapshot refresh."""
        arm_id = str(arm_run.get("id") or "")
        with self._trace_snapshot_locks_guard:
            trace_lock = self._trace_snapshot_locks.setdefault(arm_id, threading.Lock())
        with trace_lock:
            return self._trace_state_locked(arm_run, prompt)

    def _trace_state_locked(self, arm_run: Dict[str, Any], prompt: str) -> Dict[str, Any]:
        root = self.runtime_dir / arm_run["id"]
        snapshot = root / ".trace-snapshot"
        if snapshot.exists():
            shutil.rmtree(snapshot)
        snapshot.mkdir(parents=True)
        copied = run_command(
            ["docker", "cp", "%s:%s/." % (arm_run["container_name"], CONTAINER_TRACE_PATH), str(snapshot)],
            check=False, timeout=120,
        )
        if copied.returncode != 0:
            return {"complete": False, "api_error": "", "path": ""}
        for path in sorted(snapshot.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            events = []
            try:
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        events.append(event)
            except OSError:
                continue
            start_index = None
            fallback_start_index = None
            observed_prompt = ""
            prompt_id = ""
            for index, event in enumerate(events):
                if event.get("type") != "user":
                    continue
                if event.get("isMeta") is True or event.get("turnCompanion") is True:
                    continue
                content = (event.get("message") or {}).get("content") if isinstance(event.get("message"), dict) else None
                if fallback_start_index is None and isinstance(content, str) and content.strip():
                    fallback_start_index = index
                    observed_prompt = content.rstrip("\r\n")
                if start_index is None and isinstance(content, str) and self._prompt_matches(prompt, content):
                    start_index, prompt_id = index, str(event.get("promptId") or "")
                    observed_prompt = content.rstrip("\r\n")
            prompt_matches = start_index is not None
            if start_index is None and fallback_start_index is not None:
                start_index = fallback_start_index
                prompt_id = str(events[start_index].get("promptId") or "")
            if start_index is None:
                mismatched = next((
                    event for event in events
                    if event.get("type") == "user"
                    and isinstance((event.get("message") or {}).get("content"), str)
                    and str((event.get("message") or {}).get("content") or "").strip()
                    and not event.get("isMeta")
                ), None)
                if mismatched:
                    return {
                        "complete": False,
                        "result": "",
                        "api_error": "轨迹首轮 User Prompt 与数据库原题面不一致",
                        "session_id": path.stem,
                        "prompt_id": str(mismatched.get("promptId") or ""),
                        "path": str(path),
                        "followup_detected": False,
                        "followup_text": "",
                    }
                continue
            final_text, final_index, visible_text, visible_index = "", None, "", None
            native_text, native_index = "", None
            api_error, api_index, api_error_at = "", None, ""
            last_turn_activity_index = start_index
            extra_user_message = ""
            automatic_companion_messages = []
            activity = []
            last_tool_activity_at = ""
            last_tool_activity_epoch = 0.0
            # Progress for the code-stall watchdog must describe model work,
            # not raw JSONL growth.  Claude records API-error and turn-ending
            # bookkeeping in the same file; counting those records as work
            # lets a repeating 504 postpone the 40-minute idle deadline
            # forever.  Start from the prompt and advance only on a normal
            # assistant text/tool event.
            effective_progress_index = start_index
            effective_progress_timestamp = str(events[start_index].get("timestamp") or "")
            for index in range(start_index + 1, len(events)):
                event = events[index]
                if event.get("type") == "user":
                    # Claude Code adds an internal turn-companion prompt when
                    # a model turn contains no user-visible text. It is not a
                    # human follow-up and must not invalidate an otherwise
                    # valid first-prompt trace. Keep treating every unmarked
                    # post-prompt user message as a real follow-up.
                    content = (event.get("message") or {}).get("content") if isinstance(event.get("message"), dict) else ""
                    if isinstance(content, str) and content.strip():
                        if event.get("isMeta") is True or event.get("turnCompanion") is True:
                            automatic_companion_messages.append(content.strip()[:300])
                            last_turn_activity_index = index
                            continue
                        extra_user_message = content.strip()[:300]
                        break
                if event.get("type") != "assistant":
                    continue
                last_turn_activity_index = index
                message = event.get("message") if isinstance(event.get("message"), dict) else {}
                content = message.get("content")
                blocks = content if isinstance(content, list) else []
                text = "\n".join(str(x.get("text") or "") for x in blocks if isinstance(x, dict) and x.get("type") == "text").strip()
                is_api_error = bool(event.get("isApiErrorMessage") or text.startswith("API Error:"))
                has_tool_use = any(
                    isinstance(block, dict) and block.get("type") == "tool_use"
                    for block in blocks
                )
                if not is_api_error:
                    for block in blocks:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            name = str(block.get("name") or "tool")
                            value = block.get("input") if isinstance(block.get("input"), dict) else {}
                            shape = " ".join(sorted(str(key) for key in value))
                            activity.append("tool:%s:%s" % (name, shape))
                        elif block.get("type") == "text" and str(block.get("text") or "").strip():
                            normalized = re.sub(
                                r"[0-9a-f]{8,}|\d+", "#",
                                re.sub(r"\s+", " ", str(block.get("text") or "").casefold()),
                            ).strip()
                            activity.append("text:" + normalized[:180])
                    if has_tool_use:
                        timestamp = str(event.get("timestamp") or "")
                        try:
                            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                            if parsed.tzinfo is None:
                                parsed = parsed.replace(tzinfo=timezone.utc)
                            epoch = parsed.timestamp()
                        except ValueError:
                            epoch = 0.0
                        if epoch >= last_tool_activity_epoch:
                            last_tool_activity_epoch = epoch
                            last_tool_activity_at = timestamp
                    if text or has_tool_use:
                        effective_progress_index = index
                        effective_progress_timestamp = str(
                            event.get("timestamp") or effective_progress_timestamp
                        )
                if is_api_error:
                    api_error, api_index = text or "API Error", index
                    api_error_at = str(event.get("timestamp") or "")
                elif text:
                    visible_text, visible_index = text, index
                    stop_reason = message.get("stop_reason")
                    if stop_reason in ("end_turn", "stop_sequence"):
                        final_text, final_index = text, index
                    elif not stop_reason and not any(
                            isinstance(block, dict) and block.get("type") == "tool_use"
                            for block in blocks):
                        native_text, native_index = text, index
            # A text block attached to stop_reason=tool_use is progress before
            # another command, not the final answer.  Treating it as complete
            # used to checkpoint an unchanged baseline as the delivered code.
            completion_index = final_index if final_index is not None else native_index
            completion_mode = "explicit_stop" if final_index is not None else ""
            if native_index is not None:
                completion_mode = "native_turn_end"
            finished = bool(completion_index is not None and any(
                e.get("type") == "last-prompt" or (e.get("type") == "system" and e.get("subtype") == "turn_duration")
                for e in events[completion_index + 1:]
            ))
            api_error_turn_ended = bool(api_index is not None and any(
                e.get("type") == "last-prompt" or (
                    e.get("type") == "system" and e.get("subtype") == "turn_duration"
                )
                for e in events[api_index + 1:]
            ))
            # A native turn can end after tool work without emitting a final
            # user-visible answer (for example when an upstream retry silently
            # gives up). The TUI is then back at its prompt and cannot make
            # more progress until another user message is injected. Surface
            # that terminal state immediately instead of waiting for the
            # 70-minute stall watchdog. A later automatic companion/user turn
            # advances ``last_turn_activity_index`` and therefore is not
            # mistaken for this condition while it is still running.
            turn_ended_without_final = bool(
                not finished
                and not api_error_turn_ended
                and any(
                    e.get("type") == "system" and e.get("subtype") == "turn_duration"
                    for e in events[last_turn_activity_index + 1:]
                )
            )
            activity_payload = activity[:80] or ["no-assistant-activity"]
            effective_progress_age_seconds = 0.0
            if effective_progress_timestamp:
                try:
                    effective_at = datetime.fromisoformat(
                        effective_progress_timestamp.replace("Z", "+00:00")
                    )
                    if effective_at.tzinfo is None:
                        effective_at = effective_at.replace(tzinfo=timezone.utc)
                    effective_progress_age_seconds = max(
                        0.0, (datetime.now(timezone.utc) - effective_at).total_seconds(),
                    )
                except ValueError:
                    pass
            return {
                "complete": finished,
                "result": final_text or native_text or visible_text,
                "completion_mode": completion_mode if finished else "",
                # Keep API errors as visible evidence, but do not use them as
                # a completion veto. Claude can recover inside the same native
                # session and later emit a valid final response.
                "api_error": api_error if api_index is not None else "",
                "api_error_at": api_error_at if api_index is not None else "",
                # When the native turn has already ended on the API error,
                # waiting in this Session cannot produce an automatic retry.
                # The service may open a fresh non-counting Session for 429.
                "api_error_turn_ended": api_error_turn_ended,
                "turn_ended_without_final": turn_ended_without_final,
                "session_id": path.stem,
                "prompt_id": prompt_id,
                "prompt_matches": prompt_matches,
                "observed_prompt": observed_prompt,
                "path": str(path),
                # API errors, automatic companions and turn metadata do not
                # count as effective development progress.  The age survives
                # service restarts so a restart cannot reset a stale Arm's
                # 40-minute watchdog window.
                "progress_token": "%s:%d:%d" % (
                    path.stem, start_index, effective_progress_index,
                ),
                "effective_progress_at": effective_progress_timestamp,
                "effective_progress_age_seconds": effective_progress_age_seconds,
                "followup_detected": bool(extra_user_message),
                "followup_text": extra_user_message,
                "automatic_companion_count": len(automatic_companion_messages),
                "automatic_companion_messages": automatic_companion_messages[:10],
                "activity_signature": hashlib.sha256(
                    json.dumps(activity_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "activity_summary": activity_payload[:12],
                # Free-form text can describe intent without touching the
                # product. Only a real tool call extends the business-work
                # clock used by the long-running development watchdog.
                "last_tool_activity_at": last_tool_activity_at,
            }
        empty_signature = hashlib.sha256(b'["no-trace-activity"]').hexdigest()
        return {"complete": False, "api_error": "", "path": "",
                "activity_signature": empty_signature, "activity_summary": ["no-trace-activity"]}

    @staticmethod
    def business_progress(workspace: Path, baseline_sha: str = "") -> Dict[str, Any]:
        """Return changed delivery code and its latest durable modification."""
        status = run_command(["git", "status", "--porcelain"], cwd=workspace, check=False, timeout=30)
        if status.returncode != 0:
            return {"has_code": False, "last_modified": 0.0, "paths": []}
        extensions = {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".vue", ".svelte", ".html", ".css", ".sql", ".sh"}
        ignored_parts = {
            ".venv", "venv", "env", "node_modules", ".pnpm-store",
            "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
            "dist", "build", "coverage", ".next", ".nuxt", ".turbo",
            "playwright-report", "test-results", "tests", "test", "__tests__",
        }
        ignored_names = {
            "repro.py", "reproduce.py", "reproduction.py",
            "repro.js", "reproduce.js", "reproduction.js",
            "repro.ts", "reproduce.ts", "reproduction.ts",
        }

        untracked_paths = {
            line[3:].strip().rstrip("/") for line in status.stdout.splitlines()
            if line.startswith("?? ")
        }

        def ignored(item: Path) -> bool:
            try:
                parts = item.relative_to(workspace).parts
            except ValueError:
                parts = item.parts
            if item.name == "__init__.py" and item.is_file():
                try:
                    body = ast.parse(item.read_text(encoding="utf-8")).body
                    if all(
                        isinstance(node, ast.Pass)
                        or (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                            and isinstance(node.value.value, str))
                        for node in body
                    ):
                        return True
                except (OSError, UnicodeError, SyntaxError):
                    pass
            return (
                item.name.casefold() in ignored_names
                # Root-level, untracked diagnostics and prototypes are not
                # changes to the delivered application. They must not suppress
                # the 60-minute no-business-code guard.
                or (len(parts) == 1 and parts[0] in untracked_paths
                    and item.name.casefold().startswith((
                        "proto_", "scratch_", "repro_", "reproduce_", "reproduction_",
                    )))
                or any(part.casefold() in ignored_parts for part in parts)
            )

        paths = [line[3:].split(" -> ")[-1].strip() for line in status.stdout.splitlines()]
        committed_after_baseline = False
        if baseline_sha:
            committed = run_command(
                ["git", "diff", "--name-only", "%s..HEAD" % baseline_sha],
                cwd=workspace, check=False, timeout=30,
            )
            if committed.returncode == 0:
                committed_paths = [
                    line.strip() for line in committed.stdout.splitlines() if line.strip()
                ]
                paths.extend(committed_paths)
                committed_after_baseline = bool(committed_paths)
        business_paths: List[str] = []
        latest = 0.0

        def record(item: Path) -> None:
            nonlocal latest
            try:
                relative = str(item.relative_to(workspace))
            except ValueError:
                relative = str(item)
            business_paths.append(relative)
            try:
                latest = max(latest, item.stat().st_mtime)
            except OSError:
                try:
                    latest = max(latest, item.parent.stat().st_mtime)
                except OSError:
                    pass

        for path in paths:
            item = workspace / path
            if ignored(item):
                continue
            if item.name in ("Dockerfile", "compose.yaml", "compose.yml", "docker-compose.yml") or item.suffix.casefold() in extensions:
                record(item)
                continue
            if item.is_dir():
                for child in item.rglob("*"):
                    if child.is_file() and not ignored(child) and (
                        child.name in ("Dockerfile", "compose.yaml", "compose.yml", "docker-compose.yml")
                        or child.suffix.casefold() in extensions
                    ):
                        record(child)
        if committed_after_baseline and business_paths:
            committed_at = run_command(
                ["git", "show", "-s", "--format=%ct", "HEAD"],
                cwd=workspace, check=False, timeout=30,
            )
            if committed_at.returncode == 0 and committed_at.stdout.strip().isdigit():
                latest = max(latest, float(committed_at.stdout.strip()))
        return {
            "has_code": bool(business_paths),
            "last_modified": latest,
            "paths": sorted(set(business_paths))[:20],
        }

    @staticmethod
    def has_business_code(workspace: Path, baseline_sha: str = "") -> bool:
        return bool(ClaudeRunner.business_progress(workspace, baseline_sha)["has_code"])

    @staticmethod
    def _screen_running(name: str) -> bool:
        probe = run_command(["screen", "-ls"], check=False, timeout=20)
        return probe.returncode in (0, 1) and (".%s" % name) in probe.stdout

    @staticmethod
    def _container_running(name: str) -> bool:
        probe = run_command(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False, timeout=20)
        return probe.returncode == 0 and probe.stdout.strip().casefold() == "true"

    @staticmethod
    def _container_exists(name: str) -> bool:
        return run_command(["docker", "inspect", name], check=False, timeout=20).returncode == 0

    def _open_terminal(self, screen_name: str, metadata_path: Path) -> None:
        # On current Terminal versions ``do script`` can remain blocked until
        # the attached Screen command exits, serializing every later Apple
        # event. Opening a small executable command file returns immediately,
        # while the command itself assigns a unique tab title before attaching.
        attach_script = metadata_path.parent / "attach-screen.command"
        attach_script.write_text(
            "#!/bin/zsh\n/usr/bin/printf '\\033]0;%s\\007' %s\nexec /usr/bin/screen -r %s\n"
            % ("%s", shlex.quote(screen_name), shlex.quote(screen_name)),
            encoding="utf-8",
        )
        attach_script.chmod(0o700)
        lookup_script = (
            'tell application "Terminal"\nset rows to {}\n'
            'repeat with candidateWindow in windows\nrepeat with candidateTab in tabs of candidateWindow\n'
            'set tabTitle to custom title of candidateTab\n'
            'if tabTitle is not missing value and (tabTitle as text) is %s '
            'and (processes of candidateTab contains "screen") then\n'
            % json.dumps(screen_name) +
            'set end of rows to ((id of candidateWindow) as text) & "|" & (tty of candidateTab as text)\n'
            'end if\nend repeat\nend repeat\nreturn rows\nend tell'
        )
        try:
            # Terminal window creation and native prompt insertion must never
            # overlap. Under concurrent launches Terminal can otherwise apply
            # ``do script ... in tab`` to the tab that became frontmost during
            # the same Apple-event batch, crossing prompts between Pair arms.
            with self._terminal_paste_lock:
                opened = run_command(
                    ["open", "-a", "Terminal", str(attach_script)], check=False, timeout=15,
                )
                if opened.returncode != 0:
                    return
                deadline = time.monotonic() + 20
                result = None
                while time.monotonic() < deadline:
                    try:
                        candidate = run_command(
                            ["osascript", "-e", lookup_script], check=False, timeout=5,
                        )
                    except (OSError, subprocess.SubprocessError):
                        candidate = None
                    if candidate and candidate.returncode == 0:
                        rows = [row.strip() for row in candidate.stdout.strip().split(", ") if "|" in row]
                        if rows:
                            result = rows[0]
                            break
                    time.sleep(0.5)
        except Exception:
            # The visible Terminal window is only a convenience. A slow or
            # unresponsive Terminal must not invalidate a running container.
            return
        if result:
            window, _, tty = result.partition("|")
            metadata_path.write_text(json.dumps({"window_id": window, "tty": tty, "title": screen_name}), encoding="utf-8")

    def _close_terminal_window(self, metadata_path: Path, screen_name: str) -> None:
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            window_id, tty, title = str(data["window_id"]), str(data["tty"]), str(data["title"])
        except (OSError, ValueError, KeyError, TypeError):
            metadata_path.unlink(missing_ok=True)
            return
        if title != screen_name or not window_id.isdigit() or not tty.startswith("/dev/"):
            metadata_path.unlink(missing_ok=True)
            return
        script = (
            'tell application "Terminal"\nrepeat with w in windows\n'
            'if (id of w as text) is %s then\n' % json.dumps(window_id) +
            'if (count of tabs of w) is 1 then\nrepeat with t in tabs of w\n'
            'if (tty of t as text) is %s and (name of w contains %s) then close w\n' % (
                json.dumps(tty), json.dumps(screen_name),
            ) +
            'end repeat\nend if\nend if\nend repeat\nend tell'
        )
        try:
            with self._terminal_paste_lock:
                run_command(["osascript", "-e", script], check=False, timeout=5)
        except Exception:
            # Container/session cleanup is authoritative. Terminal automation
            # is best effort and must never strand a Pair in an active state.
            pass
        finally:
            metadata_path.unlink(missing_ok=True)
