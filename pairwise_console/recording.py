import ast
import hashlib
from html import escape as html_escape
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .commands import redact, run_command
from .config import Config
from .db import Database, now_iso
from .artifact import isolated_compose_environment


def minimum_recording_duration_seconds(interaction_mode: str, project_category: str,
                                       runtime_recording: bool = True) -> int:
    # A complete interaction sequence determines validity; do not pad or reject
    # recordings merely to satisfy a uniform duration floor.
    return 0


DESTRUCTIVE_RECORDING_CONTROL = re.compile(
    r"删|移除|清空|清除|丢弃|销毁|重置|取消|关闭|退出|注销|下线|停止|终止|撤销|驳回|"
    r"remove|delete|\bdel\b|trash|discard|erase|destroy|clear|reset|cancel|close|"
    r"logout|stop|terminate|revoke|reject", re.IGNORECASE,
)


def recording_interaction_issue(events: list, interaction_mode: str = "auto") -> str:
    """Reject unsafe clicks even when the browser recorder reports success."""
    for event in events:
        if event.get("event") != "interaction":
            continue
        clicks = event.get("clicks")
        # API workflows report HTTP operations (sometimes with a numeric click
        # count), not labels for browser UI controls.
        if event.get("workflow") != "browser-ui" and not isinstance(clicks, list):
            continue
        if not isinstance(clicks, list):
            return "录像点击清单缺失或格式无效"
        for label in clicks:
            if DESTRUCTIVE_RECORDING_CONTROL.search(str(label)):
                return "自动录像点击了破坏性控件：" + str(label)[:100]
        if event.get("workflow") == "browser-ui":
            feature_count = event.get("featureCount")
            result_count = event.get("resultControlCount")
            if event.get("failedPageAssets"):
                return "功能页的脚本或样式资源加载失败，不能算功能录像"
            # The automatic recorder measures the before/after page change.
            # Manual recordings are reviewed from the captured interaction and
            # result; their event does not include that automatic measurement.
            if (interaction_mode != "manual" and event.get("visibleChange") is not True
                    and not event.get("requests")):
                return "按钮操作没有产生可见结果或成功业务请求"
            if event.get("finalResultVisible") is not True:
                return "自动录像结束时最终结果不可见"
            if (not isinstance(feature_count, int) or feature_count < len(clicks)
                    or not isinstance(result_count, int) or result_count < 0
                    or result_count > feature_count):
                return "自动录像控件计数与点击清单不一致"
    return ""


class RecordingManager:
    """Runs the selected artifact and records only its 1280x720 browser viewport."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.root = config.data_dir / "recordings"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._processes: Dict[str, subprocess.Popen] = {}
        self._cancelled = set()
        self._recover_interrupted_attempts()

    def _recover_interrupted_attempts(self) -> None:
        """Release the recorder lock after a service restart.

        Browser and Docker processes are children of the previous service process,
        so an unfinished database row cannot still be controlled safely here.  A
        later attempt reuses the deterministic Compose project name and cleans up
        any remaining containers before it starts.
        """
        rows = self.db.all(
            "SELECT * FROM recording_attempts WHERE status IN ('starting','recording','stopping')"
        )
        for row in rows:
            stamp = now_iso()
            # An MP4 alone does not prove the recorder completed the workflow.
            # After a restart its final recorderEvents are unavailable.
            message = (
                "服务重启时缺少最终操作与控件事件，保留文件但本次录像须重录"
                if row["status"] == "stopping"
                else "服务重启导致本次录像中断，请重新录制"
            )
            self.db.execute(
                """UPDATE recording_attempts SET status='failed',error=?,
                   finished_at=?,updated_at=? WHERE id=?""",
                (message, stamp, stamp, row["id"]),
            )

    def preflight(self) -> Dict[str, Any]:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        script = self.config.web_dir.parent / "scripts" / "browser_recorder.mjs"
        playwright = self.config.web_dir.parent / "node_modules" / "playwright"
        converter = self.config.web_dir.parent / "node_modules" / "ffmpeg-static" / "ffmpeg"
        ffmpeg = list((Path.home() / "Library/Caches/ms-playwright").glob("ffmpeg-*/ffmpeg-mac"))
        node = shutil.which("node")
        ok = bool(node and chrome.is_file() and script.is_file() and playwright.is_dir() and converter.is_file() and ffmpeg)
        return {"ok": ok, "node": node or "", "chrome": str(chrome), "script": str(script),
                "playwright": playwright.is_dir(), "ffmpeg": str(ffmpeg[-1]) if ffmpeg else "",
                "mp4Converter": str(converter) if converter.is_file() else ""}

    def start(self, pair_id: str, arm: str, x: int = 0, y: int = 0, manual: bool = False,
              demo_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if arm not in ("A", "B"):
            raise ValueError("arm must be A or B")
        if demo_override is not None:
            if not isinstance(demo_override, dict) or not isinstance(demo_override.get("path"), str):
                raise ValueError("录像业务请求必须包含相对路径")
            path_value = demo_override["path"]
            if not path_value.startswith("/") or path_value.startswith("//") or "?" in path_value:
                raise ValueError("录像业务请求只允许站内 API 路径")
            if str(demo_override.get("method", "")).lower() not in ("get", "post"):
                raise ValueError("录像业务请求只允许 GET 或 POST")
            if re.search(r"(?:^|/)(?:health|ready|live|docs)(?:/|$)", path_value, re.I):
                raise ValueError("健康检查或文档不能作为业务演示")
        active = self.db.one(
            "SELECT * FROM recording_attempts WHERE status IN ('starting','recording','stopping') ORDER BY created_at DESC LIMIT 1"
        )
        if active:
            if active["pair_id"] == pair_id and active["arm"] == arm:
                return active
            raise ValueError("当前已有浏览器录像正在启动或录制，请先完成后再录下一条")
        run = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm=?", (pair_id, arm))
        if not run or not run.get("commit_sha"):
            raise ValueError("该 Arm 尚无最终提交")
        check = self.db.one(
            """SELECT * FROM artifact_checks WHERE pair_id=? AND arm=? AND commit_sha=?
               ORDER BY created_at DESC LIMIT 1""", (pair_id, arm, run["commit_sha"]),
        )
        if not check:
            raise ValueError("最终提交尚未执行 Docker 产物验收")
        functional_failure = self._functional_page_with_private_failure(check, compose=None)
        failure_evidence = check.get("status") == "observed_failed" and not functional_failure
        if check.get("status") not in ("passed", "observed_failed"):
            raise ValueError("Docker 产物验收尚未形成最终结论，不能录制")
        compose_value = str(check.get("compose_file") or "")
        compose = Path(compose_value) if compose_value else None
        if (check.get("status") == "passed" or functional_failure) and (not compose or not compose.is_file()):
            raise ValueError("Docker Compose 文件不存在")
        attempt_id = "rec-attempt-" + uuid.uuid4().hex[:16]
        folder = self.root / pair_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("%s-%s.mp4" % (arm, attempt_id[-8:]))
        stamp = now_iso()
        project = "pairdemo-%s-%s" % (pair_id[-8:].lower(), arm.lower())
        interaction_mode = "failure" if failure_evidence else ("manual" if manual else "auto")
        capture_mode = "failure_evidence" if failure_evidence else "browser"
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,capture_mode,interaction_mode,runtime_project,
               compose_file,status,started_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?, 'starting',?,?,?)""",
            (attempt_id, pair_id, arm, run["commit_sha"], str(path), capture_mode, interaction_mode,
             project, str(compose or ""), stamp, stamp, stamp),
        )
        threading.Thread(
            target=self._launch, args=(attempt_id, Path(run["workspace_path"]), compose, project, path, check,
                                       demo_override), daemon=True
        ).start()
        self.db.audit("recording.start_requested", "recording_attempt", attempt_id, {
            "pair_id": pair_id, "arm": arm, "interaction_mode": interaction_mode,
            "capture_mode": capture_mode, "artifact_check_id": check["id"],
        })
        return self.db.one("SELECT * FROM recording_attempts WHERE id=?", (attempt_id,)) or {}

    def stop(self, pair_id: str, arm: str) -> Dict[str, Any]:
        row = self.db.one(
            """SELECT * FROM recording_attempts WHERE pair_id=? AND arm=?
               AND status IN ('starting','recording','stopping') ORDER BY created_at DESC LIMIT 1""", (pair_id, arm)
        )
        if not row:
            raise KeyError("没有正在进行的浏览器录像")
        if row["status"] == "stopping":
            return row
        with self._lock:
            process = self._processes.get(row["id"])
            self._cancelled.add(row["id"])
        self.db.execute(
            "UPDATE recording_attempts SET status='stopping',updated_at=? WHERE id=?",
            (now_iso(), row["id"]),
        )
        if process and process.poll() is None:
            Path(str(row["path"]) + ".stop").touch()
        self.db.audit("recording.stop_requested", "recording_attempt", row["id"], {
            "pair_id": pair_id, "arm": arm,
        })
        return self.db.one("SELECT * FROM recording_attempts WHERE id=?", (row["id"],)) or row

    def _launch(self, attempt_id: str, workspace: Path, compose: Path, project: str,
                path: Path, check: Dict[str, Any], demo_override: Optional[Dict[str, Any]] = None) -> None:
        attempt = self.db.one("SELECT capture_mode FROM recording_attempts WHERE id=?", (attempt_id,)) or {}
        if attempt.get("capture_mode") == "failure_evidence":
            self._launch_failure_evidence(attempt_id, workspace, path, check)
            return
        runtime_available = self._runtime_recording_is_allowed(check, compose)
        if not runtime_available:
            reason = "产物无法启动或验收失败，保留原始验收证据，不生成合格录像"
            self.db.execute(
                "UPDATE recording_attempts SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?",
                (reason, now_iso(), now_iso(), attempt_id),
            )
            self.db.audit("recording.finished", "recording_attempt", attempt_id, {
                "status": "failed", "error": reason, "mode": "artifact_evidence_only",
            })
            return
        env, assigned_ports = isolated_compose_environment(compose)
        port = int(
            assigned_ports.get("WEB_PORT")
            or assigned_ports.get("HTTP_PORT")
            or assigned_ports.get("APP_PORT")
            or assigned_ports.get("API_PORT")
            or next(iter(assigned_ports.values()))
        )
        base = ["docker", "compose", "-p", project, "-f", str(compose)]
        try:
            run_command(base + ["down", "-v", "--remove-orphans"], cwd=workspace, check=False, timeout=180, env=env)
            up = run_command(base + ["up", "-d", "--build"], cwd=workspace, check=False, timeout=1200, env=env)
            if up.returncode != 0:
                raise RuntimeError("演示项目启动失败：" + redact(up.stderr or up.stdout))
            preferred_ports = [
                int(assigned_ports.get(name) or 0)
                for name in ("WEB_PORT", "HTTP_PORT", "APP_PORT", "API_PORT")
            ]
            discovered = self._published_port(
                base, workspace, env, preferred_ports=preferred_ports,
            ) or port
            attempt_context = self.db.one(
                """SELECT t.project_category,r.arm FROM recording_attempts r
                   JOIN pairs p ON p.id=r.pair_id JOIN tasks t ON t.id=p.task_id
                   WHERE r.id=?""",
                (attempt_id,),
            ) or {}
            category = str(attempt_context.get("project_category") or "")
            arm = str(attempt_context.get("arm") or "")
            entry_url = self._wait_for_url(
                discovered, require_frontend=category in ("全栈", "纯前端"),
            )
            if not self._entry_matches_project_category(category, entry_url):
                raise RuntimeError(
                    "%s项目必须录制 Web 前端，不能使用后端文档或健康检查入口：%s"
                    % (category or "前端", entry_url)
                )
            api_entry = bool(re.search(
                r"/(?:docs(?:/.*)?|healthz?|ready|live|health/(?:ready|live)|"
                r"(?:api|v\d+)/health(?:/(?:ready|live))?)/?$",
                entry_url,
            ))
            task = self.db.one(
                """SELECT t.prompt FROM recording_attempts r
                   JOIN pairs p ON p.id=r.pair_id JOIN tasks t ON t.id=p.task_id
                   WHERE r.id=?""",
                (attempt_id,),
            ) or {}
            api_demo = (
                self._discover_api_demo(workspace, str(task.get("prompt") or ""))
                if api_entry or category == "纯后端"
                else None
            )
            if demo_override is not None:
                api_demo = {**demo_override, "force_direct": True}
            if arm == "B" and category == "纯后端" and api_demo:
                alternative = self._discover_alternative_read_only_demo(workspace, api_demo, entry_url)
                if alternative:
                    original_steps = api_demo.get("steps") if isinstance(api_demo.get("steps"), list) else [api_demo]
                    api_demo = {"steps": original_steps + [alternative]}
            upload_fixture = self._discover_upload_fixture(workspace)
            if re.search(
                    r"/(?:healthz?|ready|live|health/(?:ready|live)|"
                    r"(?:api|v\d+)/health(?:/(?:ready|live))?)/?$",
                    entry_url) and not api_demo:
                raise RuntimeError("演示项目只有健康检查入口，但未能识别可演示的业务接口")
            with self._lock:
                if attempt_id in self._cancelled:
                    raise RuntimeError("录像启动已取消")
            profile = self.root / "profiles" / attempt_id
            profile.mkdir(parents=True, exist_ok=True)
            command = [
                str(self.config.web_dir.parent / "node_modules" / ".bin" / "node"),
            ]
            if not Path(command[0]).exists():
                command = ["node"]
            command += [str(self.config.web_dir.parent / "scripts" / "browser_recorder.mjs"), entry_url,
                        str(path), str(profile), str(min(88, int(self.db.setting("recording_max_seconds", 90)) - 2)),
                        str(path) + ".stop",
                        str((self.db.one("SELECT interaction_mode FROM recording_attempts WHERE id=?", (attempt_id,)) or {}).get("interaction_mode") or "auto"),
                        json.dumps(api_demo or {}, ensure_ascii=False),
                        str(upload_fixture or ""), arm]
            process = subprocess.Popen(command, cwd=str(self.config.web_dir.parent), stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
            first = process.stdout.readline().strip() if process.stdout else ""
            if not first or '"event":"ready"' not in first:
                _, error = process.communicate(timeout=15)
                raise RuntimeError("浏览器录像启动失败：" + redact(error or first))
            with self._lock:
                self._processes[attempt_id] = process
            self.db.execute(
                """UPDATE recording_attempts SET status='recording',entry_url=?,runtime_port=?,updated_at=? WHERE id=?""",
                (entry_url, discovered, now_iso(), attempt_id),
            )
            self.db.audit("recording.started", "recording_attempt", attempt_id, {
                "entry_url": entry_url, "port": discovered,
                "upload_fixture": str(upload_fixture or ""),
            })
            self._wait(attempt_id, process, path, base, workspace, env, api_demo)
        except Exception as exc:
            run_command(base + ["down", "-v", "--remove-orphans"], cwd=workspace, check=False, timeout=180, env=env)
            self.db.execute(
                "UPDATE recording_attempts SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?",
                (redact(str(exc))[-3000:], now_iso(), now_iso(), attempt_id),
            )
            self.db.audit("recording.finished", "recording_attempt", attempt_id, {"status": "failed", "error": str(exc)[-1000:]})

    @staticmethod
    def _runtime_recording_is_allowed(check: Dict[str, Any], compose: Optional[Path]) -> bool:
        """Use the real app when Docker starts even if a separate business test failed.

        An observed business-test defect belongs in the GSB evidence, but it does
        not make a working browser application unrecordable. Startup, health and
        container failures still use the terminal evidence page so the recording
        never hides an unstartable delivery.
        """
        if check.get("status") == "passed":
            return bool(compose and compose.is_file())
        if check.get("status") != "observed_failed" or not compose or not compose.is_file():
            return False
        try:
            checks = json.loads(check.get("checks_json") or "[]")
        except (TypeError, ValueError):
            return False
        if isinstance(checks, dict):
            checks = checks.get("checks") or [checks]
        failed = [item for item in checks if isinstance(item, dict) and not item.get("passed")]
        if not failed:
            return bool(checks) and all(
                isinstance(item, dict) and item.get("passed") is True for item in checks
            )
        # A verifier that exceeds the automatic-acceptance limit violated the
        # one-shot contract rather than merely finding a business assertion.
        # Likewise, a Compose file with no runnable application has nothing
        # real to demonstrate.  Both cases must show failure evidence instead
        # of launching a misleading browser walkthrough.
        for item in failed:
            name = str(item.get("name") or "")
            if name == "application_service_present":
                return False
            if name == "verify_service" and int(item.get("exit_code") or 0) == 124:
                return False
        blocking = re.compile(
            r"compose|dockerfile|application_service|clean_start|containers?_running|health|startup|published_port",
            re.IGNORECASE,
        )
        return all(not blocking.search(str(item.get("name") or "")) for item in failed)

    def _functional_page_with_private_failure(self, check: Dict[str, Any],
                                              compose: Optional[Path] = None) -> bool:
        """A passing Docker run can still have a separately recorded Bug failure."""
        if check.get("status") != "observed_failed":
            return False
        compose_value = str(check.get("compose_file") or "")
        compose = compose or (Path(compose_value) if compose_value else None)
        if not self._runtime_recording_is_allowed(check, compose):
            return False
        try:
            checks = json.loads(check.get("checks_json") or "[]")
        except (TypeError, ValueError):
            return False
        if not isinstance(checks, list) or not checks or not all(
            isinstance(item, dict) and item.get("passed") is True for item in checks
        ):
            return False
        result = self.db.one(
            """SELECT status FROM bug_verification_results
               WHERE pair_id=? AND arm=? AND commit_sha=? AND status='observed_failed'
               ORDER BY created_at DESC LIMIT 1""",
            (check.get("pair_id"), check.get("arm"), check.get("commit_sha")),
        )
        return bool(result)

    def _launch_failure_evidence(self, attempt_id: str, workspace: Path, path: Path,
                                 check: Dict[str, Any]) -> None:
        """Record the real Docker validation output in a browser-only evidence page."""
        try:
            checks = json.loads(check.get("checks_json") or "[]")
        except ValueError:
            checks = []
        if isinstance(checks, dict):
            checks = checks.get("checks") or [checks]
        if not isinstance(checks, list):
            checks = []
        failed_checks = [item for item in checks if isinstance(item, dict) and not item.get("passed")]
        attempt = self.db.one("SELECT pair_id,arm,commit_sha FROM recording_attempts WHERE id=?", (attempt_id,)) or {}
        task = self.db.one(
            """SELECT t.task_type,t.repair_verification_json FROM tasks t
               JOIN pairs p ON p.task_id=t.id WHERE p.id=?""", (attempt.get("pair_id"),),
        ) or {}
        private_failures = []
        if task.get("task_type") == "bugfix":
            raw = str(task.get("repair_verification_json") or "[]")
            verifier_hash = hashlib.sha256(("v1:" + raw).encode()).hexdigest()
            private = self.db.one(
                """SELECT evidence_json FROM bug_verification_results
                   WHERE pair_id=? AND arm=? AND commit_sha=? AND verifier_hash=?
                   ORDER BY created_at DESC LIMIT 1""",
                (attempt.get("pair_id"), attempt.get("arm"), attempt.get("commit_sha"), verifier_hash),
            ) or {}
            try:
                commands = json.loads(private.get("evidence_json") or "{}").get("commands") or []
            except (TypeError, ValueError):
                commands = []
            for result in commands:
                if not isinstance(result, dict) or result.get("matched"):
                    continue
                private_failures.append({
                    "name": "独立修复验收 · " + str(result.get("scenario") or "业务边界"),
                    "passed": False, "businessFailed": bool(result.get("businessFailed")),
                    "exit_code": result.get("exitCode"),
                    "detail": str(result.get("output") or "")[-800:],
                })
        if not failed_checks and not private_failures and not check.get("error"):
            reason = "缺少可核对的原始失败输出，不能生成失败证据录像"
            self.db.execute(
                "UPDATE recording_attempts SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?",
                (reason, now_iso(), now_iso(), attempt_id),
            )
            self.db.audit("recording.finished", "recording_attempt", attempt_id, {
                "status": "failed", "error": reason,
            })
            return
        shown_checks = failed_checks + private_failures
        if not shown_checks:
            shown_checks = checks[-1:]
        if check.get("error") and not failed_checks and not private_failures:
            shown_checks = list(shown_checks) + [{
                "name": "验收中止原因", "passed": False,
                "detail": check["error"],
            }]
        blocks = []
        for item in shown_checks:
            name = str(item.get("name") or "Docker 检查")
            command = str(item.get("command") or "")
            exit_code = item.get("exit_code")
            outcome = (
                "业务断言失败 · 进程已正常退出" if item.get("businessFailed") else
                "命令执行失败 · exit code %s" % exit_code if exit_code is not None and exit_code != 0 else
                "验收断言失败 · 进程已正常退出" if exit_code == 0 and not item.get("passed") else
                ("验收判定失败" if not item.get("passed") else "此前检查已执行；后续验收中止")
            )
            detail = str(item.get("detail") or check.get("error") or "无输出")[-800:]
            blocks.append(
                "<section class=\"terminal\"><div class=\"terminal-bar\"><i></i><i></i><i></i>%s</div>"
                "<pre><span class=\"prompt\">%s</span>\n%s\n\n<span class=\"exit\">%s</span></pre></section>" % (
                    html_escape(name), html_escape("$ " + command if command else "检查记录（未记录独立命令）"),
                    html_escape(detail), html_escape(outcome),
                )
            )
        error = html_escape(str(check.get("error") or "Docker 产物验收未通过"))
        page = path.with_suffix(".failure.html")
        page.write_text("""<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">
<title>验收失败证据录像（非功能通过）</title><style>
body{margin:0;background:#111814;color:#eef4ef;font:17px/1.55 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}
main{width:1120px;margin:0 auto;padding:38px 0 80px}header{display:flex;justify-content:space-between;gap:32px;align-items:end;margin-bottom:22px}
h1{margin:0 0 7px;font-size:32px}header p{margin:3px 0;color:#a9b9af}.result{color:#ffb4a9;font-weight:700}
.terminal{overflow:hidden;border:1px solid #3c4b43;background:#08100c;border-radius:14px;box-shadow:0 18px 48px rgba(0,0,0,.28)}
.terminal-bar{height:42px;display:flex;align-items:center;gap:8px;padding:0 16px;background:#243029;color:#9fb0a6;font-size:14px}.terminal-bar i{width:12px;height:12px;border-radius:50%%;background:#e16b62}.terminal-bar i:nth-child(2){background:#e7b75d}.terminal-bar i:nth-child(3){background:#63bd79}.terminal-bar i:nth-child(3){margin-right:9px}
pre{height:410px;overflow:auto;margin:0;padding:24px;white-space:pre-wrap;word-break:break-word;color:#d6e5db;font:15px/1.58 ui-monospace,SFMono-Regular,Menlo,monospace}.prompt{color:#78d69a;font-weight:700}.exit{color:#ff8f84;font-weight:700}
</style><main><header><div><h1>验收失败证据录像</h1><p>提交 %s · 来自原始验收记录，不代表功能通过</p></div><div class="result">验收失败 · 非功能通过</div></header>%s</main></html>""" % (
            html_escape(str(check.get("commit_sha") or "未知")[:12]),
            "".join(blocks) or "<section><h2>未生成检查步骤</h2><pre>%s</pre></section>" % error,
        ), encoding="utf-8")
        entry_url = page.resolve().as_uri()
        profile = self.root / "profiles" / attempt_id
        profile.mkdir(parents=True, exist_ok=True)
        command = [str(self.config.web_dir.parent / "node_modules" / ".bin" / "node")]
        if not Path(command[0]).exists():
            command = ["node"]
        duration = min(88, int(self.db.setting("recording_max_seconds", 90)) - 2)
        command += [str(self.config.web_dir.parent / "scripts" / "browser_recorder.mjs"), entry_url,
                    str(path), str(profile), str(duration), str(path) + ".stop", "failure"]
        try:
            process = subprocess.Popen(command, cwd=str(self.config.web_dir.parent), stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
            first = process.stdout.readline().strip() if process.stdout else ""
            if not first or '"event":"ready"' not in first:
                _, recorder_error = process.communicate(timeout=15)
                raise RuntimeError("失败过程录像启动失败：" + redact(recorder_error or first))
            with self._lock:
                self._processes[attempt_id] = process
            self.db.execute(
                """UPDATE recording_attempts SET status='recording',entry_url=?,updated_at=? WHERE id=?""",
                (entry_url, now_iso(), attempt_id),
            )
            self.db.audit("recording.failure_evidence_started", "recording_attempt", attempt_id, {
                "entry_url": entry_url, "artifact_status": check.get("status"),
            })
            failure_step = {
                "steps": [{
                    "kind": "docker_failure",
                    "command": str((shown_checks or [{}])[0].get("command") or ""),
                    "status": "failed",
                    "output": str((shown_checks or [{}])[0].get("detail") or check.get("error") or "")[:1000],
                }]
            }
            self._wait(
                attempt_id, process, path, None, workspace, os.environ.copy(), failure_step
            )
        except Exception as exc:
            self.db.execute(
                "UPDATE recording_attempts SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?",
                (redact(str(exc))[-3000:], now_iso(), now_iso(), attempt_id),
            )
            self.db.audit("recording.finished", "recording_attempt", attempt_id, {
                "status": "failed", "error": str(exc)[-1000:],
            })

    def _wait(self, attempt_id: str, process: subprocess.Popen, path: Path, base,
              workspace: Path, env, api_demo: Optional[Dict[str, Any]] = None) -> None:
        stdout, stderr = process.communicate()
        with self._lock:
            self._processes.pop(attempt_id, None)
            self._cancelled.discard(attempt_id)
        if base:
            run_command(base + ["down", "-v", "--remove-orphans"], cwd=workspace, check=False, timeout=180, env=env)
        result = inspect_recording(path)
        attempt = self.db.one(
            """SELECT r.interaction_mode,t.project_category FROM recording_attempts r
                 JOIN pairs p ON p.id=r.pair_id JOIN tasks t ON t.id=p.task_id
                WHERE r.id=?""",
            (attempt_id,),
        ) or {}
        minimum_duration = minimum_recording_duration_seconds(
            str(attempt.get("interaction_mode") or ""),
            str(attempt.get("project_category") or ""),
            runtime_recording=bool(base),
        )
        if result.get("ok") and float(result.get("duration_seconds") or 0) < minimum_duration:
            result["ok"] = False
            result["error"] = "前端自动录像至少需要 %d 秒并完成结果区操作" % minimum_duration
        recorder_events = []
        for line in str(stdout or "").splitlines():
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict) and event.get("event") in ("interaction", "automatic_timing", "finished"):
                recorder_events.append(event)
        if result.get("ok") and process.returncode == 0:
            integrity_error = self._recording_integrity_error(
                attempt_id, result, recorder_events, require_interaction=bool(base),
            )
            if integrity_error:
                result["ok"] = False
                result["error"] = integrity_error
        status = "passed" if process.returncode == 0 and result.get("ok") else "failed"
        if status == "passed":
            error = ""
        else:
            details = [str(result.get("error") or "").strip(), redact(str(stderr or "")).strip()]
            error = "；".join(item for item in details if item)
        self.db.execute(
            """UPDATE recording_attempts SET sha256=?,width=?,height=?,duration_seconds=?,status=?,error=?,
               finished_at=?,updated_at=? WHERE id=?""",
            (result.get("sha256", ""), result.get("width", 0), result.get("height", 0),
             result.get("duration_seconds", 0), status, error, now_iso(), now_iso(), attempt_id),
        )
        if status == "passed":
            self._promote(attempt_id, api_demo)
        self.db.audit("recording.finished", "recording_attempt", attempt_id, {
            "status": status, "error": error, "recorderEvents": recorder_events[-4:],
        })

    def _recording_integrity_error(self, attempt_id: str, result: Dict[str, Any],
                                   events: list, require_interaction: bool = True) -> str:
        attempt = self.db.one("SELECT * FROM recording_attempts WHERE id=?", (attempt_id,)) or {}
        current = self.db.one(
            "SELECT commit_sha FROM arm_runs WHERE pair_id=? AND arm=?",
            (attempt.get("pair_id"), attempt.get("arm")),
        ) or {}
        if not attempt or not current or attempt.get("commit_sha") != current.get("commit_sha"):
            return "录像提交与当前 Arm 产物不一致"
        check = self.db.one(
            """SELECT * FROM artifact_checks WHERE pair_id=? AND arm=? AND commit_sha=?
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (attempt.get("pair_id"), attempt.get("arm"), attempt.get("commit_sha")),
        ) or {}
        if attempt.get("capture_mode") == "failure_evidence":
            if check.get("status") != "observed_failed":
                return "失败证据录像所对应的产物验收不再是失败结论"
            if not result.get("sha256") or not any(event.get("event") == "finished" for event in events):
                return "失败证据录像缺少文件哈希或录制器完成事件"
            if not any(event.get("event") == "finished"
                       and (event.get("demonstration") or {}).get("interactionMode") == "failure"
                       and (event.get("demonstration") or {}).get("ok") is True
                       for event in events):
                return "录像未完整展示验收失败证据"
            page = Path(str(attempt.get("path") or "")).with_suffix(".failure.html")
            if not page.is_file() or "验收失败证据录像" not in page.read_text(encoding="utf-8"):
                return "失败证据页面缺失或未清楚标注失败性质"
            other = self.db.one(
                "SELECT path,sha256 FROM recordings WHERE pair_id=? AND arm<>? AND status='passed'",
                (attempt.get("pair_id"), attempt.get("arm")),
            ) or {}
            if other.get("path") == attempt.get("path") or (
                other.get("sha256") and other["sha256"] == result["sha256"]
            ):
                return "A/B 录像文件或内容完全相同"
            return ""
        if check.get("status") != "passed" and not self._functional_page_with_private_failure(check):
            return "产物验收未通过，不能保存为合格录像"
        if any(event.get("demonstration", {}).get("interactionMode") == "failure"
               for event in events if isinstance(event.get("demonstration"), dict)):
            return "失败证据画面不能保存为合格功能录像"
        if not result.get("sha256"):
            return "录像缺少文件哈希"
        if not any(event.get("event") == "finished" for event in events):
            return "录像缺少录制器完成事件"
        if require_interaction and not any(
            event.get("event") == "interaction" and event.get("ok") is True for event in events
        ):
            return "录像缺少可核验的成功操作事件"
        if require_interaction:
            api_operations = [event for event in events if event.get("event") == "interaction"
                              and (event.get("method") or event.get("workflow") in
                                   ("swagger-business-operations", "bare-json-api-operations"))]
            if api_operations:
                def business_success(event):
                    path = str(event.get("path") or "").rstrip("/") or "/"
                    return (event.get("ok") is True and path not in ("/", "/docs", "/openapi.json")
                            and not re.search(r"(?:^|/)(?:health|ready|live)(?:/|$)", path, re.I))

                if not any(business_success(event) for event in api_operations
                           if event.get("method")) and not any(
                    business_success(operation)
                    for event in api_operations for operation in event.get("operations", [])
                    if isinstance(operation, dict)
                ):
                    return "自动录像没有成功完成业务接口请求"
            interaction_issue = recording_interaction_issue(
                events, str(attempt.get("interaction_mode") or "auto")
            )
            if interaction_issue:
                return interaction_issue
            if attempt.get("interaction_mode") == "auto" and any(
                event.get("event") == "interaction"
                and event.get("workflow") == "browser-ui"
                and event.get("controlsComplete") is not True
                for event in events
            ):
                return "自动录像未遍历完安全功能和结果控件"
        if attempt.get("interaction_mode") == "auto" and any(
            event.get("event") == "finished" and event.get("reason") == "maximum_duration"
            for event in events
        ):
            return "自动录像达到时限但未完成业务操作与安全控件遍历"
        other = self.db.one(
            "SELECT path,sha256 FROM recordings WHERE pair_id=? AND arm<>? AND status='passed'",
            (attempt.get("pair_id"), attempt.get("arm")),
        ) or {}
        if other.get("path") == attempt.get("path") or (
            other.get("sha256") and other["sha256"] == result["sha256"]
        ):
            return "A/B 录像文件或内容完全相同，未保存为合格录像"
        return ""

    def _promote(self, attempt_id: str,
                 api_demo: Optional[Dict[str, Any]] = None) -> None:
        row = self.db.one("SELECT * FROM recording_attempts WHERE id=?", (attempt_id,)) or {}
        stamp = now_iso()
        steps = []
        if api_demo:
            raw_steps = api_demo.get("steps")
            steps = raw_steps if isinstance(raw_steps, list) else [api_demo]
        steps_json = json.dumps(steps, ensure_ascii=False)
        reviewer = str(self.db.setting("git_author_name", "刘昱") or "刘昱").strip() + "（按授权默认确认）"
        recording_id = "rec-" + str(row.get("pair_id", ""))[-8:] + str(row.get("arm", "")).lower()
        self.db.execute(
            """INSERT INTO recordings(id,pair_id,arm,path,sha256,width,height,duration_seconds,commit_sha,
               started_at,finished_at,steps_json,attempt_id,capture_mode,entry_url,commit_match,review_status,reviewed_by,
               reviewed_at,status,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'confirmed',?,?,'passed','',?,?)
               ON CONFLICT(pair_id,arm) DO UPDATE SET path=excluded.path,sha256=excluded.sha256,
               width=excluded.width,height=excluded.height,duration_seconds=excluded.duration_seconds,
               commit_sha=excluded.commit_sha,started_at=excluded.started_at,finished_at=excluded.finished_at,
               steps_json=excluded.steps_json,attempt_id=excluded.attempt_id,
               capture_mode=excluded.capture_mode,entry_url=excluded.entry_url,
               commit_match=1,review_status='confirmed',reviewed_by=excluded.reviewed_by,
               reviewed_at=excluded.reviewed_at,status='passed',error='',updated_at=excluded.updated_at""",
            (recording_id, row["pair_id"], row["arm"], row["path"], row["sha256"], row["width"], row["height"],
             row["duration_seconds"], row["commit_sha"], row["started_at"], row["finished_at"], steps_json, attempt_id,
             row["capture_mode"], row["entry_url"], reviewer, stamp, stamp, stamp),
        )
        required = self.db.one(
            """SELECT COUNT(*) count FROM artifact_checks c
                 JOIN arm_runs a ON a.pair_id=c.pair_id AND a.arm=c.arm AND a.commit_sha=c.commit_sha
                WHERE c.pair_id=? AND c.status='passed' AND c.id=(
                  SELECT latest.id FROM artifact_checks latest
                   WHERE latest.pair_id=c.pair_id AND latest.arm=c.arm
                     AND latest.commit_sha=c.commit_sha
                   ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1
                )""",
            (row["pair_id"],),
        )
        recorded = self.db.one(
            """SELECT COUNT(*) count FROM recordings r
                 JOIN arm_runs a ON a.pair_id=r.pair_id AND a.arm=r.arm AND a.commit_sha=r.commit_sha
                WHERE r.pair_id=? AND r.status='passed' AND r.commit_match=1""",
            (row["pair_id"],),
        )
        required_count = int((required or {}).get("count") or 0)
        # Old imported rows and a few recovery paths predate artifact_checks;
        # retain their historical two-video completion rule.
        target_count = required_count if required_count > 0 else 2
        arms = self.db.all(
            "SELECT status FROM arm_runs WHERE pair_id=? ORDER BY arm",
            (row["pair_id"],),
        )
        pair_development_complete = len(arms) == 2 and all(
            str(arm.get("status") or "") == "completed" for arm in arms
        )
        if (pair_development_complete
                and int((recorded or {}).get("count") or 0) >= target_count):
            review = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (row["pair_id"],))
            review_confirmed = bool(review and review.get("status") == "confirmed")
            if review and not review_confirmed:
                self.db.execute("DELETE FROM gsb_rechecks WHERE pair_id=?", (row["pair_id"],))
                self.db.execute("UPDATE gsb_reviews SET status='draft',confirmed_by='',confirmed_at=NULL,updated_at=? WHERE pair_id=?", (stamp, row["pair_id"]))
            if review_confirmed:
                # Re-recording or failure-evidence capture only replaces media
                # for the same delivered commits.  It can restore submission
                # eligibility without invalidating a confirmed comparison.
                failed_checks = self.db.all(
                    """SELECT c.arm FROM artifact_checks c
                         JOIN arm_runs a ON a.pair_id=c.pair_id AND a.arm=c.arm
                                        AND a.commit_sha=c.commit_sha
                        WHERE c.pair_id=? AND c.status='observed_failed' ORDER BY c.arm""",
                    (row["pair_id"],),
                )
                failed_arms = [str(item.get("arm") or "") for item in failed_checks]
                completion_note = (
                    "原始交付的 Docker/测试验收失败，已保存短录像并按轨迹完成 GSB："
                    + "、".join(failed_arms)
                    if failed_arms else ""
                )
                self.db.execute(
                    """UPDATE delivery_submissions SET
                         status=CASE WHEN remote_id='' THEN 'ready_to_submit' ELSE status END,
                         error='',updated_at=? WHERE pair_id=?""",
                    (stamp, row["pair_id"]),
                )
                self.db.execute(
                    """UPDATE pairs SET status='completed',stage='completed',winner=?,error=?,
                       completed_at=COALESCE(completed_at,?),updated_at=? WHERE id=?""",
                    (review.get("verdict", ""), completion_note, stamp, stamp, row["pair_id"]),
                )
            else:
                if review:
                    self.db.execute(
                        "UPDATE delivery_submissions SET status='needs_review',updated_at=? WHERE pair_id=?",
                        (stamp, row["pair_id"]),
                    )
                self.db.execute(
                    """UPDATE pairs SET status='running',stage='gsb_ready',winner='',error='',
                       completed_at=NULL,updated_at=? WHERE id=?""",
                    (stamp, row["pair_id"]),
                )
                lineage = self.db.one(
                    """SELECT p.chain_id,t.task_type FROM pairs p JOIN tasks t ON t.id=p.task_id WHERE p.id=?""",
                    (row["pair_id"],),
                ) or {}
                if lineage.get("task_type") in ("feature", "bugfix"):
                    self.db.execute(
                        "UPDATE project_chains SET status='active',followup_completed=0,completed_at=NULL,updated_at=? WHERE id=?",
                        (stamp, lineage.get("chain_id")),
                    )

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    @staticmethod
    def _published_port(base, workspace: Path, env, preferred_ports=None) -> int:
        result = run_command(base + ["ps", "--format", "json"], cwd=workspace, check=False, timeout=60, env=env)
        try:
            payload = json.loads(result.stdout)
            rows = payload if isinstance(payload, list) else [payload]
        except ValueError:
            rows = []
            for line in result.stdout.splitlines():
                try: rows.append(json.loads(line))
                except ValueError: pass
        preferred = {"web": 0, "frontend": 1, "ui": 2, "client": 3,
                     "api": 4, "app": 5, "server": 6, "backend": 7}
        def service_priority(row):
            name = str(row.get("Service") or "").casefold()
            if name in preferred:
                return preferred[name]
            if name.startswith("api"):
                return preferred["api"]
            return 100
        rows.sort(key=service_priority)
        published = []
        for row in rows:
            for item in row.get("Publishers") or []:
                value = int(item.get("PublishedPort") or 0)
                if not value:
                    continue
                target = int(item.get("TargetPort") or 0)
                protocol = str(item.get("Protocol") or "").casefold()
                published.append((value, target, protocol))

        # The isolated environment allocates several generic variables, but a
        # Compose file may publish only API_PORT.  Match the preferred values
        # against ports Docker actually published instead of blindly using the
        # first allocated variable.  This also prevents a combined DNS/HTTP
        # edge service from selecting its TCP/UDP port 53 as a browser entry.
        usable = [
            item for item in published
            if item[1] != 53 and item[2] != "udp"
        ]
        for preferred_port in preferred_ports or []:
            try:
                preferred_port = int(preferred_port or 0)
            except (TypeError, ValueError):
                continue
            if preferred_port and any(item[0] == preferred_port for item in usable):
                return preferred_port
        if usable:
            return usable[0][0]
        if published:
            return published[0][0]
        return 0

    @staticmethod
    def _entry_matches_project_category(category: str, entry_url: str) -> bool:
        if str(category or "") not in ("全栈", "纯前端"):
            return True
        path = urlparse(str(entry_url or "")).path.rstrip("/") or "/"
        return not (
            path == "/docs"
            or path.startswith("/docs/")
            or bool(re.fullmatch(r"/(?:healthz?|ready|live)", path, re.IGNORECASE))
        )

    @staticmethod
    def _wait_for_url(port: int, require_frontend: bool = False) -> str:
        # Prefer a real browser UI, but accept a health endpoint for pure API
        # artifacts.  The recorder overlays a same-origin business request on
        # that page so the video still demonstrates real functionality. Swagger
        # is probed separately first and is therefore omitted from this fallback.
        paths = (("/", "/index.html") if require_frontend else (
            "/", "/index.html", "/healthz", "/health", "/ready", "/live",
            "/health/ready", "/health/live", "/api/health", "/v1/health",
            "/v1/health/ready", "/v1/health/live",
        ))
        deadline = time.monotonic() + 120
        last = ""
        while time.monotonic() < deadline:
            # FastAPI commonly serves a small JSON object at `/` and the real
            # interactive surface at `/docs`.  Recording the first 200 response
            # therefore captured an inert JSON page even though Swagger was
            # available.  Prefer `/docs` only when it is actually Swagger so a
            # SPA that rewrites every path to index.html still opens at `/`.
            if not require_frontend:
                docs_url = "http://127.0.0.1:%d/docs" % port
                try:
                    response = urlopen(
                        Request(docs_url, headers={"User-Agent": "PairwiseRecorder/1.0"}),
                        timeout=3,
                    )
                    content_type = str(response.headers.get("Content-Type") or "").casefold()
                    body = response.read(256 * 1024).decode("utf-8", errors="ignore").casefold()
                    if response.status < 400 and "text/html" in content_type and any(
                        marker in body for marker in ("swagger-ui", "swagger ui", "openapi.json")
                    ):
                        resolved = response.geturl()
                        return resolved if isinstance(resolved, str) and resolved else docs_url
                except HTTPError as exc:
                    last = str(exc)
                except (URLError, OSError) as exc:
                    last = str(exc)
            for path in paths:
                url = "http://127.0.0.1:%d%s" % (port, path)
                try:
                    response = urlopen(Request(url, headers={"User-Agent": "PairwiseRecorder/1.0"}), timeout=3)
                    if response.status < 400:
                        if require_frontend:
                            content_type = str(response.headers.get("Content-Type") or "").casefold()
                            if "text/html" not in content_type:
                                last = "%s 不是 HTML 前端" % url
                                continue
                        resolved = response.geturl()
                        return resolved if isinstance(resolved, str) and resolved else url
                except HTTPError as exc:
                    last = str(exc)
                except (URLError, OSError) as exc:
                    last = str(exc)
            time.sleep(2)
        raise RuntimeError("演示项目没有可用的浏览器入口：%s" % last)

    @staticmethod
    def _discover_alternative_read_only_demo(workspace: Path, primary: Dict[str, Any],
                                              entry_url: str) -> Optional[Dict[str, Any]]:
        """Append a second documented, successful GET only when it really exists.

        A failed probe or an app with only one runnable operation keeps the
        original A/B workflow; recording never fabricates a distinguishing call.
        """
        try:
            readme = (workspace / "README.md").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        existing = {
            (str(step.get("method") or "").lower(), str(step.get("path") or ""))
            for step in (primary.get("steps") if isinstance(primary.get("steps"), list) else [primary])
            if isinstance(step, dict)
        }
        candidates = re.findall(r"\bGET\s*(?:\|\s*)?`?(/[A-Za-z0-9._~!$&'()*+,;=:@%/-]+)`?", readme, re.I)
        seen = set()
        parsed_entry = urlparse(entry_url)
        origin = "%s://%s" % (parsed_entry.scheme, parsed_entry.netloc)
        for raw_path in candidates:
            path = raw_path.rstrip(".,;:，。；：）)")
            if (not path.startswith("/") or path.startswith("//") or path in seen
                    or ("get", path) in existing
                    or re.search(r"/(?:healthz?|ready|live|docs)(?:/|$)", path, re.I)):
                continue
            seen.add(path)
            try:
                with urlopen(Request(origin + path, headers={"Accept": "application/json"}), timeout=3) as response:
                    if 200 <= response.status < 300:
                        return {"path": path, "method": "get", "headers": {"accept": "application/json"}, "body": None}
            except (HTTPError, URLError, OSError):
                continue
        return None

    @staticmethod
    def _discover_api_demo(workspace: Path, task_prompt: str = "") -> Optional[Dict[str, Any]]:
        """Return a deterministic real request for a documented API-only app.

        Prefer the endpoint named in the task and its README curl example.  This
        keeps recordings focused on the newly delivered feature and avoids
        manufacturing an invalid body from an underspecified OpenAPI schema.
        API-only submissions do not always ship Swagger, so the documented
        Excellon request remains as a conservative compatibility fallback.
        """
        readme = workspace / "README.md"
        try:
            text = readme.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            # A runnable acceptance suite is still authoritative evidence for
            # API-only artifacts that intentionally omit prose documentation.
            text = ""
        targets = re.findall(
            r"\b(POST|PUT|PATCH|GET)\s+`?(/[A-Za-z0-9._~!$&'()*+,;=:@%/?{}-]+)`?",
            str(task_prompt or ""),
            re.IGNORECASE,
        )
        # Bug prompts often name the production function instead of repeating
        # its HTTP route.  Prefer a documented endpoint whose final path token
        # appears in that function name (for example trace_polyline -> /trace).
        prompt_lower = str(task_prompt or "").casefold()
        for method, path in re.findall(
            r"(?im)^#{1,6}\s+`?(POST|PUT|PATCH|GET)\s+(/[A-Za-z0-9._~!$&'()*+,;=:@%/?{}-]+)`?\s*$",
            text,
        ):
            leaf = path.rstrip("/").rsplit("/", 1)[-1].casefold()
            candidate = (method.upper(), path)
            if len(leaf) >= 4 and leaf in prompt_lower and candidate not in targets:
                targets.append(candidate)
        blocks = re.findall(r"```[^\r\n]*\r?\n(.*?)```", text, re.DOTALL)

        def documented_example_kind(context: str, body: Any) -> str:
            nearby = str(context or "").casefold()
            if re.search(r"(?:请求|request|input|body)[^\n]{0,40}[:：]?\s*$", nearby):
                return "request"
            if re.search(r"(?:响应|response|output)[^\n]{0,50}[:：]?\s*$", nearby):
                return "response"
            if isinstance(body, dict) and (
                {"primary", "witness", "unique"} <= set(body)
                or {"status", "result"} <= set(body)
            ):
                return "response"
            return "unknown"

        def referenced_json_body(command: str):
            """Load a project-owned JSON body referenced by curl --data @file."""
            match = re.search(
                r"(?:-d|--data(?:-raw)?)\s+@['\"]?([^'\"\s\\]+)",
                command,
                re.IGNORECASE,
            )
            if not match:
                return None
            candidate = (workspace / match.group(1)).resolve()
            root = workspace.resolve()
            if root != candidate and root not in candidate.parents:
                return None
            try:
                if candidate.suffix.casefold() != ".json" or candidate.stat().st_size > 1_000_000:
                    return None
                value = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                return None
            return value if isinstance(value, (dict, list)) else None

        for method, raw_path in targets:
            path = raw_path.rstrip(".,;:，。；：")
            for block in blocks:
                if path not in block or not re.search(r"\bcurl\b", block, re.IGNORECASE):
                    continue
                body_match = re.search(
                    r"(?:-d|--data(?:-raw)?)\s+'(\{.*\}|\[.*\])'",
                    block,
                    re.IGNORECASE | re.DOTALL,
                )
                if body_match:
                    try:
                        body = json.loads(body_match.group(1))
                    except (TypeError, ValueError):
                        continue
                else:
                    body = referenced_json_body(block)
                    if body is None:
                        continue
                content_type = re.search(
                    r"Content-Type:\s*([^'\"\\\r\n]+)", block, re.IGNORECASE
                )
                return {
                    "path": path,
                    "method": method.casefold(),
                    "headers": {
                        "content-type": (
                            content_type.group(1).strip()
                            if content_type
                            else "application/json"
                        )
                    },
                    "body": body,
                }
            heading = re.search(
                r"(?im)^#{1,6}\s+`?%s\s+%s`?\s*$"
                % (re.escape(method), re.escape(path)),
                text,
            )
            if heading:
                section = text[heading.end():]
                next_heading = re.search(r"(?m)^#{1,6}\s+", section)
                if next_heading:
                    section = section[:next_heading.start()]
                body_match = re.search(
                    r"```json\s*\r?\n(\{.*?\}|\[.*?\])\s*```",
                    section,
                    re.IGNORECASE | re.DOTALL,
                )
                if body_match:
                    try:
                        body = json.loads(body_match.group(1))
                    except (TypeError, ValueError):
                        body = None
                    if body is not None:
                        context = section[max(0, body_match.start() - 240):body_match.start()]
                        if documented_example_kind(context, body) == "response":
                            continue
                        return {
                            "path": path,
                            "method": method.casefold(),
                            "headers": {"content-type": "application/json"},
                            "body": body,
                        }

        # Some backend-only tasks describe the feature in prose instead of
        # naming its HTTP route.  In that case, use a documented local curl
        # workflow as a deterministic end-to-end demo.  Keeping the calls in
        # README order matters for stateful APIs (for example, create a device
        # before updating its policy).
        workflows = []
        for block in blocks:
            normalized = re.sub(r"\\\s*\r?\n\s*", " ", block)
            commands = re.findall(
                r"(?ims)(?:^|\n)\s*(curl\b.*?)(?=\n\s*curl\b|\Z)",
                normalized,
            )
            steps = []
            for command in commands:
                url_match = re.search(
                    r"(?:https?://)?(?:localhost|127\.0\.0\.1)"
                    r"(?::(?:\d+|\$\{[^}\s]+\}|\$[A-Za-z_][A-Za-z0-9_]*))?"
                    r"(?P<path>/[^\s'\"]*)",
                    command,
                    re.IGNORECASE,
                )
                if not url_match:
                    continue
                path = url_match.group("path").rstrip(".,;:，。；：")
                if re.search(r"/(?:healthz?|ready|live)(?:[/?]|$)", path, re.IGNORECASE):
                    continue
                method_match = re.search(r"(?:^|\s)-X\s+([A-Z]+)\b", command, re.IGNORECASE)
                method = method_match.group(1).casefold() if method_match else "get"
                body = None
                body_match = re.search(
                    r"(?:-d|--data(?:-raw)?)\s+'(\{.*?\}|\[.*?\])'",
                    command,
                    re.IGNORECASE | re.DOTALL,
                )
                if body_match:
                    try:
                        body = json.loads(body_match.group(1))
                    except (TypeError, ValueError):
                        continue
                else:
                    body = referenced_json_body(command)
                # curl sends POST when -d/--data is present even without -X.
                # Recording that command as GET loses the README fixture and
                # falls back to a synthetic Swagger body that may be invalid.
                if not method_match and re.search(r"(?:^|\s)(?:-d|--data(?:-raw)?)\s", command, re.I):
                    method = "post"
                content_type = re.search(
                    r"Content-Type:\s*([^'\"\\\r\n]+)", command, re.IGNORECASE
                )
                steps.append({
                    "path": path,
                    "method": method,
                    "headers": {
                        "content-type": (
                            content_type.group(1).strip()
                            if content_type
                            else "application/json"
                        )
                    },
                    "body": body,
                })
            # A documented workflow can be a single business request.  This is
            # common for solver-style APIs whose README submits one complete
            # fixture with ``--data @examples/request.json``.  Health requests
            # are filtered above, so retaining one step is still a real feature
            # demonstration rather than a readiness-only recording.
            if steps:
                workflows.append(steps)
        if workflows:
            workflow = max(workflows, key=len)[:5]
            return workflow[0] if len(workflow) == 1 else {"steps": workflow}

        # Some API-only projects document the business POST route in a table
        # and place its request example in a following "Request" section, or
        # put both directly under a route heading.  Prefer that runnable body
        # over a harmless version-info GET and over literals found in tests.
        raw_documented_posts = re.findall(
            r"\bPOST\s*(?:\|\s*)?`?(/[^\s`|]+)", text, re.IGNORECASE
        )
        documented_posts = []
        for raw_path in raw_documented_posts:
            path = raw_path.rstrip(".,;:，。；：）)")
            if (
                path
                and "{" not in path
                and "}" not in path
                and not re.search(r"/(?:healthz?|ready|live)(?:[/?]|$)", path, re.IGNORECASE)
                and path not in documented_posts
            ):
                documented_posts.append(path)

        for path in documented_posts:
            heading = re.search(
                r"(?im)^#{1,6}\s+`?POST\s+%s`?\s*$" % re.escape(path), text
            )
            if not heading:
                continue
            section = text[heading.end():]
            next_heading = re.search(r"(?m)^#{1,6}\s+", section)
            if next_heading:
                section = section[:next_heading.start()]
            body_match = re.search(
                r"```json\s*\r?\n(\{.*?\}|\[.*?\])\s*```",
                section,
                re.IGNORECASE | re.DOTALL,
            )
            if body_match:
                try:
                    body = json.loads(body_match.group(1))
                except (TypeError, ValueError):
                    continue
                context = section[max(0, body_match.start() - 240):body_match.start()]
                if documented_example_kind(context, body) == "response":
                    continue
                return {
                    "path": path,
                    "method": "post",
                    "headers": {"content-type": "application/json"},
                    "body": body,
                }
        static_post_contract_is_unambiguous = len({
            raw_path.rstrip(".,;:，。；：）)") for raw_path in raw_documented_posts
        }) == 1
        if len(documented_posts) == 1 and static_post_contract_is_unambiguous:
            examples = []
            for match in re.finditer(
                r"```json\s*\r?\n(\{.*?\}|\[.*?\])\s*```",
                text,
                re.IGNORECASE | re.DOTALL,
            ):
                try:
                    body = json.loads(match.group(1))
                except (TypeError, ValueError):
                    continue
                context = text[max(0, match.start() - 240):match.start()].casefold()
                kind = documented_example_kind(context, body)
                score = 10 if kind == "request" else (-20 if kind == "response" else 0)
                examples.append((score, -match.start(), body))
            if examples:
                score, _, body = max(examples, key=lambda item: (item[0], item[1]))
                if score > 0:
                    return {
                        "path": documented_posts[0],
                        "method": "post",
                        "headers": {"content-type": "application/json"},
                        "body": body,
                    }

        # A backend-only artifact may document its API contract without
        # including runnable curl examples.  In that case it is still safe to
        # demonstrate a parameterless, read-only business endpoint.  Do not
        # guess path/query values and never fall back to a mutating request.
        documented_gets = []
        for raw_path in re.findall(
            r"\bGET\s*(?:\|\s*)?`?(/[^\s`|]+)", text, re.IGNORECASE
        ):
            path = raw_path.rstrip(".,;:，。；：）)")
            parsed = urlparse(path)
            route = parsed.path
            if (
                not route
                or parsed.query
                or "{" in path
                or "}" in path
                or "<" in path
                or ">" in path
                or re.search(
                    r"/(?:docs(?:/.*)?|healthz?|ready|live|health/(?:ready|live)|"
                    r"(?:api|v\d+)/health(?:/(?:ready|live))?)/?$",
                    route,
                    re.IGNORECASE,
                )
            ):
                continue
            score = 0
            if re.search(r"/(?:status|summary|stats|metrics)$", route, re.IGNORECASE):
                score += 40
            if re.search(r"s$", route, re.IGNORECASE):
                score += 10
            score -= len(route) / 1000
            documented_gets.append((score, route))
        if documented_gets:
            _, path = max(documented_gets, key=lambda item: item[0])
            return {
                "path": path,
                "method": "get",
                "headers": {"accept": "application/json"},
                "body": None,
            }

        python_example = RecordingManager._discover_python_api_example(workspace)
        if python_example:
            return python_example

        javascript_example = RecordingManager._discover_javascript_api_example(workspace)
        if javascript_example:
            return javascript_example

        match = re.search(
            r"(?im)^#{1,6}\s+`?POST\s+(/drill-files/statistics(?:\?[^\s`]*)?)`?\s*$",
            text,
        )
        if not match or "M48" not in text or "METRIC" not in text:
            return None
        return {
            "path": match.group(1),
            "method": "post",
            "headers": {"content-type": "text/plain"},
            "body": "M48\nMETRIC\nT01C0.300\n%\nT01\nX1.500Y2.250\nX-0.500Y2.250\nM30\n",
        }

    @staticmethod
    def _discover_javascript_api_example(workspace: Path) -> Optional[Dict[str, Any]]:
        """Use a literal request from a project-owned JS acceptance test.

        This deliberately accepts only a small JSON-like subset.  Expressions,
        template substitutions and imported fixtures are ignored so recording
        never guesses a mutating payload.  The request may exercise structured
        business validation; it is still preferable to a health-only video and
        mirrors an assertion the submitted project itself owns.
        """

        def literal_body(raw: str):
            if any(marker in raw for marker in ("`", "...", "=>", "new ")):
                return None
            normalized = re.sub(
                r"([,{]\s*)([A-Za-z_$][A-Za-z0-9_$]*)(\s*:)",
                r"\1'\2'\3",
                raw,
            )
            normalized = re.sub(r"\btrue\b", "True", normalized)
            normalized = re.sub(r"\bfalse\b", "False", normalized)
            normalized = re.sub(r"\bnull\b", "None", normalized)
            try:
                value = ast.literal_eval(normalized)
                encoded = json.dumps(value, ensure_ascii=False)
            except (SyntaxError, ValueError, TypeError):
                return None
            if not isinstance(value, (dict, list)) or len(encoded) > 5_000:
                return None
            return value

        candidates = []
        for folder_name in ("acceptance", "tests", "test", "verify"):
            folder = workspace / folder_name
            if not folder.is_dir():
                continue
            paths = []
            for suffix in ("*.js", "*.mjs", "*.cjs", "*.ts"):
                paths.extend(folder.rglob(suffix))
            for path in sorted(set(paths))[:80]:
                try:
                    if path.stat().st_size > 2_000_000:
                        continue
                    source = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                # Common acceptance helpers use call(method, path, { body }).
                # Limit the body to a non-nested object here; richer examples
                # remain covered by README JSON and Python literal discovery.
                pattern = re.compile(
                    r"\b(?:"
                    r"(?:api|request)\s*\(\s*|"
                    r"call\s*\(\s*[^,\r\n]{1,200},\s*"
                    r")['\"]"
                    r"(POST|PUT|PATCH)['\"]\s*,\s*['\"]"
                    r"(/[^'\"`{}<>]+)['\"]\s*,\s*\{[^{}]{0,800}?"
                    r"\bbody\s*:\s*(\{[^{}]{0,4000}\})",
                    re.IGNORECASE | re.DOTALL,
                )
                for match in pattern.finditer(source):
                    route = urlparse(match.group(2)).path
                    if (
                        not route.startswith("/")
                        or re.search(r"/(?:healthz?|ready|live)(?:/|$)", route, re.IGNORECASE)
                    ):
                        continue
                    body = literal_body(match.group(3))
                    if (
                        body is None
                        and route == "/v1/devices"
                        and re.search(r"\bdeviceId\s*:", match.group(3))
                        and re.search(r"\bpublicKey\s*:", match.group(3))
                    ):
                        # Registration tests commonly create an Ed25519 key at
                        # runtime.  Use the public key from RFC 8032's first
                        # test vector so the exact submitted request shape can
                        # still be exercised without copying executable test
                        # setup into the browser recorder.
                        body = {
                            "deviceId": "recording-demo-device",
                            "publicKey": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
                        }
                    if body is None:
                        continue
                    empty_values = sum(
                        1 for value in (body.values() if isinstance(body, dict) else body)
                        if value in ("", None)
                    )
                    candidates.append((empty_values, path.as_posix(), match.start(), {
                        "path": route,
                        "method": match.group(1).casefold(),
                        "headers": {"content-type": "application/json"},
                        "body": body,
                    }))
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item[0], item[1], item[2]))[3]

    @staticmethod
    def _discover_python_api_example(workspace: Path) -> Optional[Dict[str, Any]]:
        """Use a literal request from a project-owned acceptance test.

        Backend submissions sometimes document routes only in a Markdown table
        while keeping the runnable request bodies in their acceptance suite.
        Reading a literal ``json=`` payload avoids inventing a mutating request
        and lets the recording demonstrate the same call the project tests.
        """
        for folder_name in ("acceptance", "tests"):
            folder = workspace / folder_name
            if not folder.is_dir():
                continue
            for path in sorted(folder.rglob("*.py"))[:50]:
                try:
                    if path.stat().st_size > 2_000_000:
                        continue
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, SyntaxError):
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or not node.args:
                        continue
                    func = node.func
                    method = (
                        str(func.attr).casefold()
                        if isinstance(func, ast.Attribute) else ""
                    )
                    if method not in ("post", "put", "patch"):
                        continue
                    fragments = [
                        str(item.value) for item in ast.walk(node.args[0])
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    ]
                    raw_path = next((item for item in fragments if "/" in item), "")
                    route = urlparse(raw_path).path if raw_path else ""
                    if (
                        not route.startswith("/")
                        or any(marker in route for marker in ("{", "}", "<", ">"))
                        or re.search(r"/(?:healthz?|ready|live)(?:/|$)", route, re.IGNORECASE)
                    ):
                        continue
                    body_node = next(
                        (item.value for item in node.keywords if item.arg == "json"), None,
                    )
                    if body_node is None:
                        continue
                    try:
                        body = ast.literal_eval(body_node)
                        encoded = json.dumps(body, ensure_ascii=False)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(body, (dict, list)) or len(encoded) > 5_000:
                        continue
                    return {
                        "path": route,
                        "method": method,
                        "headers": {"content-type": "application/json"},
                        "body": body,
                    }
        return None

    @staticmethod
    def _discover_upload_fixture(workspace: Path) -> Optional[Path]:
        """Pick a valid project-owned JSON sample for a browser file input."""
        skip_dirs = {
            ".git", "node_modules", ".venv", "venv", "dist", "build",
            "coverage", "test-results", "__pycache__",
        }
        ignored_names = {
            "package.json", "package-lock.json", "tsconfig.json", "tsconfig.app.json",
            "tsconfig.node.json", ".last-run.json",
        }
        candidates = []
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [name for name in dirs if name not in skip_dirs]
            for name in files:
                lowered_name = name.casefold()
                if not lowered_name.endswith(".json") or lowered_name in ignored_names:
                    continue
                path = Path(root) / name
                try:
                    if path.stat().st_size <= 1 or path.stat().st_size > 1024 * 1024:
                        continue
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError):
                    continue
                if not isinstance(value, (dict, list)):
                    continue
                relative = str(path.relative_to(workspace)).casefold()
                score = 0
                if re.search(r"(?:^|/)(?:fixtures?|samples?|examples?|demo)(?:/|$)", relative):
                    score += 80
                if re.search(r"isolation|order[-_]?trap", relative):
                    score += 80
                elif re.search(r"conflict|valid|sample|example|demo", relative):
                    score += 50
                if re.search(r"invalid|malformed|broken|error|failure", relative):
                    score -= 120
                if isinstance(value, list) and value:
                    score += 15
                score -= len(relative) / 10000
                candidates.append((score, relative, path))
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[0], item[1]))[2]


def inspect_recording(path: Path) -> Dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {"ok": False, "error": "录像文件不存在或为空"}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = run_command([
        "mdls", "-plist", "-name", "kMDItemDurationSeconds", "-name", "kMDItemPixelWidth",
        "-name", "kMDItemPixelHeight", str(path),
    ], check=False, timeout=60)
    if result.returncode != 0:
        return _inspect_with_ffprobe(path, digest, redact(result.stderr or result.stdout))
    try:
        metadata = plistlib.loads(result.stdout.encode("utf-8"))
        width = int(metadata.get("kMDItemPixelWidth") or 0)
        height = int(metadata.get("kMDItemPixelHeight") or 0)
        duration = float(metadata.get("kMDItemDurationSeconds") or 0)
    except (ValueError, TypeError, plistlib.InvalidFileException) as exc:
        return _inspect_with_ffprobe(path, digest, "无法读取 Spotlight 录像规格：%s" % exc)
    if not width or not height or not duration:
        return _inspect_with_ffprobe(path, digest, "Spotlight 录像元数据尚未生成")
    ok = width == 1280 and height == 720 and 0 < duration < 90
    return {
        "ok": ok, "sha256": digest, "width": width, "height": height,
        "duration_seconds": round(duration, 3),
        "error": "" if ok else "录像必须为 1280×720 且少于 90 秒",
    }


def _inspect_with_ffprobe(path: Path, digest: str, prior_error: str) -> Dict[str, Any]:
    media_info = run_command(["/usr/bin/avmediainfo", str(path)], check=False, timeout=60)
    if media_info.returncode == 0:
        dimensions = re.search(r"Dimensions:\s*(\d+)\s*x\s*(\d+)", media_info.stdout)
        duration_match = re.search(r"^Duration:\s*([\d.]+)\s+seconds", media_info.stdout, re.MULTILINE)
        if dimensions and duration_match:
            width, height = int(dimensions.group(1)), int(dimensions.group(2))
            duration = float(duration_match.group(1))
            ok = width == 1280 and height == 720 and 0 < duration < 90
            return {
                "ok": ok, "sha256": digest, "width": width, "height": height,
                "duration_seconds": round(duration, 3),
                "error": "" if ok else "录像必须为 1280×720 且少于 90 秒",
            }
    try:
        probe = run_command([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path),
        ], check=False, timeout=60)
    except FileNotFoundError:
        class MissingProbe:
            returncode, stdout, stderr = 127, "", "ffprobe 未安装"
        probe = MissingProbe()
    if probe.returncode != 0:
        candidates = sorted((Path.home() / "Library/Caches/ms-playwright").glob("ffmpeg-*/ffmpeg-mac"), reverse=True)
        if candidates:
            media = run_command([str(candidates[0]), "-i", str(path)], check=False, timeout=60)
            text = media.stderr + "\n" + media.stdout
            dimensions = re.search(r"\b(\d{3,5})x(\d{3,5})\b", text)
            duration_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
            if dimensions and duration_match:
                width, height = int(dimensions.group(1)), int(dimensions.group(2))
                duration = int(duration_match.group(1)) * 3600 + int(duration_match.group(2)) * 60 + float(duration_match.group(3))
                ok = width == 1280 and height == 720 and 0 < duration < 90
                return {"ok": ok, "sha256": digest, "width": width, "height": height,
                        "duration_seconds": round(duration, 3),
                        "error": "" if ok else "录像必须为 1280×720 且少于 90 秒"}
        return {"ok": False, "sha256": digest, "error": redact(probe.stderr or prior_error)}
    try:
        data = json.loads(probe.stdout)
        stream = (data.get("streams") or [{}])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "sha256": digest, "error": "无法读取录像规格：%s" % exc}
    ok = width == 1280 and height == 720 and 0 < duration < 90
    return {
        "ok": ok, "sha256": digest, "width": width, "height": height,
        "duration_seconds": round(duration, 3),
        "error": "" if ok else "录像必须为 1280×720 且少于 90 秒",
    }


def _normalize_to_720p(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    info = run_command(["/usr/bin/avmediainfo", str(path)], check=False, timeout=60)
    dimensions = re.search(r"Dimensions:\s*(\d+)\s*x\s*(\d+)", info.stdout) if info.returncode == 0 else None
    if dimensions and (int(dimensions.group(1)), int(dimensions.group(2))) == (1280, 720):
        return
    converted = path.with_name(path.stem + ".720p" + path.suffix)
    converted.unlink(missing_ok=True)
    result = run_command([
        "/usr/bin/avconvert", "--source", str(path), "--output", str(converted),
        "--preset", "Preset1280x720", "--replace",
    ], check=False, timeout=600)
    if result.returncode == 0 and converted.exists() and converted.stat().st_size:
        os.replace(converted, path)
    else:
        converted.unlink(missing_ok=True)
