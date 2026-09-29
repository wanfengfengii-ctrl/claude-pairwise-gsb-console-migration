import hashlib
import json
import mimetypes
import re
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .analytics import dashboard
from .artifact import ArtifactChecker, isolated_compose_environment
from .claude_runner import ClaudeRunner
from .classification import infer_project_stack, normalize_project_category, normalize_stack
from .codex_runner import (
    ACTUAL_DIFFICULTY_SCHEMA, BUG_DISCOVERY_SCHEMA, BUG_TASK_PROMPT_SCHEMA, CodexRunner,
    DELIVERY_ASSESSMENT_SCHEMA, GSB_RECHECK_SCHEMA, GSB_SCHEMA, TASK_SCHEMA,
)
from .config import Config, MAX_CLAUDE_TERMINALS, MAX_PAIR_PROJECTS
from .db import Database, now_iso
from .gitops import GitOps
from .gsb_rewrite import rewrite_preview, validate_source
from .importer import fingerprint, import_historical_tasks
from .prompts import (
    GENERATED_TASK_MAX_ESTIMATED_MINUTES,
    TASK_PROMPT_BROWSER_VERIFY_MARKERS, task_prompt_browser_policy_marker,
    actual_difficulty_review_prompt, bug_discovery_prompt, bugfix_task_prompt, feature_generation_prompt,
    delivery_assessment_prompt, generated_task_estimate_issues, generated_task_prompt_issues,
    repair_generated_task_punctuation,
    gsb_independent_recheck_prompt, gsb_prompt,
    gsb_recheck_prompt, task_generation_prompt,
    task_validation_prompt,
)
from .recording import RecordingManager
from .commands import redact, run_command
from .pipeline_state import operation_ready, operation_failed
from .bug_verification import clean_commands, validate_specs
from .resources import DOCKER_WORK
from .task_estimation import summarize_reviewed_estimate
import tempfile
import os


VALIDATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["accepted", "difficulty", "difficultyEvidence", "banned", "duplicate", "baselineReady", "reason"],
    "properties": {
        "accepted": {"type": "boolean"},
        "difficulty": {"type": "string", "enum": ["简单", "中等", "困难", "地狱"]},
        "difficultyEvidence": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "banned": {"type": "boolean"},
        "duplicate": {"type": "boolean"},
        "baselineReady": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 500},
    },
}
ESTIMATED_VALIDATION_SCHEMA = {
    **VALIDATION_SCHEMA,
    "required": [*VALIDATION_SCHEMA["required"], "workItems"],
    "properties": {
        **VALIDATION_SCHEMA["properties"],
        "workItems": {
            "type": "array", "minItems": 3, "maxItems": 8,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["name", "phase", "minMinutes", "maxMinutes", "basis"],
                "properties": {
                    "name": {"type": "string", "minLength": 2, "maxLength": 80},
                    "phase": {"type": "string", "enum": ["development", "docker_delivery"]},
                    "minMinutes": {"type": "integer", "minimum": 0, "maximum": 480},
                    "maxMinutes": {"type": "integer", "minimum": 0, "maximum": 480},
                    "basis": {"type": "string", "minLength": 4, "maxLength": 240},
                },
            },
        },
    },
}

BUG_SOURCE_SCAN_IGNORED_PARTS = {
    ".git", "node_modules", "dist", "build", ".next", "coverage",
    ".venv", "venv", "env", "site-packages", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    ".npm-cache",
}

BUG_SOURCE_REPAIR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "changedFiles"],
    "properties": {
        "summary": {"type": "string", "minLength": 10, "maxLength": 1000},
        "changedFiles": {
            "type": "array", "minItems": 1, "maxItems": 30,
            "items": {"type": "string", "maxLength": 300},
        },
    },
}

GSB_STEP_REFERENCE = re.compile(
    r"第\s*[一二三四五六七八九十百千万零〇\d]+"
    r"(?:\s*[、，,及和与]\s*[一二三四五六七八九十百千万零〇\d]+)*\s*步"
)
TASK_SOURCE_TYPES = ("bugfix", "feature", "zero_to_one")
ELIGIBLE_TASK_SQL = "difficulty IN ('困难','地狱')"
MAX_FEATURE_TASKS_PER_PROJECT = 3
MAX_FEATURE_GENERATION_ATTEMPTS_PER_PROJECT = 6
A9_REJECTED_PROMPT_FRAGMENTS = (
    "请修复该问题保留现有dockercompose启动与验收链路并补充覆盖复现路径的自动化验收",
)
RETIRED_BUG_PROMPT_FRAGMENTS = (
    "这个缺陷已在清洁环境中重复出现",
    "正确性要求是",
    "沿用项目当前的dockercompose启动方式自动化验收要重放从",
    "修复应保持已有dockercompose启动入口可用请把",
    "同时保留当前dockercompose启动流程新增验收需要从",
    "不改变项目现有的dockercompose使用方式回归验收要实际执行",
)

MANUAL_BUG_MIN_MODULES = 2
MANUAL_BUG_MAX_MODULES = 4
MANUAL_BUG_MIN_SOURCE_LINES = 80
MANUAL_BUG_MAX_SOURCE_LINES = 250
MANUAL_BUG_MIN_ESTIMATED_MINUTES = 45
MANUAL_BUG_TARGET_ESTIMATED_MINUTES = 90
MANUAL_BUG_MAX_ESTIMATED_MINUTES = 180


def task_difficulty_allowed(task_type: str, difficulty: str) -> bool:
    return difficulty in ("困难", "地狱")


def contains_browser_verification(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False).casefold()
    return any(marker in text for marker in TASK_PROMPT_BROWSER_VERIFY_MARKERS)


def _comparison_text(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


class PairwiseService:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.codex = CodexRunner(config, db)
        self.claude = ClaudeRunner(config, db)
        self.git = GitOps(config, db)
        self.artifacts = ArtifactChecker(db)
        self.recordings = RecordingManager(config, db)
        # Arm monitors are intentionally long lived.  Keeping them in the same
        # pool as user actions can starve GSB rechecks (and every other short
        # operation) whenever all A/B slots are occupied.
        self.executor = ThreadPoolExecutor(
            max_workers=max(8, config.task_generation_max_parallel + 2),
            thread_name_prefix="pairwise-operation",
        )
        self.monitor_executor = ThreadPoolExecutor(
            max_workers=max(8, MAX_PAIR_PROJECTS * 2 + 2),
            thread_name_prefix="pairwise-monitor",
        )
        self._future_lock = threading.Lock()
        self._bug_conversion_lock = threading.RLock()
        self._futures: Dict[str, Any] = {}
        self._scheduler_started = False
        self._automation_lock = threading.Lock()
        self._pair_creation_lock = threading.Lock()
        self._repository_locks_lock = threading.Lock()
        self._repository_locks: Dict[str, threading.Lock] = {}
        self._start_locks_lock = threading.Lock()
        self._start_locks: Dict[str, threading.Lock] = {}
        # Pair capacity and Claude-terminal capacity are separate.  A Pair may
        # remain active with one Arm queued until one of the three stable
        # development-terminal slots becomes available.
        self._arm_launch_lock = threading.Lock()
        self._prompt_locks_lock = threading.Lock()
        self._prompt_locks: Dict[str, threading.Lock] = {}
        self._failure_locks_lock = threading.Lock()
        self._failure_locks: Dict[str, threading.RLock] = {}
        self._pair_completion_lock = threading.RLock()
        self._checkpoint_locks_guard = threading.Lock()
        self._checkpoint_locks: Dict[str, threading.Lock] = {}
        self._arm_monitor_locks_guard = threading.Lock()
        self._arm_monitor_locks: Dict[str, threading.Lock] = {}
        self._auto_retry_after: Dict[str, float] = {}
        self._artifact_retry_after: Dict[str, float] = {}
        self._seed_settings()
        self._restore_old_scale_rejected_bug_tasks()
        self._retire_outdated_ready_bug_tasks()
        self._quarantine_invalid_completed_pairs()
        self._queue_completed_prompt_mismatch_pairs()
        self._restore_discarded_false_prompt_mismatches()
        self._queue_invalid_delivery_lineage_pairs()
        self._restore_false_completed_tasks()
        self._recover_interrupted_background_jobs()
        self._recover_verify_dependency_port_conflicts()
        self._quarantine_non_bug_preflight_recoveries()
        self._reopen_dependency_only_browser_scans()

    def _recover_interrupted_background_jobs(self) -> None:
        """Close process-local jobs that cannot survive a service restart."""
        stamp = now_iso()
        for resource in self.db.all("SELECT * FROM runtime_resources WHERE status='active'"):
            try:
                os.kill(int(resource["owner_pid"]), 0)
            except ProcessLookupError:
                self.db.execute("UPDATE runtime_resources SET status='needs_review',updated_at=? WHERE project=?",
                                (stamp, resource["project"]))
                self.db.audit("runtime.resource_owner_lost", "compose", resource["project"], {
                    "action": "inspect_before_cleanup", "runningContainersStopped": False,
                })
            except PermissionError:
                pass
        self.db.execute(
            """UPDATE arm_runs SET status='failed',
                 error='人工重排准备被服务重启中断，请重新点击该侧的重置并排队',updated_at=?
                 WHERE status='manual_preparing'""",
            (stamp,),
        )
        interrupted_batches = int((self.db.one(
            "SELECT COUNT(*) count FROM generation_batches WHERE status='running'"
        ) or {"count": 0})["count"])
        interrupted_reproductions = int((self.db.one(
            "SELECT COUNT(*) count FROM bug_candidates WHERE status='reproducing'"
        ) or {"count": 0})["count"])
        self.db.execute(
            """UPDATE codex_jobs SET status='failed',error='服务重启时作业仍处于运行态，已安全释放以便重新排队',
               finished_at=?,updated_at=? WHERE status='running'""",
            (stamp, stamp),
        )
        self.db.execute(
            """UPDATE generation_batches SET status='failed',
               error='服务重启时出题批次仍处于运行态，已结束陈旧状态并允许重新补题',
               finished_at=?,updated_at=? WHERE status='running'""",
            (stamp, stamp),
        )
        self.db.execute(
            """UPDATE bug_candidates SET status='awaiting_reproduction',
               error='服务重启中断了未完成的清洁环境复现，已释放为可重试',
               updated_at=? WHERE status='reproducing'""",
            (stamp,),
        )
        if interrupted_batches:
            self.db.audit("automation.generation_batches_recovered", "scheduler", "task-refill", {
                "count": interrupted_batches, "status": "failed", "retriable": True,
            })
        if interrupted_reproductions:
            self.db.audit("bug.reproductions_recovered", "scheduler", "bug-refill", {
                "count": interrupted_reproductions,
                "status": "awaiting_reproduction",
                "retriable": True,
            })

    def _reopen_dependency_only_browser_scans(self) -> int:
        """Reopen scans rejected solely by ignored dependency/cache files.

        Older builds walked local virtual environments.  A third-party SBOM
        mentioning a browser package could therefore mark a clean product Arm
        as browser-test dependent.  Preserve the original event and detail,
        but reclassify it so that the Arm becomes eligible for a fresh scan.
        """
        reopened = 0
        rows = self.db.all(
            """SELECT id,entity_id,detail_json FROM audit_events
                 WHERE event_type='bug.discovery_completed' AND entity_type='pair'"""
        )
        for row in rows:
            try:
                detail = json.loads(str(row.get("detail_json") or "{}"))
            except (TypeError, ValueError):
                continue
            rejected = [str(path) for path in (detail.get("browserRejected") or []) if str(path)]
            if detail.get("candidateIds") or not rejected:
                continue
            if not all(self._bug_source_scan_path_ignored(path) for path in rejected):
                continue
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE audit_events SET event_type='bug.discovery_false_positive'
                         WHERE id=? AND event_type='bug.discovery_completed'""",
                    (row["id"],),
                ).rowcount
            if not changed:
                continue
            reopened += 1
            self.db.audit("bug.discovery_reopened", "pair", str(row.get("entity_id") or ""), {
                "arm": str(detail.get("arm") or ""),
                "sourceEventId": row["id"],
                "reason": "旧扫描仅命中依赖环境或工具缓存，按新规则重新开放",
                "ignoredFiles": rejected,
            })
        return reopened

    def _recover_verify_dependency_port_conflicts(self) -> int:
        """Requeue baselines falsely rejected while Compose recreated dependencies.

        Older artifact checks ran the one-shot verifier with dependency
        reconciliation enabled even though every application service was
        already healthy.  Compose could then recreate the proxy and fail to
        bind the port still owned by its previous container.  No Claude Arm
        had started, so preserve the prepared Pair and retry its preflight
        after capacity becomes available.
        """
        recovered = 0
        rows = self.db.all(
            """SELECT e.id event_id,e.entity_id pair_id,e.detail_json,p.task_id,t.task_type
                 FROM audit_events e JOIN pairs p ON p.id=e.entity_id
                 JOIN tasks t ON t.id=p.task_id
                WHERE e.event_type='task.baseline_preflight_failed'
                  AND p.status='failed'
                  AND p.stage IN ('baseline_preflight_failed','replaced','replacement_failed')"""
        )
        manual_bug_only = bool(self.db.setting("manual_bug_only_mode", False))
        reserved = set(self._manual_bug_reserved_ids()) if manual_bug_only else set()
        for row in rows:
            if manual_bug_only and (
                row.get("task_type") != "bugfix" or row.get("task_id") not in reserved
            ):
                continue
            try:
                detail = json.loads(str(row.get("detail_json") or "{}"))
            except (TypeError, ValueError):
                continue
            checks = detail.get("checks") or []
            verifier = next(
                (item for item in checks if str(item.get("name") or "") == "verify_service"),
                {},
            )
            failure = str(verifier.get("detail") or "").casefold()
            command = str(verifier.get("command") or "").casefold()
            explicit_port_conflict = (
                "ports are not available" in failure
                and "bind: address already in use" in failure
            )
            # The old checker retained only the final 1,200 characters of
            # Compose output, which could omit the daemon's bind error.  Any
            # preflight produced by the old dependency-reconciling command is
            # safe to retry once with --no-deps: real verifier failures will
            # fail again and cannot match this legacy signature afterward.
            legacy_dependency_reconcile = (
                " run --rm " in (" " + command + " ") and "--no-deps" not in command
            )
            if verifier.get("passed") is not False or not (
                explicit_port_conflict or legacy_dependency_reconcile
            ):
                continue
            # Only recover checks whose setup and running containers passed;
            # a real verify assertion failure must remain rejected.
            required = {"compose_config", "clean_start", "containers_running"}
            passed = {
                str(item.get("name") or "") for item in checks if item.get("passed") is True
            }
            if not required.issubset(passed):
                continue
            stamp = now_iso()
            marker = "Docker 基线预检依赖重建端口冲突，已修复并等待安全重试"
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE audit_events SET event_type='task.baseline_preflight_false_positive'
                         WHERE id=? AND event_type='task.baseline_preflight_failed'""",
                    (row["event_id"],),
                ).rowcount
                if not changed:
                    continue
                conn.execute(
                    """UPDATE tasks SET status='used',rejection_reason='',locked_by=?,updated_at=?
                         WHERE id=? AND status='rejected'""",
                    (row["pair_id"], stamp, row["task_id"]),
                )
                conn.execute(
                    """UPDATE pairs SET status='repair_pending',stage='baseline_preflight_retry_pending',
                         error=?,completed_at=NULL,updated_at=? WHERE id=? AND status='failed'
                         AND stage IN ('baseline_preflight_failed','replaced','replacement_failed')""",
                    (marker, stamp, row["pair_id"]),
                )
                conn.execute(
                    """UPDATE arm_runs SET status='queued',error='',finished_at=NULL,updated_at=?
                         WHERE pair_id=? AND status='failed'""",
                    (stamp, row["pair_id"]),
                )
            recovered += 1
            self.db.audit("task.baseline_preflight_recovered", "pair", row["pair_id"], {
                "task_id": row["task_id"],
                "sourceEventId": row["event_id"],
                "reason": marker,
                "action": "retry_after_pair_capacity",
            })
        return recovered

    def _seed_settings(self) -> None:
        defaults = {
            "codex_model": self.config.codex_model,
            "codex_default_effort": self.config.codex_default_effort,
            "codex_bug_effort": self.config.codex_bug_effort,
            "gsb_recheck_model": "gpt-6-astra",
            "gsb_recheck_effort": "high",
            "claude_model": self.config.claude_model,
            "claude_image": self.config.claude_image,
            "max_pairs_parallel": self.config.max_pairs_parallel,
            "max_claude_terminals": self.config.max_claude_terminals,
            "ab_prompt_stagger_seconds": 30,
            "task_generation_max_parallel": self.config.task_generation_max_parallel,
            "task_pool_min_ready": 6,
            "task_pool_target_ready": 12,
            "feature_ready_target": 10,
            "generated_task_target_estimated_minutes": 120,
            "generated_task_max_estimated_minutes": GENERATED_TASK_MAX_ESTIMATED_MINUTES,
            "auto_refill_enabled": True,
            "auto_refill_interval_seconds": 60,
            "task_generation_zero_to_one_only": True,
            "manual_bug_only_mode": False,
            "manual_bug_auto_refill_enabled": False,
            "manual_bug_ready_target": 6,
            "manual_bug_min_estimated_modules": MANUAL_BUG_MIN_MODULES,
            "manual_bug_max_estimated_modules": MANUAL_BUG_MAX_MODULES,
            "manual_bug_min_estimated_source_lines": MANUAL_BUG_MIN_SOURCE_LINES,
            "manual_bug_max_estimated_source_lines": MANUAL_BUG_MAX_SOURCE_LINES,
            "manual_bug_min_estimated_minutes": MANUAL_BUG_MIN_ESTIMATED_MINUTES,
            "manual_bug_target_estimated_minutes": MANUAL_BUG_TARGET_ESTIMATED_MINUTES,
            "manual_bug_max_estimated_minutes": MANUAL_BUG_MAX_ESTIMATED_MINUTES,
            "manual_bug_allow_failed_source_repair": True,
            "zero_to_one_preferred_categories": [],
            "auto_pipeline_enabled": False,
            "git_author_name": self.config.git_author_name,
            "git_author_email": self.config.git_author_email,
            "github_owner": self.config.github_owner,
            "github_visibility": self.config.github_visibility,
            "repository_prefix": self.config.repository_prefix,
            "first_prompt_warning_minutes": 15,
            "first_prompt_stop_minutes": 60,
            "repeated_no_code_trace_minutes": 40,
            "development_total_warning_minutes": 60,
            "development_total_stop_minutes": 70,
            "development_trace_stall_minutes": 40,
            "business_progress_idle_minutes": 60,
            "development_max_attempts": 2,
            "prompt_infrastructure_retry_limit": 2,
            "rate_limit_retry_base_seconds": 60,
            "rate_limit_retry_max_seconds": 900,
            "terminal_idle_seconds": 120,
            "claude_api_auto_retry_enabled": True,
            "claude_api_cooldown_until": "",
            "claude_api_probe_after": "",
            "claude_pair_start_after": "",
            "recording_width": 1280,
            "recording_height": 720,
            "recording_max_seconds": 90,
            # The temporary Dockerless admission policy is retired. Existing
            # installations may retain its timestamp for audit; operators
            # clear it when returning to the Compose delivery contract.
            "dockerless_task_policy_started_at": "",
        }
        for key, value in defaults.items():
            if self.db.one("SELECT key FROM settings WHERE key=?", (key,)) is None:
                self.db.set_setting(key, value)
        if int(self.db.setting("generated_task_max_estimated_minutes", GENERATED_TASK_MAX_ESTIMATED_MINUTES)) != GENERATED_TASK_MAX_ESTIMATED_MINUTES:
            self.db.set_setting("generated_task_max_estimated_minutes", GENERATED_TASK_MAX_ESTIMATED_MINUTES)
        # Keep existing installations aligned with the current hard-Bug
        # envelope. These limits are operating policy, so a restart must not
        # preserve stale values from an earlier calibration.
        scale_limits = {
            "manual_bug_min_estimated_modules": MANUAL_BUG_MIN_MODULES,
            "manual_bug_max_estimated_modules": MANUAL_BUG_MAX_MODULES,
            "manual_bug_min_estimated_source_lines": MANUAL_BUG_MIN_SOURCE_LINES,
            "manual_bug_max_estimated_source_lines": MANUAL_BUG_MAX_SOURCE_LINES,
            "manual_bug_min_estimated_minutes": MANUAL_BUG_MIN_ESTIMATED_MINUTES,
            "manual_bug_target_estimated_minutes": MANUAL_BUG_TARGET_ESTIMATED_MINUTES,
            "manual_bug_max_estimated_minutes": MANUAL_BUG_MAX_ESTIMATED_MINUTES,
        }
        for key, value in scale_limits.items():
            if int(self.db.setting(key, value)) != value:
                self.db.set_setting(key, value)
        mix_policy = self.db.setting("task_mix_policy", {})
        if isinstance(mix_policy, dict) and mix_policy.get("enabled"):
            if mix_policy.get("maxEstimatedMinutes") != GENERATED_TASK_MAX_ESTIMATED_MINUTES:
                mix_policy["maxEstimatedMinutes"] = GENERATED_TASK_MAX_ESTIMATED_MINUTES
                self.db.set_setting("task_mix_policy", mix_policy)
        # Fixed task-type quotas were retired. Remove the old knobs so an
        # upgraded installation cannot accidentally suggest they still apply.
        self.db.execute(
            """DELETE FROM settings WHERE key IN
               ('task_mix_zero_to_one','task_mix_feature','task_mix_bugfix','task_mix_started_at')"""
        )
        # Upgrade prior shipped workflow values to the current operating rule.
        if int(self.db.setting("first_prompt_stop_minutes", 60)) in (25, 40):
            self.db.set_setting("first_prompt_stop_minutes", 60)
        if int(self.db.setting("development_trace_stall_minutes", 40)) == 15:
            self.db.set_setting("development_trace_stall_minutes", 40)
        if int(self.db.setting("development_max_attempts", 2)) == 3:
            self.db.set_setting("development_max_attempts", 2)
        if (str(self.db.setting("claude_image", self.config.claude_image))
                == "claude-eval-runtime:claude-2.1.269"
                and self.config.claude_image == "claude-eval-runtime:prepared-2.1.269"):
            self.db.set_setting("claude_image", self.config.claude_image)
        configured = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
        if configured > MAX_PAIR_PROJECTS or configured < 1:
            self.db.set_setting("max_pairs_parallel", MAX_PAIR_PROJECTS)
        configured_terminals = int(self.db.setting(
            "max_claude_terminals", self.config.max_claude_terminals,
        ))
        if configured_terminals > MAX_CLAUDE_TERMINALS or configured_terminals < 1:
            self.db.set_setting("max_claude_terminals", self.config.max_claude_terminals)

    def _quarantine_invalid_completed_pairs(self) -> None:
        """Stop unevaluated Docker failures from being presented as deliveries.

        ``observed_failed`` is a final product result with recorded failure
        evidence.  It is valid comparison data under the current workflow and
        must survive service restarts.
        """
        rows = self.db.all(
            """SELECT DISTINCT p.id,p.chain_id FROM pairs p
                 JOIN arm_runs a ON a.pair_id=p.id
            LEFT JOIN artifact_checks c ON c.pair_id=p.id AND c.arm=a.arm AND c.commit_sha=a.commit_sha
                WHERE p.status='completed'
                  AND NOT EXISTS (
                      SELECT 1 FROM delivery_submissions d
                       WHERE d.pair_id=p.id
                         AND (d.status='qc_passed' OR UPPER(d.remote_status)='QC_PASSED')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM audit_events e
                       WHERE e.entity_type='pair' AND e.entity_id=p.id
                         AND e.event_type='pair.prompt_mismatch_retry_cancelled_by_user'
                  )
                  AND COALESCE(c.status,'missing') NOT IN ('passed','observed_failed')"""
        )
        for row in rows:
            stamp = now_iso()
            error = "A/B 至少一侧缺少 Docker 验收记录，原完成记录已拦截"
            with self.db.transaction() as conn:
                conn.execute(
                    """UPDATE pairs SET status='failed',stage='artifact_failed',winner='',completed_at=NULL,
                       error=?,updated_at=? WHERE id=?""",
                    (error, stamp, row["id"]),
                )
                conn.execute(
                    """UPDATE gsb_reviews SET status='draft',confirmed_by='',confirmed_at=NULL,updated_at=?
                       WHERE pair_id=?""",
                    (stamp, row["id"]),
                )
                conn.execute(
                    """UPDATE delivery_submissions SET status='blocked',error=?,updated_at=? WHERE pair_id=?""",
                    (error, stamp, row["id"]),
                )
                if row.get("chain_id"):
                    conn.execute(
                        """UPDATE project_chains SET status='active',followup_completed=0,completed_at=NULL,
                           updated_at=? WHERE id=?""",
                        (stamp, row["chain_id"]),
                    )
            self._invalidate_recordings(row["id"], reason=error)
            self.db.audit("artifact.invalid_delivery_quarantined", "pair", row["id"], {"error": error})

    def _queue_completed_prompt_mismatch_pairs(self) -> int:
        """Block unsubmitted completions whose two original prompts differ."""
        queued = 0
        rows = self.db.all(
            """SELECT p.id,p.chain_id,p.task_id,t.prompt FROM pairs p
                 JOIN tasks t ON t.id=p.task_id
                WHERE p.status='completed'
                  AND NOT EXISTS (
                      SELECT 1 FROM delivery_submissions d WHERE d.pair_id=p.id
                       AND (d.status='qc_passed' OR UPPER(d.remote_status)='QC_PASSED')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM delivery_submissions d WHERE d.pair_id=p.id
                       AND (UPPER(d.status)='DISCARDED' OR UPPER(d.remote_status)='DISCARDED')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM audit_events e
                       WHERE e.entity_type='pair' AND e.entity_id=p.id
                         AND e.event_type='pair.prompt_mismatch_retry_cancelled_by_user'
                  )
                ORDER BY p.updated_at,p.id"""
        )
        for row in rows:
            runs = self.db.all(
                "SELECT * FROM arm_runs WHERE pair_id=? AND status='completed' ORDER BY arm",
                (row["id"],),
            )
            if len(runs) != 2:
                continue
            prompts: Dict[str, str] = {}
            trace_paths: Dict[str, str] = {}
            for run in runs:
                trace, _, _ = self._inspect_trace(run, str(row.get("prompt") or ""))
                if trace:
                    arm_name = str(run.get("arm") or "")
                    prompts[arm_name] = self._trace_first_user_prompt(trace)
                    trace_paths[arm_name] = str(trace)
            if not prompts.get("A") or not prompts.get("B"):
                continue
            if self._paired_trace_prompts_match(
                str(row.get("prompt") or ""), prompts["A"], prompts["B"],
            ):
                continue
            stamp = now_iso()
            reason = "A/B 原生轨迹的完整首轮 User Prompt 不一致，已拦截提交并等待整对重跑"
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE pairs SET status='repair_pending',
                         stage='prompt_mismatch_retry_pending',winner='',completed_at=NULL,
                         error=?,updated_at=? WHERE id=? AND status='completed'""",
                    (reason, stamp, row["id"]),
                ).rowcount
                if not changed:
                    continue
                conn.execute(
                    """UPDATE gsb_reviews SET status='draft',confirmed_by='',confirmed_at=NULL,
                         final_verdict='',final_reason='',updated_at=? WHERE pair_id=?""",
                    (stamp, row["id"]),
                )
                conn.execute(
                    """UPDATE delivery_submissions SET status='blocked',error=?,
                         payload_sha256='',updated_at=? WHERE pair_id=?""",
                    (reason, stamp, row["id"]),
                )
                if row.get("chain_id"):
                    conn.execute(
                        """UPDATE project_chains SET status='active',followup_completed=0,
                             completed_at=NULL,updated_at=? WHERE id=?""",
                        (stamp, row["chain_id"]),
                    )
            self._invalidate_recordings(str(row["id"]), reason=reason)
            self.db.audit("claude.completed_prompt_mismatch_queued", "pair", row["id"], {
                "tracePaths": trace_paths,
                "promptHashes": {
                    arm: hashlib.sha256(value.encode("utf-8")).hexdigest()
                    for arm, value in prompts.items()
                },
                "source": "same_common_baseline",
                "nextStage": "prompt_mismatch_retry_pending",
            })
            queued += 1
        return queued

    def _restore_discarded_false_prompt_mismatches(self) -> int:
        """Undo prompt quarantine for discarded data whose A/B prompts match.

        Claude's TUI canonicalizes blank paragraph rows before recording the
        first User event.  That transport difference does not make A and B
        unequal.  Discarded platform data must not consume two fresh Claude
        sessions merely because the database retains the original spacing.
        """
        restored = 0
        rows = self.db.all(
            """SELECT p.id,p.chain_id,t.prompt,g.verdict,g.updated_at gsb_updated,
                      d.qc_summary,d.remote_status
                 FROM pairs p JOIN tasks t ON t.id=p.task_id
                 JOIN gsb_reviews g ON g.pair_id=p.id
                 JOIN delivery_submissions d ON d.pair_id=p.id
                WHERE p.status='repair_pending'
                  AND p.stage='prompt_mismatch_retry_pending'
                  AND (UPPER(d.status)='DISCARDED' OR UPPER(d.remote_status)='DISCARDED')
                ORDER BY p.updated_at,p.id"""
        )
        for row in rows:
            runs = self.db.all(
                "SELECT * FROM arm_runs WHERE pair_id=? AND status='completed' ORDER BY arm",
                (row["id"],),
            )
            if len(runs) != 2:
                continue
            prompts: Dict[str, str] = {}
            for run in runs:
                trace, _, _ = self._inspect_trace(run, str(row.get("prompt") or ""))
                if trace:
                    prompts[str(run.get("arm") or "")] = self._trace_first_user_prompt(trace)
            if not prompts.get("A") or not prompts.get("B"):
                continue
            if not self._paired_trace_prompts_match(
                str(row.get("prompt") or ""), prompts["A"], prompts["B"],
            ):
                continue
            confirmation = self.db.one(
                """SELECT detail_json,created_at FROM audit_events
                     WHERE event_type='gsb.confirmed' AND entity_id=?
                     ORDER BY id DESC LIMIT 1""",
                (row["id"],),
            ) or {}
            try:
                confirmation_detail = json.loads(str(confirmation.get("detail_json") or "{}"))
            except (TypeError, ValueError):
                confirmation_detail = {}
            completed_at = str(
                confirmation.get("created_at") or row.get("gsb_updated") or now_iso()
            )
            confirmed_by = str(
                confirmation_detail.get("confirmed_by") or "刘昱（按授权默认确认）"
            )
            stamp = now_iso()
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE pairs SET status='completed',stage='completed',winner=?,error='',
                         completed_at=?,updated_at=? WHERE id=? AND status='repair_pending'
                         AND stage='prompt_mismatch_retry_pending'""",
                    (str(row.get("verdict") or ""), completed_at, stamp, row["id"]),
                ).rowcount
                if not changed:
                    continue
                conn.execute(
                    """UPDATE gsb_reviews SET status='confirmed',confirmed_by=?,confirmed_at=?,
                         updated_at=? WHERE pair_id=?""",
                    (confirmed_by, completed_at, stamp, row["id"]),
                )
                conn.execute(
                    """UPDATE delivery_submissions SET status='discarded',error=?,updated_at=?
                         WHERE pair_id=?""",
                    (str(row.get("qc_summary") or ""), stamp, row["id"]),
                )
                if row.get("chain_id"):
                    remaining = conn.execute(
                        "SELECT 1 FROM pairs WHERE chain_id=? AND id<>? AND status<>'completed' LIMIT 1",
                        (row["chain_id"], row["id"]),
                    ).fetchone()
                    if not remaining:
                        conn.execute(
                            """UPDATE project_chains SET status='completed',followup_completed=1,
                                 completed_at=?,updated_at=? WHERE id=?""",
                            (completed_at, stamp, row["chain_id"]),
                        )
            self.db.audit("claude.prompt_mismatch_false_positive_restored", "pair", row["id"], {
                "reason": "A/B_first_user_prompts_match_after_newline_normalization",
                "databaseFormattingIgnored": True,
                "deliveryPreservedAs": "discarded",
                "freshSessionsStarted": False,
            })
            restored += 1
        return restored

    def _queue_invalid_delivery_lineage_pairs(self) -> None:
        """Normalize safe legacy chains; redevelop only snapshots unrelated to main."""
        rows = self.db.all(
            """SELECT DISTINCT p.id,p.baseline_sha FROM pairs p
                 JOIN delivery_submissions d ON d.pair_id=p.id
                WHERE p.status='completed' AND d.remote_id=''
                  AND d.status IN ('ready_to_submit','failed','needs_fix')"""
        )
        for pair in rows:
            baseline = str(pair.get("baseline_sha") or "")
            invalid_arms = []
            normalization_errors = []
            for arm in self.db.all(
                    "SELECT arm,commit_sha,workspace_path FROM arm_runs WHERE pair_id=? ORDER BY arm",
                    (pair["id"],)):
                workspace = Path(str(arm.get("workspace_path") or ""))
                commit = str(arm.get("commit_sha") or "")
                parent = run_command(
                    ["git", "rev-parse", commit + "^"], cwd=workspace,
                    check=False, timeout=15,
                ) if commit and workspace.is_dir() else None
                if parent and parent.returncode == 0 and parent.stdout.strip() == baseline:
                    continue
                ancestor = run_command(
                    ["git", "merge-base", "--is-ancestor", baseline, commit], cwd=workspace,
                    check=False, timeout=15,
                ) if commit and workspace.is_dir() and baseline else None
                if ancestor and ancestor.returncode == 0:
                    try:
                        self._normalize_delivery_arm_snapshot(pair["id"], arm)
                    except Exception as exc:
                        normalization_errors.append("%s：%s" % (
                            str(arm.get("arm") or "?"), redact(str(exc))[-1000:],
                        ))
                    continue
                invalid_arms.append(str(arm.get("arm") or "?"))
            if normalization_errors and not invalid_arms:
                stamp = now_iso()
                error = "产物代码来自初始环境，但提交历史需要压成单一快照，等待自动重试：" + "；".join(normalization_errors)
                with self.db.transaction() as conn:
                    conn.execute(
                        """UPDATE pairs SET status='repair_pending',stage='lineage_normalization_pending',
                           error=?,updated_at=? WHERE id=?""", (error, stamp, pair["id"]),
                    )
                    conn.execute(
                        """UPDATE delivery_submissions SET status='needs_review',error=?,updated_at=?
                           WHERE pair_id=?""", (error, stamp, pair["id"]),
                    )
                self.db.audit("git.delivery_lineage_normalization_deferred", "pair", pair["id"], {
                    "errors": normalization_errors, "baseline_sha": baseline,
                })
                continue
            if not invalid_arms:
                continue
            stamp = now_iso()
            error = "A/B 产物没有形成基于初始环境的有效代码提交，已排队用原题面重新开发：" + "、".join(invalid_arms)
            with self.db.transaction() as conn:
                conn.execute(
                    """UPDATE pairs SET status='repair_pending',stage='lineage_repair_pending',
                       winner='',completed_at=NULL,error=?,updated_at=? WHERE id=?""",
                    (error, stamp, pair["id"]),
                )
                conn.execute(
                    """UPDATE delivery_submissions SET status='needs_review',error=?,updated_at=?
                       WHERE pair_id=?""", (error, stamp, pair["id"]),
                )
            self.db.audit("git.invalid_delivery_lineage_queued", "pair", pair["id"], {
                "arms": invalid_arms, "baseline_sha": baseline,
            })

    def _normalize_delivery_arm_snapshot(self, pair_id: str, arm: Dict[str, Any]) -> str:
        """Squash a legacy repair chain without changing its delivered tree or evidence."""
        arm_name = str(arm.get("arm") or "")
        old_sha = str(arm.get("commit_sha") or "")
        workspace = Path(str(arm.get("workspace_path") or ""))
        pair = self._pair(pair_id)
        baseline = str(pair.get("baseline_sha") or "")
        if arm_name not in ("A", "B") or not re.fullmatch(r"[0-9a-f]{40}", old_sha):
            raise RuntimeError("缺少可规范化的 Arm 提交")
        if not (workspace / ".git").is_dir() or not re.fullmatch(r"[0-9a-f]{40}", baseline):
            raise RuntimeError("缺少可用的工作区或初始环境快照")
        if run_command(["git", "merge-base", "--is-ancestor", baseline, old_sha], cwd=workspace,
                       check=False, timeout=15).returncode != 0:
            raise RuntimeError("现有提交不是从初始环境快照派生，不能只压平历史")
        if run_command(["git", "status", "--porcelain"], cwd=workspace, timeout=15).stdout.strip():
            raise RuntimeError("交付工作区存在未提交改动，不能安全压平")
        old_tree = run_command(["git", "rev-parse", old_sha + "^{tree}"], cwd=workspace, timeout=15).stdout.strip()
        new_sha = self.git.push_arm(pair_id, arm_name)
        new_tree = run_command(["git", "rev-parse", new_sha + "^{tree}"], cwd=workspace, timeout=15).stdout.strip()
        if old_tree != new_tree:
            raise RuntimeError("压平提交后代码树发生变化，已停止更新证据引用")
        stamp = now_iso()
        repo_column = "a_sha" if arm_name == "A" else "b_sha"
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE arm_runs SET commit_sha=?,updated_at=? WHERE pair_id=? AND arm=? AND commit_sha=?",
                (new_sha, stamp, pair_id, arm_name, old_sha),
            )
            conn.execute(
                "UPDATE git_repositories SET %s=?,updated_at=? WHERE pair_id=?" % repo_column,
                (new_sha, stamp, pair_id),
            )
            for table in ("artifact_checks", "recordings", "recording_attempts"):
                conn.execute(
                    "UPDATE %s SET commit_sha=?,updated_at=? WHERE pair_id=? AND arm=? AND commit_sha=?" % table,
                    (new_sha, stamp, pair_id, arm_name, old_sha),
                )
            conn.execute(
                "UPDATE bug_candidates SET source_sha=?,updated_at=? WHERE source_pair_id=? AND source_arm=? AND source_sha=?",
                (new_sha, stamp, pair_id, arm_name, old_sha),
            )
        self.db.audit("git.delivery_lineage_normalized", "pair", pair_id, {
            "arm": arm_name, "old_sha": old_sha, "new_sha": new_sha,
            "baseline_sha": baseline, "tree_unchanged": True,
        })
        return new_sha

    def normalize_delivery_lineage(self, pair_id: str) -> Dict[str, Any]:
        """Normalize every safe Arm chain and restore the Pair without redevelopment."""
        pair = self._pair(pair_id)
        baseline = str(pair.get("baseline_sha") or "")
        normalized: Dict[str, str] = {}
        for arm in self.db.all(
                "SELECT arm,commit_sha,workspace_path FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,)):
            workspace = Path(str(arm.get("workspace_path") or ""))
            commit = str(arm.get("commit_sha") or "")
            parent = run_command(["git", "rev-parse", commit + "^"], cwd=workspace,
                                 check=False, timeout=15)
            if parent.returncode == 0 and parent.stdout.strip() == baseline:
                continue
            normalized[str(arm.get("arm") or "?")] = self._normalize_delivery_arm_snapshot(pair_id, arm)
        review = self.db.one("SELECT verdict FROM gsb_reviews WHERE pair_id=?", (pair_id,)) or {}
        stamp = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE pairs SET status='completed',stage='completed',winner=?,error='',
                   completed_at=COALESCE(completed_at,?),updated_at=? WHERE id=?""",
                (str(review.get("verdict") or ""), stamp, stamp, pair_id),
            )
            conn.execute(
                """UPDATE delivery_submissions SET status=CASE WHEN remote_id='' THEN 'ready_to_submit'
                     WHEN remote_status='PENDING_FIX' THEN 'needs_fix' ELSE status END,
                   error='',updated_at=? WHERE pair_id=?""", (stamp, pair_id),
            )
        self.db.audit("git.delivery_lineage_restored", "pair", pair_id, {
            "normalized_arms": normalized, "baseline_sha": baseline,
        })
        return {"pair_id": pair_id, "normalized": normalized, "pair": self.pair_detail(pair_id)}

    def _resume_one_lineage_normalization(self) -> bool:
        row = self.db.one(
            """SELECT id FROM pairs WHERE status='repair_pending'
                 AND stage='lineage_normalization_pending' ORDER BY updated_at,created_at LIMIT 1"""
        )
        if not row:
            return False
        pair_id = str(row["id"])
        try:
            self.normalize_delivery_lineage(pair_id)
            return True
        except Exception as exc:
            error = "提交历史自动压平失败，稍后重试：" + redact(str(exc))[-1500:]
            self.db.execute(
                "UPDATE pairs SET error=?,updated_at=? WHERE id=?", (error, now_iso(), pair_id),
            )
            self.db.audit("git.delivery_lineage_normalization_failed", "pair", pair_id, {"error": error})
            return False

    def _restore_false_completed_tasks(self) -> None:
        """Return tasks retired only because progress text was mistaken for completion."""
        rows = self.db.all(
            """SELECT DISTINCT t.id,p.id pair_id,t.title FROM tasks t
                 JOIN pairs p ON p.task_id=t.id
                 JOIN arm_runs ar ON ar.pair_id=p.id
                 JOIN audit_events e ON e.entity_id=ar.id
                WHERE t.status='used' AND p.status='failed'
                  AND p.stage IN ('replaced','replacement_failed','artifact_failed','development_failed')
                  AND e.event_type='claude.arm_completed'
                  AND json_extract(e.detail_json,'$.commit_sha')=p.baseline_sha
                  AND NOT EXISTS(
                    SELECT 1 FROM pairs newer WHERE newer.task_id=t.id AND newer.id<>p.id
                      AND newer.status IN ('queued','running','review','completed','repair_pending')
                  )
                ORDER BY p.updated_at"""
        )
        for row in rows:
            stamp = now_iso()
            self.db.execute(
                """UPDATE tasks SET status='ready',used_at=NULL,rejection_reason='',updated_at=?
                   WHERE id=? AND status='used'""", (stamp, row["id"]),
            )
            self.db.audit("task.false_completion_restored", "task", row["id"], {
                "retired_pair_id": row["pair_id"],
                "reason": "historical_tool_use_progress_was_mistaken_for_completion",
            })

    def _invalidate_recordings(self, pair_id: str, arms=None, reason: str = "") -> int:
        """Remove current-delivery pointers while preserving attempt history and files."""
        selected = [str(arm) for arm in (arms or []) if str(arm) in ("A", "B")]
        where = "pair_id=?"
        params = [pair_id]
        if selected:
            where += " AND arm IN (%s)" % ",".join("?" for _ in selected)
            params.extend(selected)
        rows = self.db.all("SELECT id,arm,attempt_id FROM recordings WHERE " + where, tuple(params))
        if not rows:
            return 0
        self.db.execute("DELETE FROM recordings WHERE " + where, tuple(params))
        self.db.audit("recording.current_invalidated", "pair", pair_id, {
            "arms": [row["arm"] for row in rows],
            "attemptIds": [row.get("attempt_id", "") for row in rows],
            "reason": redact(reason)[-1000:],
            "historyPreserved": True,
        })
        return len(rows)

    def start_scheduler(self) -> None:
        if self._scheduler_started:
            return
        self._scheduler_started = True
        self._resume_active_monitors()
        threading.Thread(target=self._scheduler_loop, name="task-pool-refill", daemon=True).start()

    def _resume_active_monitors(self) -> None:
        """Reattach monitoring after the web service restarts.

        Claude runs in independent Docker/screen sessions, so a service update
        must not strand work that already received its prompt.
        """
        # A replacement worker can finish while restart recovery is still
        # attaching monitors. Terminal replacement stages must never be
        # counted as active Pair capacity.
        self.db.execute(
            """UPDATE pairs SET status='failed',updated_at=?
               WHERE stage IN ('replaced','replacement_failed') AND status<>'failed'""",
            (now_iso(),),
        )
        rows = self.db.all(
            """SELECT a.id arm_id,a.pair_id,t.prompt FROM arm_runs a
               JOIN pairs p ON p.id=a.pair_id JOIN tasks t ON t.id=p.task_id
               WHERE a.prompt_sent_at IS NOT NULL
                 AND a.status IN ('running','developing','waiting_retry')
                 AND p.stage='development'"""
        )
        pair_ids = set()
        for row in rows:
            pair_ids.add(row["pair_id"])
            self._submit_monitor(
                "monitor-" + row["arm_id"], self._monitor_arm,
                row["pair_id"], row["arm_id"], row["prompt"],
            )
        pending_retries = self.db.all(
            """SELECT a.id arm_id,a.pair_id,t.prompt FROM arm_runs a
               JOIN pairs p ON p.id=a.pair_id JOIN tasks t ON t.id=p.task_id
               WHERE a.prompt_sent_at IS NULL
                 AND a.status IN ('queued','waiting_retry','running')
                 AND p.stage='development'"""
        )
        for row in pending_retries:
            pair_ids.add(row["pair_id"])
            self._submit_monitor(
                "retry-recover-" + row["arm_id"], self._recover_pending_retry,
                row["pair_id"], row["arm_id"], row["prompt"],
            )
        for pair_id in pair_ids:
            self.db.execute(
                """UPDATE pairs SET status='running',error='',updated_at=?
                   WHERE id=? AND stage='development'
                     AND status IN ('queued','running','review')""",
                (now_iso(), pair_id),
            )

    def _recover_pending_retry(self, pair_id: str, arm_id: str, prompt: str) -> Dict[str, Any]:
        """Finish a fresh-session retry interrupted by a service restart."""
        arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,))
        if not arm:
            return {}
        if self.db.setting("pipeline_drain", False) and not arm.get("prompt_sent_at"):
            return arm
        pair = self._pair(pair_id)
        if pair.get("stage") != "development":
            return arm
        if self._pair_development_budget_exhausted(pair_id):
            # Restart recovery must obey the same Pair-wide failure budget as
            # the normal scheduler.  A service restart is not a new attempt
            # window for an unsent queued/deferred Arm.
            self._finish_exhausted_pair_after_peer(pair_id)
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
        if arm.get("prompt_sent_at"):
            return self._monitor_arm(pair_id, arm_id, prompt)
        repo = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,)) or {}
        canonical = Path(str(repo.get("local_root") or "")) / str(arm["arm"])
        expected_sha = str(pair.get("baseline_sha") or "")
        column = "a_sha" if arm["arm"] == "A" else "b_sha"
        delivered_sha = str(arm.get("commit_sha") or repo.get(column) or "")
        failed_artifact = None
        if re.fullmatch(r"[0-9a-f]{40}", delivered_sha) and delivered_sha != expected_sha:
            failed_artifact = self.db.one(
                """SELECT id FROM artifact_checks
                   WHERE pair_id=? AND arm=? AND commit_sha=? AND status='failed'
                   ORDER BY created_at DESC LIMIT 1""",
                (pair_id, arm["arm"], delivered_sha),
            )
        repair_error = (
            "docker 产物验收" in str(arm.get("error") or "").casefold()
            or bool(failed_artifact)
        )
        if repair_error:
            if not re.fullmatch(r"[0-9a-f]{40}", delivered_sha) or delivered_sha == expected_sha:
                message = "旧 Docker 返工任务缺少可核对的已交付提交，已停止且不会重新调用 Claude"
                stamp = now_iso()
                self.db.execute(
                    "UPDATE arm_runs SET status='failed',error=?,updated_at=? WHERE id=?",
                    (message, stamp, arm_id),
                )
                self.db.execute(
                    """UPDATE pairs SET status='failed',stage='artifact_failed',error=?,updated_at=?
                       WHERE id=?""",
                    (message, stamp, pair_id),
                )
                self.db.audit("artifact.legacy_repair_retired", "arm_run", arm_id, {
                    "reason": message, "claudeRepairStarted": False,
                })
                return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            stamp = now_iso()
            self.db.execute(
                """UPDATE artifact_checks SET status='observed_failed',updated_at=?
                   WHERE pair_id=? AND arm=? AND commit_sha=? AND status='failed'""",
                (stamp, pair_id, arm["arm"], delivered_sha),
            )
            self.db.execute(
                """UPDATE arm_runs SET status='completed',commit_sha=?,error='',updated_at=?
                   WHERE id=?""",
                (delivered_sha, stamp, arm_id),
            )
            self.db.audit("artifact.legacy_repair_preserved_for_gsb", "arm_run", arm_id, {
                "sourceCommit": delivered_sha,
                "claudeRepairStarted": False,
                "action": "record_failure_evidence_and_continue_gsb",
            })
            self._refresh_pair_after_arm(pair_id)
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
        active_prompt = prompt
        try:
            if (arm.get("image_id") and self._is_prompt_delivery_error(
                    str(arm.get("error") or ""))):
                # A rejected first prompt is still a real native JSONL event.
                # Preserve it before replacing only this Arm's session.
                self.claude.archive_failed_attempt(
                    arm, str(arm.get("error") or ""), prepare_retry=True,
                    count_development_failure=False, count_error_retry=False,
                )
            else:
                self.claude.reset_unsent_arm(arm)
            if expected_sha != str(pair.get("baseline_sha") or ""):
                canonical = self.git.prepare_arm_commit(pair_id, str(arm["arm"]), expected_sha)
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            reservation = self._launch_arm_if_capacity(arm)
            if reservation is None:
                return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            if not reservation:
                return self._mark_arm_waiting_for_capacity(arm_id, str(arm.get("error") or ""))
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            self.claude.wait_until_ready(arm)
            self.claude.materialize_repository(arm, canonical, expected_sha)
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            self._send_prompt_with_pair_stagger(pair_id, arm, active_prompt)
            event_type = "artifact.pending_repair_recovered" if repair_error else "claude.pending_retry_recovered"
            self.db.audit(event_type, "arm_run", arm_id, {
                "attempt": int(arm.get("attempt_no") or 1),
                "prompt_mode": "original_plus_artifact_failure" if repair_error else "same_original_prompt_once",
                "source_commit": expected_sha,
            })
            # Hand monitoring to the canonical per-Arm operation. Calling the
            # loop inline leaves this recovery future under a different key,
            # allowing the scheduler to start a second monitor for the same
            # Arm while both mutate the same trace snapshot.
            self._submit_monitor(
                "monitor-" + arm_id, self._monitor_arm,
                pair_id, arm_id, active_prompt,
            )
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
        except Exception as exc:
            detail = redact(str(exc))
            prompt_repair = self._is_prompt_delivery_error(detail) or (
                ("轨迹" in str(arm.get("error") or "") and "Prompt" in str(arm.get("error") or ""))
            )
            prefix = "轨迹首轮 User Prompt 重跑启动失败" if prompt_repair else "恢复全新 Session 失败"
            failure = "%s：%s" % (prefix, detail)
            current = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            return self._handle_attempt_failure(pair_id, current, active_prompt, failure)

    def _canonicalize_pair_prompt(self, pair_id: str, fallback: str) -> str:
        """Persist exactly the prompt form stored by Claude's native trace."""
        task = self.db.one(
            """SELECT t.id,t.prompt FROM tasks t
               JOIN pairs p ON p.task_id=t.id WHERE p.id=?""",
            (pair_id,),
        ) or {}
        source = str(task.get("prompt") if task.get("prompt") is not None else fallback)
        canonical = self.claude.canonical_prompt(source)
        if task.get("id") and canonical != source:
            self.db.execute(
                "UPDATE tasks SET prompt=?,updated_at=? WHERE id=? AND prompt=?",
                (canonical, now_iso(), task["id"], source),
            )
            self.db.audit("task.prompt_canonicalized_for_native_trace", "task", task["id"], {
                "pairId": pair_id,
                "beforeLength": len(source),
                "afterLength": len(canonical),
                "normalization": "line_endings_and_blank_paragraph_rows",
            })
        return canonical

    def _send_prompt_with_pair_stagger(self, pair_id: str, arm: Dict[str, Any], prompt: str) -> None:
        """Send one original prompt while keeping the two Arm sends apart.

        The per-Pair lock also covers concurrent A/B recovery threads. The
        persisted timestamp keeps the spacing after a service restart; one
        extra second compensates for the database timestamp's second-level
        precision so the real interval never becomes shorter than configured.
        """
        prompt = self._canonicalize_pair_prompt(pair_id, prompt)
        with self._prompt_locks_lock:
            prompt_lock = self._prompt_locks.setdefault(pair_id, threading.Lock())
        with prompt_lock:
            interval = max(0, min(300, int(self.db.setting("ab_prompt_stagger_seconds", 30))))
            other = self.db.one(
                """SELECT arm,prompt_sent_at FROM arm_runs
                   WHERE pair_id=? AND arm<>? AND prompt_sent_at IS NOT NULL
                   ORDER BY prompt_sent_at DESC LIMIT 1""",
                (pair_id, arm["arm"]),
            )
            waited = 0.0
            if interval and other and other.get("prompt_sent_at"):
                try:
                    sent_at = datetime.fromisoformat(str(other["prompt_sent_at"]).replace("Z", "+00:00"))
                    if sent_at.tzinfo is None:
                        sent_at = sent_at.replace(tzinfo=timezone.utc)
                    elapsed = max(0.0, (datetime.now(timezone.utc) - sent_at).total_seconds())
                    waited = max(0.0, interval + 1.0 - elapsed)
                except (TypeError, ValueError):
                    waited = float(interval)
                if waited:
                    time.sleep(waited)
            refreshed = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or arm
            if refreshed.get("prompt_sent_at"):
                return
            self.claude.send_prompt(refreshed, prompt)
            self.db.audit("claude.original_prompt_sent", "arm_run", arm["id"], {
                "arm": arm["arm"],
                "configured_stagger_seconds": interval,
                "waited_seconds": round(waited, 3),
            })

    def _scheduler_loop(self) -> None:
        # Let HTTP start first. Task-pool refill has its own slower cadence;
        # the full-pipeline driver reacts quickly when a Pair finishes.
        time.sleep(3)
        next_refill = 0.0
        while True:
            try:
                if self.db.setting("pipeline_drain", False):
                    time.sleep(5)
                    continue
                self._advance_task_mix_policy()
                current = time.monotonic()
                refill_enabled = bool(self.db.setting("auto_refill_enabled", True)) or (
                    bool(self.db.setting("manual_bug_only_mode", False))
                    and bool(self.db.setting("manual_bug_auto_refill_enabled", False))
                )
                if refill_enabled and current >= next_refill:
                    self._schedule_refill_once()
                    interval = max(30, int(self.db.setting("auto_refill_interval_seconds", 60)))
                    next_refill = current + interval
                # Operator-requested A/B requeues are independent from the
                # automatic task pipeline.  They must still start when a Pair
                # slot opens even when automatic task creation is paused.
                while self._resume_one_manual_arm_requeue():
                    pass
                if bool(self.db.setting("auto_pipeline_enabled", False)):
                    self._schedule_auto_pipeline_once()
            except Exception as exc:
                self.db.audit("automation.scheduler_error", "scheduler", "full-pipeline", {"error": str(exc)[-2000:]})
            time.sleep(5)

    def automation_status(self) -> Dict[str, Any]:
        configured = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
        target = max(1, min(MAX_PAIR_PROJECTS, configured))
        terminal_target = self._development_arm_limit()
        manual_bug_only = bool(self.db.setting("manual_bug_only_mode", False))
        zero_to_one_only = bool(
            self.db.setting("task_generation_zero_to_one_only", True)
        )
        active = int((self.db.one(
            "SELECT COUNT(*) count FROM development_pairs WHERE status IN ('queued','running','review')"
        ) or {"count": 0})["count"])
        waiting_api_pairs = int((self.db.one(
            "SELECT COUNT(*) count FROM pairs WHERE status='waiting_api_retry'"
        ) or {"count": 0})["count"])
        waiting_api_arms = int((self.db.one(
            """SELECT COUNT(*) count FROM arm_runs a JOIN pairs p ON p.id=a.pair_id
                 WHERE a.status='waiting_api_retry' AND p.stage='development'
                   AND p.status IN ('running','waiting_api_retry')"""
        ) or {"count": 0})["count"])
        ready = (
            self._manual_bug_ready_count()
            if manual_bug_only else
            int((self.db.one(
                "SELECT COUNT(*) count FROM tasks WHERE status='ready' AND " + ELIGIBLE_TASK_SQL
                + (" AND task_type='zero_to_one'" if zero_to_one_only else "")
            ) or {"count": 0})["count"])
        )
        generating = int((self.db.one(
            "SELECT COUNT(*) count FROM generation_batches WHERE status='running'"
        ) or {"count": 0})["count"])
        stages = self.db.all(
            """SELECT stage,COUNT(*) count FROM pairs
               WHERE status IN ('queued','running','review','waiting_api_retry')
               GROUP BY stage ORDER BY stage"""
        )
        return {
            "enabled": bool(self.db.setting("auto_pipeline_enabled", False)),
            "draining": bool(self.db.setting("pipeline_drain", False)),
            "resourcesNeedingReview": self.db.all("SELECT project,updated_at FROM runtime_resources WHERE status='needs_review'"),
            "blockedOperations": self.db.all(
                "SELECT operation,attempts,error_kind,error,retry_after FROM pipeline_operations WHERE status='blocked'"
            ),
            "postprocessingPairs": int((self.db.one(
                "SELECT COUNT(*) count FROM pairs WHERE status IN ('running','review') "
                "AND stage IN ('artifact_validation','difficulty_review','recording','gsb_ready','gsb_confirmation')"
            ) or {"count": 0})["count"]),
            "targetPairs": target,
            "targetDevelopmentArms": terminal_target,
            "activeDevelopmentArms": self._active_development_arm_count(),
            "waitingDevelopmentArms": int((self.db.one(
                """SELECT COUNT(*) count FROM arm_runs a JOIN pairs p ON p.id=a.pair_id
                     WHERE p.stage='development' AND p.status IN ('queued','running','review')
                       AND a.prompt_sent_at IS NULL
                       AND a.status IN ('queued','waiting_retry')"""
            ) or {"count": 0})["count"]),
            "activePairs": active,
            "waitingApiPairs": waiting_api_pairs,
            "waitingApiArms": waiting_api_arms,
            "apiAutoRetryEnabled": bool(
                self.db.setting("claude_api_auto_retry_enabled", True)
            ),
            "apiCooldownUntil": (
                str(self.db.setting("claude_api_cooldown_until", "") or "")
                if self._api_cooldown_active() else ""
            ),
            "readyTasks": ready,
            "readyTaskTarget": int(self.db.setting(
                "manual_bug_ready_target" if manual_bug_only else "task_pool_target_ready",
                6 if manual_bug_only else 12,
            )),
            "bugAutoRefillEnabled": bool(
                self.db.setting("manual_bug_auto_refill_enabled", False)
                and (manual_bug_only or (
                    self._task_mix_policy() and self.db.setting("auto_refill_enabled", True)
                ))
            ),
            "generatingBatches": generating,
            "stages": stages,
            "taskSelectionMode": (
                "phased_ratio" if self._task_mix_policy() else
                "manual_bug_only" if manual_bug_only else
                "zero_to_one_only" if zero_to_one_only else "available_first"
            ),
            "taskSelectionPolicy": (
                "phased_ratio" if self._task_mix_policy() else
                "manual_bug_only" if manual_bug_only else
                "zero_to_one_only" if zero_to_one_only else "first_available"
            ),
            "taskMix": self._task_mix_progress(),
        }

    def _development_arm_limit(self) -> int:
        configured = int(self.db.setting(
            "max_claude_terminals", self.config.max_claude_terminals,
        ))
        return max(1, min(MAX_CLAUDE_TERMINALS, configured))

    def _active_development_arm_count(self) -> int:
        """Count terminal slots already owned by live/checkpointing Arms."""
        return int((self.db.one(
            """SELECT COUNT(*) count FROM arm_runs
                 WHERE status IN ('running','developing','checkpointing')
                    OR (status='waiting_retry' AND image_id<>'')"""
        ) or {"count": 0})["count"])

    def _available_development_arm_slots(self) -> int:
        return max(0, self._development_arm_limit() - self._active_development_arm_count())

    def _priority_partial_pair_ids(self) -> set:
        """Return active Pairs whose already-started side is waiting for its peer.

        A newly prepared Pair must not race one of these deferred Arms for the
        next terminal slot.  The waiting side remains the priority until its
        original prompt has actually been sent, unless the Pair-wide failure
        budget has already been exhausted.
        """
        maximum = max(1, int(self.db.setting("development_max_attempts", 2)))
        return {
            str(row["pair_id"])
            for row in self.db.all(
                """SELECT DISTINCT p.id pair_id
                     FROM pairs p JOIN arm_runs pending ON pending.pair_id=p.id
                    WHERE p.status IN ('queued','running','review')
                      AND p.stage='development'
                      AND p.development_failure_count<?
                      AND pending.prompt_sent_at IS NULL
                      AND pending.status IN ('queued','running','waiting_retry')
                      AND EXISTS(
                          SELECT 1 FROM arm_runs peer
                           WHERE peer.pair_id=p.id
                             AND peer.arm<>pending.arm
                             AND (peer.prompt_sent_at IS NOT NULL
                                  OR peer.commit_sha<>''
                                  OR peer.status IN ('running','developing','completed',
                                                     'checkpointing','exported'))
                      )""",
                (maximum,),
            )
        }

    def _pair_development_budget_exhausted(self, pair_id: str) -> bool:
        pair = self.db.one(
            "SELECT development_failure_count FROM pairs WHERE id=?", (pair_id,),
        ) or {}
        maximum = max(1, int(self.db.setting("development_max_attempts", 2)))
        return int(pair.get("development_failure_count") or 0) >= maximum

    def _launch_arm_if_capacity(self, arm: Dict[str, Any]) -> Optional[bool]:
        """Reserve a terminal; ``None`` means another worker already owns it."""
        with self._arm_launch_lock:
            current = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or arm
            pair_id = str(current.get("pair_id") or arm.get("pair_id") or "")
            if pair_id and self._pair_development_budget_exhausted(pair_id):
                # The failure counter can reach its limit while a previously
                # queued launch worker is waiting on this lock.  Recheck at the
                # final reservation boundary so that stale work cannot create
                # another Claude session after the exhausted attempt was
                # archived.
                self.db.audit(
                    "claude.terminal_reservation_blocked_by_failure_budget",
                    "arm_run", arm["id"], {
                        "pair_id": pair_id,
                        "status": current.get("status") or "",
                        "action": "skip_stale_launch",
                    },
                )
                return None
            if current.get("status") in ("running", "developing", "checkpointing"):
                self.db.audit(
                    "claude.terminal_reservation_already_claimed", "arm_run", arm["id"], {
                        "status": current.get("status"),
                        "action": "skip_duplicate_launch",
                    },
                )
                return None
            if self._available_development_arm_slots() <= 0:
                return False
            self.claude.launch(current)
            return True

    def _mark_arm_waiting_for_capacity(self, arm_id: str, error: str = "") -> Dict[str, Any]:
        marker = "等待 Claude 开发终端空位"
        current = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or {}
        previous = str(error or current.get("error") or "").strip()
        message = previous if marker in previous else (previous + ("；" if previous else "") + marker)
        self.db.execute(
            "UPDATE arm_runs SET status='waiting_retry',error=?,updated_at=? WHERE id=?",
            (message[-2000:], now_iso(), arm_id),
        )
        if marker not in str(current.get("error") or ""):
            self.db.audit("claude.arm_deferred_for_capacity", "arm_run", arm_id, {
                "terminalLimit": self._development_arm_limit(),
                "activeTerminals": self._active_development_arm_count(),
            })
        return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or current

    @staticmethod
    def _future_iso(value: Any) -> bool:
        text = str(value or "")
        return bool(text and text > now_iso())

    def _api_cooldown_active(self) -> bool:
        return self._future_iso(self.db.setting("claude_api_cooldown_until", ""))

    def _api_probe_blocked(self) -> bool:
        return self._future_iso(self.db.setting("claude_api_probe_after", ""))

    def _pair_start_blocked(self) -> bool:
        return self._future_iso(self.db.setting("claude_pair_start_after", ""))

    def _register_global_rate_limit(self, error: str, retry_after: datetime) -> None:
        """Open a shared breaker so replacing a Pair cannot bypass a 429."""
        lowered = str(error or "").casefold()
        if "429" not in lowered and "max_parallel_requests" not in lowered:
            return
        match = re.search(
            r"resets at:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*utc",
            str(error or ""), re.IGNORECASE,
        )
        provider_reset = (
            datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if match else retry_after
        )
        cooldown = max(provider_reset, datetime.now(timezone.utc) + timedelta(minutes=5))
        cooldown_text = cooldown.isoformat(timespec="seconds")
        current = str(self.db.setting("claude_api_cooldown_until", "") or "")
        if cooldown_text > current:
            self.db.set_setting("claude_api_cooldown_until", cooldown_text)
        self.db.audit("claude.api_global_cooldown_started", "scheduler", "claude-api", {
            "cooldown_until": max(current, cooldown_text),
            "reason": "max_parallel_requests",
            "new_pair_launches_blocked": True,
            "provider_reset": provider_reset.isoformat(timespec="seconds"),
            "single_probe_after_cooldown": True,
        })

    def set_auto_pipeline(self, enabled: bool) -> Dict[str, Any]:
        self.db.set_setting("auto_pipeline_enabled", bool(enabled))
        configured = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
        target = max(1, min(MAX_PAIR_PROJECTS, configured))
        self.db.audit(
            "automation.started" if enabled else "automation.stopped",
            "scheduler", "full-pipeline", {"targetPairs": target},
        )
        if enabled:
            self._schedule_auto_pipeline_once()
        return self.automation_status()

    def cancel_pair_async(self, pair_id: str, reason: str = "人工停止") -> str:
        operation = "cancel-pair-" + pair_id
        self._submit(operation, self.cancel_pair, pair_id, reason)
        return operation

    def pause_claude_sessions(self, pair_id: str, reason: str = "人工暂停模型") -> Dict[str, Any]:
        """Stop live Claude sessions while retaining the Pair and completed peer."""
        reason = re.sub(r"\s+", " ", str(reason or "人工暂停模型")).strip()[:1000]
        with self._pair_failure_lock(pair_id):
            pair = self._require_local_pair_edit(pair_id)
            if pair.get("status") not in ("running", "waiting_api_retry") or pair.get("stage") != "development":
                raise ValueError("只有开发中的 Pair 可以暂停 Claude 会话")
            live = self.db.all(
                "SELECT * FROM arm_runs WHERE pair_id=? AND status IN "
                "('running','developing','waiting_retry','waiting_api_retry') ORDER BY arm",
                (pair_id,),
            )
            stamp = now_iso()
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE pairs SET status='paused',stage='manual_pause',error=?,updated_at=? WHERE id=?",
                    (reason, stamp, pair_id),
                )
                for arm in live:
                    conn.execute(
                        "UPDATE arm_runs SET status='paused',error=?,updated_at=? WHERE id=?",
                        (reason, stamp, arm["id"]),
                    )
        stopped = []
        errors = []
        for arm in live:
            with self._arm_monitor_locks_guard:
                monitor_lock = self._arm_monitor_locks.setdefault(arm["id"], threading.Lock())
            if not monitor_lock.acquire(timeout=30):
                errors.append({"arm": arm["arm"], "error": "监控线程未退出；会话尚未安全关闭"})
                continue
            try:
                archived = self.claude.archive_failed_attempt(
                    arm, reason, prepare_retry=False,
                    count_development_failure=False, count_error_retry=False,
                )
                archive_event = self.db.one(
                    "SELECT detail_json FROM audit_events WHERE event_type='claude.failed_attempt_archived' "
                    "AND entity_id=? ORDER BY id DESC LIMIT 1", (arm["id"],),
                ) or {}
                archive_path = str(json.loads(archive_event.get("detail_json") or "{}").get("archive") or "")
                trace_dir = Path(archive_path) / "traces" if archive_path else None
                self.db.execute(
                    "UPDATE arm_runs SET status='paused',trace_path=?,error=?,updated_at=? WHERE id=?",
                    (str(trace_dir) if trace_dir and trace_dir.is_dir() else "",
                     reason, now_iso(), arm["id"]),
                )
                stopped.append({"arm": arm["arm"], "archive": archive_path,
                                "traceExported": bool(trace_dir and trace_dir.is_dir())})
            except Exception as exc:
                errors.append({"arm": arm["arm"], "error": redact(str(exc))[-1000:]})
            finally:
                monitor_lock.release()
        self.db.audit("pair.claude_sessions_paused", "pair", pair_id, {
            "reason": reason, "stopped": stopped, "errors": errors,
            "completed_peer_preserved": True, "failure_count_changed": False,
        })
        return {"pairId": pair_id, "stopped": stopped, "errors": errors}

    def cancel_pair(self, pair_id: str, reason: str = "人工停止") -> Dict[str, Any]:
        """Stop one Pair while preserving each active Arm's code and native trace."""
        reason = re.sub(r"\s+", " ", str(reason or "人工停止")).strip()[:1000]
        with self._pair_failure_lock(pair_id):
            pair = self._pair(pair_id)
            if pair.get("status") in ("completed", "cancelled"):
                return {"pairId": pair_id, "status": pair.get("status"), "stoppedArms": []}
            stamp = now_iso()
            self.db.execute(
                """UPDATE pairs SET status='cancelled',stage='cancelled',error=?,updated_at=?
                     WHERE id=?""",
                (reason, stamp, pair_id),
            )
            self.db.execute(
                """UPDATE delivery_submissions SET status='discarded',error=?,updated_at=?
                     WHERE pair_id=?""",
                (reason, stamp, pair_id),
            )
            stopped: List[str] = []
            active_statuses = {
                "queued", "running", "developing", "waiting_retry", "waiting_api_retry",
                "checkpointing", "exported",
            }
            for arm in self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,)):
                if arm.get("status") not in active_statuses:
                    continue
                try:
                    self.claude.archive_failed_attempt(
                        arm, reason, prepare_retry=False,
                        count_development_failure=False, count_error_retry=False,
                    )
                except Exception as exc:
                    self.db.execute(
                        """UPDATE arm_runs SET status='failed',error=?,finished_at=?,updated_at=?
                             WHERE id=?""",
                        ((reason + "；停止现场时出现错误：" + redact(str(exc)))[-3000:],
                         now_iso(), now_iso(), arm["id"]),
                    )
                stopped.append(str(arm.get("arm") or ""))
            self._invalidate_recordings(pair_id, reason=reason)
            self.db.audit("pair.cancelled", "pair", pair_id, {
                "reason": reason, "stopped_arms": stopped,
                "code_and_trace_preserved": True,
            })
            return {"pairId": pair_id, "status": "cancelled", "stoppedArms": stopped}

    def _require_local_pair_edit(self, pair_id: str) -> Dict[str, Any]:
        """Allow local controls only before a record is bound to SOLO-QA."""
        pair = self._pair(pair_id)
        delivery = self.db.one(
            "SELECT * FROM delivery_submissions WHERE pair_id=?", (pair_id,),
        ) or {}
        locked_statuses = {
            "submitting", "submitted", "qc_pending", "qc_passed", "needs_fix",
            "SUBMITTED", "QC_PASSED", "PENDING_FIX",
        }
        if (delivery.get("remote_id") or delivery.get("remote_url")
                or delivery.get("submitted_at")
                or delivery.get("status") in locked_statuses):
            raise ValueError("已提交或已绑定 SOLO-QA 的数据不能重置项目或编辑难度")
        return pair

    def reset_pair_retries(self, pair_id: str) -> Dict[str, Any]:
        """Clear retry counters without touching code, traces or current work."""
        with self._pair_failure_lock(pair_id):
            pair = self._require_local_pair_edit(pair_id)
            if pair.get("stage") in (
                "task_replacement", "replaced", "replacement_failed", "cancelled",
            ) or pair.get("status") == "cancelled":
                raise ValueError("自动换题已启动或原 Pair 已退役，重置次数不能恢复该项目")
            arms = self.db.all(
                "SELECT id,arm,status,attempt_no,error_retry_count,api_retry_count FROM arm_runs WHERE pair_id=?",
                (pair_id,),
            )
            stamp = now_iso()
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE pairs SET development_failure_count=0,updated_at=? WHERE id=?",
                    (stamp, pair_id),
                )
                conn.execute(
                    """UPDATE arm_runs SET error_retry_count=0,api_retry_count=0,
                       api_retry_after=NULL,last_api_error='',updated_at=? WHERE pair_id=?""",
                    (stamp, pair_id),
                )
            self.db.audit("pair.retry_budget_reset", "pair", pair_id, {
                "previousPairFailureCount": int(pair.get("development_failure_count") or 0),
                "previousArms": arms,
                "codeAndTracePreserved": True,
            })
        return self.pair_detail(pair_id)

    def restart_active_arm_async(self, pair_id: str, arm_name: str) -> str:
        """Operator restart of an active Claude process without charging a failure."""
        if arm_name not in ("A", "B"):
            raise ValueError("只能选择 A 或 B")
        self._require_local_pair_edit(pair_id)
        operation = "operator-restart-%s-%s" % (pair_id, arm_name)
        if not self._submit_monitor(operation, self.restart_active_arm, pair_id, arm_name):
            raise ValueError("该侧的人工重启已经在进行")
        return operation

    def restart_active_arm(self, pair_id: str, arm_name: str) -> Dict[str, Any]:
        """Archive a live attempt, then resume the original task from its baseline.

        This is an explicit operator interruption, not a development failure.
        The Pair-wide failure budget and the other Arm remain unchanged.
        """
        if arm_name not in ("A", "B"):
            raise ValueError("只能选择 A 或 B")
        arm_id = "%s-%s" % (pair_id, arm_name.lower())
        with self._pair_failure_lock(pair_id):
            pair = self._require_local_pair_edit(pair_id)
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or {}
            if pair.get("stage") != "development" or arm.get("status") not in ("running", "developing"):
                raise ValueError("该侧不是运行中的开发会话，不能重启")
            if self._pair_development_budget_exhausted(pair_id):
                raise ValueError("Pair 开发失败次数已达上限，不能自动重启")
            self.db.execute(
                "UPDATE arm_runs SET status='manual_preparing',updated_at=? WHERE id=?",
                (now_iso(), arm_id),
            )
        with self._arm_monitor_locks_guard:
            monitor_lock = self._arm_monitor_locks.setdefault(arm_id, threading.Lock())
        if not monitor_lock.acquire(timeout=60):
            with self._pair_failure_lock(pair_id):
                self.db.execute(
                    "UPDATE arm_runs SET status='developing',updated_at=? WHERE id=? AND status='manual_preparing'",
                    (now_iso(), arm_id),
                )
            raise RuntimeError("旧会话监控仍在收尾，未中断开发进程")
        try:
            with self._pair_failure_lock(pair_id):
                arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or {}
                if arm.get("status") != "manual_preparing":
                    raise ValueError("该侧在重启前已完成或改变状态，未触碰旧会话")
                task = self.db.one(
                    "SELECT t.prompt FROM pairs p JOIN tasks t ON t.id=p.task_id WHERE p.id=?",
                    (pair_id,),
                ) or {}
                prompt = str(task.get("prompt") or "")
                if not prompt:
                    raise ValueError("数据库原题面缺失，不能重启")
                restarted = self._restart_arm_from_baseline(
                    pair_id, arm, prompt, "用户要求重启开发终端；原生轨迹和工作区已归档",
                    count_development_failure=False, count_error_retry=False,
                )
                self.db.audit("claude.operator_restarted_active_arm", "arm_run", arm_id, {
                    "pair_id": pair_id, "arm": arm_name,
                    "failureBudgetUnchanged": True,
                    "newStatus": restarted.get("status"),
                })
        finally:
            monitor_lock.release()
        self._submit_monitor(
            "operator-monitor-%s-%s" % (arm_id, uuid.uuid4().hex[:8]),
            self._recover_pending_retry, pair_id, arm_id, prompt,
        )
        return self.pair_detail(pair_id)

    def queue_arm_manually_async(self, pair_id: str, arm_name: str,
                                 exception_approval_id: int = 0) -> str:
        if arm_name not in ("A", "B"):
            raise ValueError("只能选择 A 或 B")
        self._require_local_pair_edit(pair_id)
        operation = "manual-queue-%s-%s" % (pair_id, arm_name)
        self._submit(operation, self.queue_arm_manually, pair_id, arm_name,
                     exception_approval_id)
        return operation

    def queue_arm_manually(self, pair_id: str, arm_name: str,
                           exception_approval_id: int = 0) -> Dict[str, Any]:
        """Archive and reset one Arm, then put it in the bounded Pair queue."""
        if arm_name not in ("A", "B"):
            raise ValueError("只能选择 A 或 B")
        operation = "manual-queue-%s-%s" % (pair_id, arm_name)
        with self._pair_failure_lock(pair_id):
            pair = self._require_local_pair_edit(pair_id)
            arm = self.db.one(
                "SELECT * FROM arm_runs WHERE pair_id=? AND arm=?", (pair_id, arm_name),
            )
            if not arm or not pair.get("baseline_sha"):
                raise ValueError("请先完成仓库和 A/B 共同基线准备")
            repository = self.db.one(
                "SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,),
            ) or {}
            if repository.get("status") != "ready":
                raise ValueError("仓库尚未准备完成，不能重新排队")
            if arm.get("status") == "manual_waiting":
                return self.pair_detail(pair_id)

            paused_recovery = (
                (pair.get("status") == "paused"
                 and pair.get("stage") == "manual_pause"
                 and arm.get("status") in {"paused", "infrastructure_paused"})
                or (pair.get("status") == "running"
                    and pair.get("stage") == "development"
                    and arm.get("status") in {"paused", "infrastructure_paused"})
            )
            stable = {"failed", "completed", "exported", "cancelled"}
            unsent = (
                arm.get("status") in {"queued", "waiting_retry", "waiting_api_retry"}
                and not arm.get("prompt_sent_at") and not arm.get("image_id")
            )
            if arm.get("status") not in stable and not unsent and not paused_recovery:
                raise ValueError("该侧仍在运行或收尾，请完成或停止后再重置排队")
            active_peer = self.db.one(
                """SELECT arm,status FROM arm_runs WHERE pair_id=? AND arm<>?
                     AND (status IN ('running','developing','checkpointing')
                          OR (status='waiting_retry' AND image_id<>'')) LIMIT 1""",
                (pair_id, arm_name),
            )
            if active_peer:
                raise ValueError("%s 侧仍在运行或收尾，不能在此时改写 Pair 状态" % active_peer["arm"])
            exception_requeue = False
            if pair.get("stage") == "replaced":
                peer = self.db.one(
                    "SELECT arm,status,commit_sha FROM arm_runs WHERE pair_id=? AND arm<>?",
                    (pair_id, arm_name),
                ) or {}
                verified_peer = bool(peer.get("status") == "completed" and peer.get("commit_sha")
                    and self.db.one(
                        """SELECT id FROM artifact_checks WHERE pair_id=? AND arm=?
                             AND commit_sha=? AND status='passed' LIMIT 1""",
                        (pair_id, peer.get("arm"), peer.get("commit_sha")),
                    ))
                if exception_approval_id:
                    approval = self.db.one(
                        """SELECT detail_json FROM audit_events WHERE id=? AND entity_id=?
                             AND event_type='pair.manual_arm_requeue_exception_approved'""",
                        (exception_approval_id, pair_id),
                    ) or {}
                    try:
                        details = json.loads(approval.get("detail_json") or "{}")
                    except (TypeError, ValueError):
                        details = {}
                    peer_check = self.db.one(
                        """SELECT id FROM artifact_checks WHERE id=? AND pair_id=? AND arm=?
                             AND commit_sha=? AND status='observed_failed'""",
                        (details.get("peerCheckId"), pair_id, peer.get("arm"),
                         peer.get("commit_sha")),
                    )
                    used = self.db.one(
                        """SELECT id FROM audit_events WHERE event_type=
                             'pair.manual_arm_requeue_exception_used' AND entity_id=?
                             AND json_extract(detail_json,'$.approvalId')=? LIMIT 1""",
                        (pair_id, exception_approval_id),
                    )
                    maximum = max(1, int(self.db.setting("development_max_attempts", 2)))
                    exception_requeue = bool(
                        not verified_peer and peer.get("status") == "completed"
                        and peer.get("commit_sha") and peer_check and not used
                        and details.get("arm") == arm_name
                        and details.get("peerCommit") == peer.get("commit_sha")
                        and details.get("grant") == "one_extra_attempt"
                        and int(pair.get("development_failure_count") or 0) >= maximum
                    )
                if (pair.get("status") != "failed" or arm.get("status") != "failed"
                        or not (verified_peer or exception_requeue)):
                    raise ValueError("已换题的 Pair 仅允许补跑失败侧，且另一侧须已完成 Docker 验收")
            elif exception_approval_id:
                raise ValueError("此人工特批仅适用于已换题的失败侧")
            if pair.get("stage") in (
                "repository", "artifact_validation", "difficulty_review", "recording",
                "gsb_ready", "gsb_confirmation", "task_replacement",
                "replacement_failed", "cancelled",
            ) or pair.get("status") == "deferred_priority":
                raise ValueError("项目正在准备、验收、评审或已经退役，当前不能重新排队")
            if self.db.one(
                "SELECT id FROM artifact_checks WHERE pair_id=? AND status='running' LIMIT 1", (pair_id,),
            ):
                raise ValueError("项目仍有 Docker 验收在运行，请等待结束后再重排")
            if self.db.one(
                """SELECT id FROM recording_attempts WHERE pair_id=?
                     AND status IN ('starting','recording','stopping') LIMIT 1""", (pair_id,),
            ):
                raise ValueError("项目仍有录像在运行，请先停止并保存")
            if self.db.one(
                """SELECT id FROM codex_jobs WHERE pair_id=? AND status='running'
                     AND job_type<>'bug_discovery' LIMIT 1""", (pair_id,),
            ):
                raise ValueError("项目仍有复评或分析作业运行，请等待结束后再重排")
            with self._future_lock:
                busy = any(
                    not future.done() and key != operation
                    and (str(arm["id"]) in key or key in (
                        "start-" + pair_id, "replace-task-" + pair_id,
                    ))
                    for key, future in self._futures.items()
                )
            if busy:
                raise ValueError("该侧仍有后台工作正在收尾，请稍后再重排")

            previous_review = self.db.one(
                "SELECT * FROM difficulty_reviews WHERE pair_id=?", (pair_id,),
            ) or {}
            previous_gsb = self.db.one(
                "SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,),
            ) or {}
            self.db.audit("pair.manual_arm_requeue_snapshot", "pair", pair_id, {
                "selectedArm": arm_name,
                "previousPair": {
                    "status": pair.get("status"), "stage": pair.get("stage"),
                    "winner": pair.get("winner"),
                    "developmentFailureCount": pair.get("development_failure_count"),
                },
                "previousArm": {
                    key: arm.get(key) for key in (
                        "id", "status", "session_id", "prompt_id", "trace_path",
                        "commit_sha", "attempt_no", "error_retry_count", "api_retry_count",
                    )
                },
                "previousDifficultyReview": previous_review,
                "previousGsb": previous_gsb,
                "exceptionApprovalId": exception_approval_id if exception_requeue else None,
            })
            self.db.execute(
                "UPDATE arm_runs SET status='manual_preparing',updated_at=? WHERE id=?",
                (now_iso(), arm["id"]),
            )
            try:
                self.claude.archive_failed_attempt(
                    arm, "人工重置并重新排队 %s 侧，旧代码与轨迹已归档" % arm_name,
                    prepare_retry=True, count_development_failure=True,
                    count_error_retry=False,
                )
                self.git.reset_arm_to_baseline(pair_id, arm_name)
            except Exception as exc:
                self.db.execute(
                    "UPDATE arm_runs SET status='failed',error=?,updated_at=? WHERE id=?",
                    (("人工重排准备失败：" + redact(str(exc)))[-2000:], now_iso(), arm["id"]),
                )
                raise

            self._invalidate_recordings(
                pair_id, [arm_name], "人工重跑 %s 侧；旧录像只保留在尝试历史" % arm_name,
            )
            stamp = now_iso()
            maximum = max(1, int(self.db.setting("development_max_attempts", 2)))
            failure_count = (
                int(pair.get("development_failure_count") or 0) if paused_recovery else
                maximum - 1 if exception_requeue else 0
            )
            with self.db.transaction() as conn:
                conn.execute(
                    """UPDATE arm_runs SET status='manual_waiting',api_retry_after=NULL,
                       last_api_error='',error='',error_retry_count=0,api_retry_count=0,
                       updated_at=? WHERE id=?""",
                    (stamp, arm["id"]),
                )
                conn.execute(
                    """UPDATE pairs SET status='repair_pending',stage='manual_arm_requeue_pending',
                       development_failure_count=?,
                       winner='',error='',completed_at=NULL,updated_at=? WHERE id=?""",
                    (failure_count, stamp, pair_id),
                )
                conn.execute(
                    """UPDATE difficulty_reviews SET status='pending',error='所选侧已人工重排，等待新产物后复评',
                       reviewed_at=NULL,updated_at=? WHERE pair_id=?""",
                    (stamp, pair_id),
                )
                conn.execute(
                    """UPDATE gsb_reviews SET status='draft',confirmed_by='',confirmed_at=NULL,
                       final_verdict='',final_reason='',evidence_version='',updated_at=? WHERE pair_id=?""",
                    (stamp, pair_id),
                )
                conn.execute(
                    """UPDATE delivery_submissions SET status='needs_review',error='',hidden_at=NULL,
                       payload_sha256='',updated_at=? WHERE pair_id=?""",
                    (stamp, pair_id),
                )
                if pair.get("chain_id"):
                    conn.execute(
                        """UPDATE project_chains SET status='active',followup_completed=0,
                           completed_at=NULL,updated_at=? WHERE id=?""",
                        (stamp, pair["chain_id"]),
                    )
                if exception_requeue:
                    conn.execute(
                        """INSERT INTO audit_events(event_type,entity_type,entity_id,detail_json,created_at)
                           VALUES('pair.manual_arm_requeue_exception_used','pair',?,?,?)""",
                        (pair_id, json.dumps({
                            "approvalId": exception_approval_id, "arm": arm_name,
                            "peerCommit": peer.get("commit_sha"),
                            "remainingDevelopmentFailures": maximum - failure_count,
                        }, ensure_ascii=False), stamp),
                    )
            self.db.audit("claude.manual_arm_requeue_queued", "arm_run", arm["id"], {
                "pair_id": pair_id, "arm": arm_name,
                "baseline_sha": pair.get("baseline_sha"),
                "peerPreserved": True, "failureBudgetReset": not paused_recovery,
                "pausedRecovery": paused_recovery,
                "exceptionApprovalId": exception_approval_id if exception_requeue else None,
                "remainingDevelopmentFailures": maximum - failure_count,
            })
        # Try immediately; the scheduler will keep the pending request when
        # every Pair slot is currently occupied.
        self._resume_one_manual_arm_requeue(pair_id)
        return self.pair_detail(pair_id)

    def _submit_auto(self, operation: str, fn, *args) -> bool:
        """Submit an idempotent pipeline action with a small failure backoff."""
        if self.db.setting("pipeline_drain", False):
            return False
        with self._future_lock:
            existing = self._futures.get(operation)
            if existing and not existing.done():
                return False
            if time.monotonic() < self._auto_retry_after.get(operation, 0.0):
                return False
            if operation.startswith(("bugs-", "bug-", "feature-")) and not operation_ready(self.db, operation):
                return False

            def run_action():
                try:
                    result = fn(*args)
                    self.db.execute("DELETE FROM pipeline_operations WHERE operation=?", (operation,))
                    with self._future_lock:
                        self._auto_retry_after.pop(operation, None)
                    return result
                except Exception as exc:
                    if operation.startswith(("bugs-", "bug-", "feature-")):
                        backoff = operation_failed(self.db, operation, redact(str(exc)))
                        self.db.audit("automation.operation_backoff", "operation", operation, backoff)
                    with self._future_lock:
                        self._auto_retry_after[operation] = time.monotonic() + (
                            300 if operation.startswith("delivery-assessment-") else 30
                        )
                    self.db.audit("automation.action_failed", "operation", operation, {
                        "error": redact(str(exc))[-2000:],
                    })
                    raise

            self._futures[operation] = self.executor.submit(run_action)
            return True

    def _schedule_auto_pipeline_once(self) -> Dict[str, Any]:
        """Advance every active Pair and refill to the configured Pair target."""
        if self.db.setting("pipeline_drain", False):
            return self.automation_status()
        if not self._automation_lock.acquire(blocking=False):
            return self.automation_status()
        try:
            waiting_api_arms = int((self.db.one(
                """SELECT COUNT(*) count FROM arm_runs a JOIN pairs p ON p.id=a.pair_id
                     WHERE a.status='waiting_api_retry' AND p.stage='development'
                       AND p.status IN ('running','waiting_api_retry')"""
            ) or {"count": 0})["count"])
            api_cooling = self._api_cooldown_active()
            start_blocked = api_cooling or waiting_api_arms > 0 or self._pair_start_blocked()
            pair_start_scheduled = False
            priority_partial_pairs = self._priority_partial_pair_ids()
            active_pairs = self.db.all(
                """SELECT * FROM pairs WHERE status IN ('queued','running','review')
                   ORDER BY created_at,id"""
            )
            active_pairs.sort(key=lambda pair: (
                0 if pair["id"] in priority_partial_pairs else 1,
                pair["created_at"], pair["id"],
            ))
            recording_pairs: List[Dict[str, Any]] = []
            for pair in active_pairs:
                pair_id, stage = pair["id"], pair["stage"]
                if stage == "repository":
                    self._submit_auto("repo-" + pair_id, self.prepare_pair_repository, pair_id)
                elif stage == "ready_to_start":
                    # A Pair with one completed Arm gets the next development
                    # terminal before a fresh Pair can start either side.  In
                    # addition to ordering the scan above, this guard removes
                    # the race between the monitor and operation executors.
                    task_kind = (self.db.one(
                        "SELECT task_type FROM tasks WHERE id=?", (pair["task_id"],),
                    ) or {}).get("task_type")
                    if (not (self._task_mix_refill_deficit() and task_kind != "bugfix")
                            and not priority_partial_pairs and not start_blocked
                            and not pair_start_scheduled):
                        pair_start_scheduled = self._submit_auto(
                            "start-" + pair_id, self.start_pair, pair_id,
                        )
                        if pair_start_scheduled:
                            self.db.set_setting(
                                "claude_pair_start_after",
                                (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(timespec="seconds"),
                            )
                elif stage in ("development", "artifact_validation"):
                    if stage == "development":
                        if self._finish_exhausted_pair_after_peer(pair_id):
                            continue
                        arm_statuses = {
                            arm["status"] for arm in self.db.all(
                                "SELECT status FROM arm_runs WHERE pair_id=?", (pair_id,),
                            )
                        }
                        if "failed" in arm_statuses and "completed" in arm_statuses:
                            self._refresh_pair_after_arm(pair_id)
                            current = self.db.one(
                                "SELECT status,stage FROM pairs WHERE id=?", (pair_id,),
                            ) or {}
                            if current.get("status") != "running" or current.get("stage") != "development":
                                continue
                        self._schedule_active_arm_monitors(pair_id)
                        self._schedule_pending_arm_retries(pair_id)
                        self._schedule_checkpoint_pushes(pair_id)
                    self._schedule_completed_arm_validations(pair_id)
                    recording_pairs.append(pair)
                elif stage == "lineage_repair_pending":
                    self._submit_auto(
                        "lineage-repair-" + pair_id, self._run_lineage_repair, pair_id,
                    )
                elif stage == "difficulty_review":
                    self._submit_auto(
                        "difficulty-" + pair_id,
                        self.reassess_actual_difficulty,
                        pair_id,
                    )
                elif stage == "recording":
                    recording_pairs.append(pair)
                elif stage == "gsb_ready":
                    # A stale stage must not submit a GSB job every scheduler
                    # tick while one side is still awaiting Docker evidence.
                    self._schedule_completed_arm_validations(pair_id)
                    if self._gsb_evidence_ready(pair_id):
                        self._submit_auto("gsb-" + pair_id, self.generate_gsb, pair_id)
                elif stage == "gsb_confirmation":
                    review = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,)) or {}
                    if review.get("status") == "draft" and review.get("a_reason") and review.get("b_reason"):
                        reviewer = str(self.db.setting("git_author_name", "刘昱") or "刘昱").strip() + "（按授权默认确认）"
                        self._submit_auto(
                            "confirm-gsb-" + pair_id, self.confirm_gsb, pair_id,
                            review.get("verdict", ""), review.get("a_reason", ""),
                            review.get("b_reason", ""), reviewer,
                        )

            self._schedule_next_automatic_recording(recording_pairs)
            self._schedule_missing_delivery_assessment()

            # Evidence-only re-recording does not own a Claude terminal or a
            # development Pair slot. Resume every queued revalidation before
            # applying the development concurrency limit; the recorder itself
            # remains globally single-flight.
            while self._resume_one_recording_revalidation_pair():
                pass

            active_count = int((self.db.one(
                "SELECT COUNT(*) count FROM development_pairs WHERE status IN ('queued','running','review')"
            ) or {"count": 0})["count"])
            self._resume_one_lineage_normalization()
            configured = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
            pair_limit = max(1, min(MAX_PAIR_PROJECTS, configured))
            # Recording and GSB have their own workers and never reserve a
            # development Pair start slot, including through this backlog
            # safety gate. Keep the gate for Docker/difficulty validation.
            validation_backlog = int((self.db.one(
                "SELECT COUNT(*) count FROM pairs WHERE status IN ('running','review') "
                "AND stage IN ('artifact_validation','difficulty_review')"
            ) or {"count": 0})["count"])
            if validation_backlog >= int(self.db.setting("postprocess_backlog_limit", 8)):
                return self.automation_status()
            waiting_api_arms = int((self.db.one(
                """SELECT COUNT(*) count FROM arm_runs a JOIN pairs p ON p.id=a.pair_id
                     WHERE a.status='waiting_api_retry' AND p.stage='development'
                       AND p.status IN ('running','review','waiting_api_retry')"""
            ) or {"count": 0})["count"])
            if waiting_api_arms:
                if (bool(self.db.setting("claude_api_auto_retry_enabled", True))
                        and not self._api_cooldown_active()
                        and not self._api_probe_blocked()):
                    active_count += self._schedule_due_api_retries(
                        max(0, pair_limit - active_count),
                    )
                return self.automation_status()
            if self._api_cooldown_active():
                return self.automation_status()

            while active_count < pair_limit and self._resume_one_single_arm_repair():
                active_count += 1
            while active_count < pair_limit and self._resume_one_manual_arm_requeue():
                active_count += 1
            while active_count < pair_limit and self._resume_one_lineage_repair():
                active_count += 1
            if active_count < pair_limit and self._resume_one_baseline_preflight_retry():
                active_count += 1
            if active_count < pair_limit and self._resume_one_manual_full_pair_retry():
                active_count += 1
            if active_count < pair_limit and self._resume_one_environment_failed_pair():
                active_count += 1
            if active_count < pair_limit and self._resume_one_reusable_pair():
                active_count += 1
            while active_count < pair_limit and self._resume_one_deferred_prepared_pair():
                active_count += 1
            while active_count < pair_limit:
                task = self._next_ready_task()
                if not task:
                    break
                try:
                    pair = self.create_pair(task["id"])
                except ValueError:
                    # A failed-Pair replacement can claim the same ready task
                    # after selection but before this scheduler creates it.
                    # Only that changed task state is a harmless race.
                    current = self.db.one("SELECT status FROM tasks WHERE id=?", (task["id"],))
                    if current and current["status"] != "ready":
                        active_count = int((self.db.one(
                            "SELECT COUNT(*) count FROM development_pairs "
                            "WHERE status IN ('queued','running','review')"
                        ) or {"count": 0})["count"])
                        continue
                    raise
                self._submit_auto("repo-" + pair["id"], self.prepare_pair_repository, pair["id"])
                active_count += 1

            # Existing approved questions are consumed first. Refill begins
            # only when no additional approved question can fill the target.
            if active_count < pair_limit:
                self._schedule_refill_once()
            return self.automation_status()
        finally:
            self._automation_lock.release()

    def _schedule_next_automatic_recording(self, pairs: List[Dict[str, Any]]) -> None:
        if self.db.one(
            "SELECT id FROM recording_attempts WHERE status IN ('starting','recording') LIMIT 1"
        ):
            return
        for pair in pairs:
            pair_id = pair["id"]
            for arm in ("A", "B"):
                run = self.db.one("SELECT commit_sha FROM arm_runs WHERE pair_id=? AND arm=?", (pair_id, arm)) or {}
                commit_sha = str(run.get("commit_sha") or "")
                if not commit_sha:
                    continue
                check = self.db.one(
                    """SELECT status FROM artifact_checks
                       WHERE pair_id=? AND arm=? AND commit_sha=?
                       ORDER BY created_at DESC,id DESC LIMIT 1""",
                    (pair_id, arm, commit_sha),
                ) or {}
                # A product failure is final comparison evidence.  It has no
                # runnable artifact to record, but must not prevent the other,
                # successfully validated Arm from being recorded.  Final
                # failures are normalized to observed_failed before this
                # scheduler runs, so only passed artifacts are recordable.
                if check.get("status") != "passed":
                    continue
                recording = self.db.one(
                    """SELECT id FROM recordings WHERE pair_id=? AND arm=? AND status='passed'
                       AND commit_match=1 AND commit_sha=?""", (pair_id, arm, commit_sha),
                )
                if recording:
                    continue
                retry_window = self.db.one(
                    """SELECT created_at FROM audit_events
                       WHERE event_type='recording.retry_window_started' AND entity_id=?
                       ORDER BY id DESC LIMIT 1""", (pair_id,),
                ) or {}
                cutoff = str(retry_window.get("created_at") or "")
                failures = int((self.db.one(
                    """SELECT COUNT(*) count FROM recording_attempts WHERE pair_id=? AND arm=?
                       AND commit_sha=? AND interaction_mode<>'manual' AND status='failed'
                       AND (?='' OR created_at>=?)""",
                    (pair_id, arm, commit_sha, cutoff, cutoff),
                ) or {"count": 0})["count"])
                latest_failure = self.db.one(
                    """SELECT error FROM recording_attempts WHERE pair_id=? AND arm=?
                       AND commit_sha=? AND interaction_mode<>'manual' AND status='failed'
                       AND (?='' OR created_at>=?) ORDER BY created_at DESC LIMIT 1""",
                    (pair_id, arm, commit_sha, cutoff, cutoff),
                ) or {}
                deterministic = self._recording_failure_is_deterministic(
                    str(latest_failure.get("error") or "")
                )
                if failures >= 3 or deterministic:
                    if pair.get("stage") != "recording":
                        # A recording failure must never terminate a healthy peer.
                        continue
                    stamp = now_iso()
                    if deterministic:
                        message = "自动录像遇到确定性演示错误，已停止重试，请检查后重新录制"
                    else:
                        message = "自动录像连续 3 次失败，请人工检查后重新录制"
                    self.db.execute(
                        """UPDATE pairs SET status='failed',stage='recording_failed',
                           error=?,updated_at=? WHERE id=?""",
                        (message, stamp, pair_id),
                    )
                    self.db.audit("automation.recording_exhausted", "pair", pair_id, {
                        "arm": arm,
                        "reason": "deterministic_failure" if deterministic else "retry_limit",
                        "last_error": redact(str(latest_failure.get("error") or ""))[-1000:],
                    })
                    break
                try:
                    self.start_recording(pair_id, arm, manual=False)
                except Exception as exc:
                    self.db.audit("automation.recording_start_failed", "pair", pair_id, {
                        "arm": arm, "error": redact(str(exc))[-2000:],
                    })
                return
            self.refresh_recording_stage(pair_id)

    def _schedule_due_api_retries(self, available_slots: int) -> int:
        """Resume one cooled-down Arm without charging an already-active Pair twice."""
        if self._available_development_arm_slots() <= 0:
            return 0
        maximum = max(1, int(self.db.setting("development_max_attempts", 2)))
        due = self.db.all(
            """SELECT a.id arm_id,a.pair_id,p.status pair_status,t.prompt FROM arm_runs a
                 JOIN pairs p ON p.id=a.pair_id JOIN tasks t ON t.id=p.task_id
                WHERE a.status='waiting_api_retry' AND a.prompt_sent_at IS NULL
                  AND p.stage='development'
                  AND p.development_failure_count<?
                  AND (p.status IN ('running','review')
                       OR (? > 0 AND p.status='waiting_api_retry'))
                  AND (a.api_retry_after IS NULL OR a.api_retry_after<=?)
                ORDER BY CASE WHEN p.status IN ('running','review') THEN 0 ELSE 1 END,
                         COALESCE(a.api_retry_after,a.updated_at),a.updated_at,a.id""",
            (maximum, available_slots, now_iso()),
        )
        if not due:
            return 0
        # A single Arm is the probe. Launching both A/B sides together simply
        # consumes the same saturated provider pool twice.
        row = due[0]
        submitted = self._submit_monitor(
            "api-retry-" + row["arm_id"], self._recover_api_retry,
            row["pair_id"], row["arm_id"], row["prompt"],
        )
        if not submitted:
            return 0
        stamp = now_iso()
        self.db.execute(
            """UPDATE pairs SET status='running',error='',updated_at=?
                 WHERE id=? AND status='waiting_api_retry' AND stage='development'""",
            (stamp, row["pair_id"]),
        )
        self.db.set_setting(
            "claude_api_probe_after",
            (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(timespec="seconds"),
        )
        return 1 if row["pair_status"] == "waiting_api_retry" else 0

    def _schedule_active_arm_monitors(self, pair_id: str) -> None:
        """Reconnect monitors to live Claude sessions after a service restart."""
        task = self.db.one(
            """SELECT t.prompt FROM tasks t JOIN pairs p ON p.task_id=t.id
               WHERE p.id=?""", (pair_id,),
        ) or {}
        prompt = str(task.get("prompt") or "")
        if not prompt:
            return
        for arm in self.db.all(
            """SELECT id FROM arm_runs WHERE pair_id=? AND status='developing'
               AND prompt_sent_at IS NOT NULL""", (pair_id,),
        ):
            self._submit_monitor(
                "monitor-" + arm["id"], self._monitor_arm,
                pair_id, arm["id"], prompt,
            )

    def _schedule_pending_arm_retries(self, pair_id: str) -> None:
        """Start deferred Arms as terminal slots open and recover launch debris."""
        if self.db.setting("pipeline_drain", False):
            return
        if self._api_cooldown_active():
            return
        if self._pair_development_budget_exhausted(pair_id):
            # The second Pair-wide failure is terminal for new sessions.  A
            # peer that is already running may finish naturally, but a queued
            # or deferred side must not consume another terminal slot.
            self._finish_exhausted_pair_after_peer(pair_id)
            return
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        task = self.db.one(
            """SELECT t.prompt FROM tasks t JOIN pairs p ON p.task_id=t.id
               WHERE p.id=?""", (pair_id,),
        ) or {}
        prompt = str(task.get("prompt") or "")
        if not prompt:
            return
        available = self._available_development_arm_slots()
        for arm in self.db.all(
            """SELECT * FROM arm_runs WHERE pair_id=? AND prompt_sent_at IS NULL
               AND status IN ('running','queued','waiting_retry')
               ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'waiting_retry' THEN 1 ELSE 2 END,
                        arm""", (pair_id,),
        ):
            already_running = arm.get("status") == "running"
            owns_slot = bool(arm.get("status") == "waiting_retry" and arm.get("image_id"))
            capacity_deferred = "等待 Claude 开发终端空位" in str(arm.get("error") or "")
            if not already_running and not owns_slot and available <= 0:
                continue
            try:
                updated = datetime.fromisoformat(
                    str(arm.get("updated_at") or "").replace("Z", "+00:00")
                )
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if (updated > cutoff and arm.get("status") != "queued"
                        and not capacity_deferred):
                    continue
            except (TypeError, ValueError):
                continue
            submitted = self._submit_monitor(
                "retry-recover-" + arm["id"], self._recover_pending_retry,
                pair_id, arm["id"], prompt,
            )
            if submitted and not already_running and not owns_slot:
                available -= 1

    def _schedule_checkpoint_pushes(self, pair_id: str) -> None:
        for arm in self.db.all(
            """SELECT id FROM arm_runs WHERE pair_id=?
               AND status IN ('checkpointing','exported')""", (pair_id,),
        ):
            # The live monitor owns trace export and the first Git push. A
            # scheduler tick can observe its short-lived checkpointing state;
            # starting a second recovery worker here races export_and_stop and
            # can report a false "No such container" after the first worker
            # has already removed the verified container. Only recover when
            # there is no live monitor (for example after a service restart or
            # after an unexpected monitor exception).
            with self._future_lock:
                monitor = self._futures.get("monitor-" + arm["id"])
                monitor_active = bool(monitor and not monitor.done())
            if monitor_active:
                continue
            self._submit_auto(
                "checkpoint-push-" + arm["id"], self._resume_checkpointed_arm,
                pair_id, arm["id"],
            )

    def _resume_checkpointed_arm(self, pair_id: str, arm_id: str) -> Dict[str, Any]:
        """Resume either half of the post-Claude checkpoint after a restart.

        A service restart can land after the Arm is marked ``checkpointing``
        but before ``export_and_stop`` stores ``trace_path``.  In that state
        the implementation and native session still exist, so finish the
        trace export first and then continue with the normal Git delivery.
        """
        with self._checkpoint_lock(arm_id):
            arm = self.db.one(
                "SELECT * FROM arm_runs WHERE id=? AND pair_id=?", (arm_id, pair_id),
            ) or {}
            pair = self.db.one("SELECT status FROM pairs WHERE id=?", (pair_id,)) or {}
            if arm.get("status") not in ("checkpointing", "exported") or pair.get("status") not in ("running", "review"):
                return {"pairId": pair_id, "armId": arm_id, "skipped": True}
            trace_path_value = str(arm.get("trace_path") or "").strip()
            if not trace_path_value or not Path(trace_path_value).is_dir():
                if arm.get("status") != "checkpointing":
                    raise RuntimeError("已导出 Arm 缺少原生轨迹，不能继续推送")
                self.claude.export_and_stop(arm)
            return self._finish_checkpointed_arm_locked(pair_id, arm_id)

    def _finish_checkpointed_arm(self, pair_id: str, arm_id: str) -> Dict[str, Any]:
        with self._checkpoint_lock(arm_id):
            return self._finish_checkpointed_arm_locked(pair_id, arm_id)

    def _finish_checkpointed_arm_locked(self, pair_id: str, arm_id: str) -> Dict[str, Any]:
        arm = self.db.one("SELECT * FROM arm_runs WHERE id=? AND pair_id=?", (arm_id, pair_id)) or {}
        pair = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair_id,)) or {}
        if arm.get("status") not in ("checkpointing", "exported") or pair.get("status") not in ("running", "review"):
            return {"pairId": pair_id, "armId": arm_id, "skipped": True}
        trace_path_value = str(arm.get("trace_path") or "").strip()
        trace_path = Path(trace_path_value) if trace_path_value else None
        if trace_path is None or not trace_path.is_dir():
            raise RuntimeError("已完成 Arm 缺少导出的原生轨迹，不能继续推送")
        workspace = Path(str(arm.get("workspace_path") or ""))
        comparison_sha = self._arm_comparison_sha(pair_id, str(arm.get("arm") or ""))
        if (workspace / ".git").is_dir() and not self.claude.has_business_code(workspace, comparison_sha):
            # A completed Claude turn can contain only a plan. Preserve its
            # trace and workspace, but do not spend the Pair's shared failure
            # budget before the no-code window measured from prompt delivery.
            sent_at_text = str(arm.get("prompt_sent_at") or "")
            try:
                sent_at = datetime.fromisoformat(sent_at_text)
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
            except ValueError:
                sent_at = None
            deadline_minutes = max(0, int(self.db.setting("first_prompt_stop_minutes", 60)))
            if sent_at is not None and datetime.now(timezone.utc) < sent_at + timedelta(minutes=deadline_minutes):
                deferred_error = "Claude 会话已结束且没有业务代码；等待发题后 %d 分钟再计开发失败" % deadline_minutes
                if arm.get("error") != deferred_error:
                    self.db.execute(
                        "UPDATE arm_runs SET error=?,updated_at=? WHERE id=?",
                        (deferred_error, now_iso(), arm_id),
                    )
                    self.db.audit("claude.completed_no_code_deferred", "arm_run", arm_id, {
                        "prompt_sent_at": sent_at_text,
                        "deadline_minutes": deadline_minutes,
                        "counts_toward_pair_failure_limit": False,
                    })
                return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            task = self.db.one(
                "SELECT t.prompt FROM tasks t JOIN pairs p ON p.task_id=t.id WHERE p.id=?",
                (pair_id,),
            ) or {}
            return self._handle_attempt_failure(
                pair_id, arm, str(task.get("prompt") or ""),
                "Claude 会话已结束，但没有形成相对初始环境的代码产出",
            )
        try:
            sha = self.git.push_arm(pair_id, str(arm["arm"]))
        except Exception as exc:
            # A scheduler tick can observe the checkpoint while another push
            # is finishing.  If that worker has already completed the arm,
            # this stale push failure must not overwrite the successful state
            # with a misleading retry error.
            current = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or {}
            if current.get("status") == "completed" and current.get("commit_sha"):
                return current
            error = "已保留完成代码和轨迹，等待重试 Git 推送：%s" % redact(str(exc))
            self.db.execute(
                """UPDATE arm_runs SET error=?,updated_at=? WHERE id=?
                   AND status IN ('checkpointing','exported')""",
                (error[-3000:], now_iso(), arm_id),
            )
            self.db.audit("git.completed_arm_push_deferred", "arm_run", arm_id, {
                "error": redact(str(exc))[-1000:], "codePreserved": True, "tracePreserved": True,
            })
            raise
        stamp = now_iso()
        self.db.execute(
            """UPDATE arm_runs SET status='completed',commit_sha=?,error='',
               finished_at=?,updated_at=? WHERE id=?""",
            (sha, stamp, stamp, arm_id),
        )
        self.db.audit("claude.arm_completed", "arm_run", arm_id, {
            "arm": arm["arm"], "commit_sha": sha, "checkpointedDelivery": True,
        })
        self._refresh_pair_after_arm(pair_id)
        return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or {}

    @staticmethod
    def _recording_failure_is_deterministic(error: str) -> bool:
        lowered = str(error or "").casefold()
        return any(marker in lowered for marker in (
            "没有完成可见的真实功能操作",
            "没有检测到真实功能点击和成功接口请求",
            "真实接口请求失败",
            "没有成功完成业务接口请求",
            "没有成功的业务接口请求",
            "没有可演示的业务接口",
            "只有健康检查入口",
            "未能识别可演示的业务接口",
            "演示项目没有可用的浏览器入口",
        ))

    def _resume_one_reusable_pair(self) -> bool:
        # Reopening a preserved delivery consumes the same Pair slot as
        # creating a new Pair. Serialize both paths so the scheduler and the
        # replacement worker cannot claim the last free slot together.
        with self._pair_creation_lock:
            active_count = (self.db.one(
                "SELECT COUNT(*) count FROM development_pairs WHERE status IN ('queued','running','review')"
            ) or {"count": 0})["count"]
            configured_limit = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
            pair_limit = max(1, min(MAX_PAIR_PROJECTS, configured_limit))
            if active_count >= pair_limit:
                return False
            return self._resume_one_reusable_pair_locked()

    def _resume_one_single_arm_repair(self) -> bool:
        """Resume only the failed side after its completed peer was evaluated.

        These repairs remain outside the active Pair count while queued.  When
        capacity is available, the failed Arm may use only the Pair's remaining
        shared attempt budget from the common baseline.  The completed Arm, its
        native trace, commit, and terminal artifact evidence are deliberately
        left untouched.
        A peer with an observed product failure is still useful comparison
        evidence, so an explicitly queued opposite side may finish the Pair.
        """
        with self._pair_creation_lock:
            active_count = int((self.db.one(
                "SELECT COUNT(*) count FROM development_pairs WHERE status IN ('queued','running','review')"
            ) or {"count": 0})["count"])
            configured_limit = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
            pair_limit = max(1, min(MAX_PAIR_PROJECTS, configured_limit))
            if active_count >= pair_limit:
                return False
            maximum = max(1, int(self.db.setting("development_max_attempts", 2)))
            row = self.db.one(
                """SELECT p.id,p.chain_id,p.task_id,p.development_failure_count,
                          a.id arm_id,a.arm,t.prompt
                     FROM pairs p JOIN tasks t ON t.id=p.task_id
                     JOIN arm_runs a ON a.pair_id=p.id AND a.status='failed'
                    WHERE p.status='repair_pending' AND p.stage='single_arm_repair_pending'
                      AND p.development_failure_count<?
                      AND (SELECT COUNT(*) FROM arm_runs x WHERE x.pair_id=p.id)=2
                      AND (SELECT COUNT(*) FROM arm_runs x
                            WHERE x.pair_id=p.id AND x.status='failed')=1
                      AND (SELECT COUNT(*) FROM arm_runs x
                            WHERE x.pair_id=p.id AND x.status='completed' AND x.commit_sha<>''
                              AND EXISTS(SELECT 1 FROM artifact_checks c
                                WHERE c.pair_id=x.pair_id AND c.arm=x.arm
                                  AND c.commit_sha=x.commit_sha
                                  AND c.status IN ('passed','observed_failed')))=1
                    ORDER BY p.updated_at,p.created_at LIMIT 1""",
                (maximum,),
            )
            if not row:
                return False
            stamp = now_iso()
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE pairs SET status='running',stage='development',error='',
                         winner='',completed_at=NULL,updated_at=?
                         WHERE id=? AND status='repair_pending'
                           AND stage='single_arm_repair_pending'
                           AND development_failure_count<?""",
                    (stamp, row["id"], maximum),
                ).rowcount
                if not changed:
                    return False
                conn.execute(
                    """UPDATE arm_runs SET status='queued',image_id='',session_id='',prompt_id='',
                         trace_path='',commit_sha='',result='',warning_at=NULL,error='',
                         prompt_sent_at=NULL,finished_at=NULL,attempt_no=1,error_retry_count=0,
                         api_retry_count=0,api_retry_after=NULL,last_api_error='',
                         updated_at=? WHERE id=?""",
                    (stamp, row["arm_id"]),
                )
                conn.execute(
                    """UPDATE delivery_submissions SET status='not_submitted',error='',
                         hidden_at=NULL,updated_at=? WHERE pair_id=?""",
                    (stamp, row["id"]),
                )
                if row.get("chain_id"):
                    conn.execute(
                        """UPDATE project_chains SET status='active',followup_completed=0,
                             completed_at=NULL,updated_at=? WHERE id=?""",
                        (stamp, row["chain_id"]),
                    )
        self.db.audit("claude.single_arm_repair_resumed", "arm_run", row["arm_id"], {
            "pair_id": row["id"], "arm": row["arm"], "attempt": 1,
            "preserved_peer": True, "source": "common_baseline",
            "pair_failure_count": int(row.get("development_failure_count") or 0),
            "maximum": maximum,
        })
        self._submit_monitor(
            "retry-recover-" + row["arm_id"], self._recover_pending_retry,
            row["id"], row["arm_id"], str(row.get("prompt") or ""),
        )
        return True

    def _resume_one_manual_arm_requeue(self, pair_id: str = "") -> bool:
        """Start operator-selected Arms when one Pair slot is available."""
        if self.db.setting("pipeline_drain", False):
            return False
        with self._pair_creation_lock:
            params: Tuple[Any, ...] = ()
            pair_filter = ""
            if pair_id:
                pair_filter = " AND p.id=?"
                params = (pair_id,)
            row = self.db.one(
                """SELECT p.id,p.chain_id,t.prompt FROM pairs p
                     JOIN tasks t ON t.id=p.task_id
                    WHERE p.status='repair_pending'
                      AND p.stage='manual_arm_requeue_pending'
                      AND EXISTS(SELECT 1 FROM arm_runs a WHERE a.pair_id=p.id
                                   AND a.status='manual_waiting')%s
                    ORDER BY p.updated_at,p.created_at LIMIT 1""" % pair_filter,
                params,
            )
            if not row:
                return False
            active_count = int((self.db.one(
                "SELECT COUNT(*) count FROM development_pairs WHERE status IN ('queued','running','review')"
            ) or {"count": 0})["count"])
            configured_limit = int(self.db.setting(
                "max_pairs_parallel", self.config.max_pairs_parallel,
            ))
            pair_limit = max(1, min(MAX_PAIR_PROJECTS, configured_limit))
            if active_count >= pair_limit:
                # A prepared Pair with neither prompt sent is only holding a
                # reservation. Move that reservation behind the already-done
                # Arm's peer without touching either development session.
                if not self._defer_unstarted_pair_for_manual_requeue_locked(row["id"]):
                    return False
            queued = self.db.all(
                "SELECT id,arm FROM arm_runs WHERE pair_id=? AND status='manual_waiting' ORDER BY arm",
                (row["id"],),
            )
            if not queued:
                return False
            stamp = now_iso()
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE pairs SET status='running',stage='development',winner='',error='',
                       completed_at=NULL,updated_at=? WHERE id=? AND status='repair_pending'
                       AND stage='manual_arm_requeue_pending'""",
                    (stamp, row["id"]),
                ).rowcount
                if not changed:
                    return False
                conn.execute(
                    """UPDATE arm_runs SET status='queued',updated_at=? WHERE pair_id=?
                       AND status='manual_waiting'""",
                    (stamp, row["id"]),
                )
                conn.execute(
                    """UPDATE delivery_submissions SET status='needs_review',error='',
                       hidden_at=NULL,payload_sha256='',updated_at=? WHERE pair_id=?""",
                    (stamp, row["id"]),
                )
                if row.get("chain_id"):
                    conn.execute(
                        """UPDATE project_chains SET status='active',followup_completed=0,
                           completed_at=NULL,updated_at=? WHERE id=?""",
                        (stamp, row["chain_id"]),
                    )
        for arm in queued:
            self._submit_monitor(
                "retry-recover-" + arm["id"], self._recover_pending_retry,
                row["id"], arm["id"], str(row.get("prompt") or ""),
            )
        queued_event = self.db.one(
            """SELECT detail_json FROM audit_events
               WHERE event_type='claude.manual_arm_requeue_queued' AND entity_id=?
               ORDER BY id DESC LIMIT 1""",
            (queued[0]["id"],),
        ) or {}
        try:
            failure_budget_reset = bool(json.loads(queued_event.get("detail_json") or "{}").get(
                "failureBudgetReset", True,
            ))
        except (TypeError, ValueError):
            failure_budget_reset = True
        self.db.audit("claude.manual_arm_requeue_resumed", "pair", row["id"], {
            "arms": [arm["arm"] for arm in queued],
            "failureBudgetReset": failure_budget_reset,
            "boundedByPairAndTerminalLimits": True,
        })
        return True

    def _defer_unstarted_pair_for_manual_requeue_locked(self, priority_pair_id: str) -> bool:
        """Yield one never-started Pair slot to a selected single-Arm retry."""
        candidates = self.db.all(
            """SELECT p.id FROM pairs p
                WHERE p.status='queued' AND p.stage IN ('repository','ready_to_start') AND p.id<>?
                  AND ((p.stage='repository' AND NOT EXISTS (
                           SELECT 1 FROM arm_runs a WHERE a.pair_id=p.id))
                    OR (p.stage='ready_to_start'
                        AND (SELECT COUNT(*) FROM arm_runs a WHERE a.pair_id=p.id)=2
                        AND NOT EXISTS (SELECT 1 FROM arm_runs a WHERE a.pair_id=p.id
                            AND (a.status<>'queued' OR a.prompt_sent_at IS NOT NULL
                                 OR a.session_id<>'' OR a.commit_sha<>''))))
                ORDER BY p.created_at DESC,p.id DESC""",
            (priority_pair_id,),
        )
        for candidate in candidates:
            deferred_id = str(candidate["id"])
            with self._future_lock:
                start_future = self._futures.get("start-" + deferred_id)
                repo_future = self._futures.get("repo-" + deferred_id)
                if ((start_future and not start_future.done())
                        or (repo_future and not repo_future.done())):
                    continue
            with self._start_locks_lock:
                start_lock = self._start_locks.setdefault(deferred_id, threading.Lock())
            if not start_lock.acquire(blocking=False):
                continue
            try:
                with self.db.transaction() as conn:
                    changed = conn.execute(
                        """UPDATE pairs SET status='deferred_priority',updated_at=?
                             WHERE id=? AND status='queued'
                               AND ((stage='repository' AND NOT EXISTS (
                                      SELECT 1 FROM arm_runs WHERE pair_id=?))
                                 OR (stage='ready_to_start'
                                     AND (SELECT COUNT(*) FROM arm_runs WHERE pair_id=?)=2
                                     AND NOT EXISTS (SELECT 1 FROM arm_runs a WHERE a.pair_id=?
                                         AND (a.status<>'queued' OR a.prompt_sent_at IS NOT NULL
                                              OR a.session_id<>'' OR a.commit_sha<>''))))""",
                        (now_iso(), deferred_id, deferred_id, deferred_id, deferred_id),
                    ).rowcount
                if not changed:
                    continue
            finally:
                start_lock.release()
            self.db.audit("pair.unstarted_deferred_for_manual_arm", "pair", deferred_id, {
                "priorityPairId": priority_pair_id,
                "preservedExistingState": True,
            })
            return True
        return False

    def _resume_one_deferred_prepared_pair(self) -> bool:
        """Reactivate a preserved unstarted Pair before consuming another task."""
        with self._pair_creation_lock:
            active_count = int((self.db.one(
                "SELECT COUNT(*) count FROM development_pairs WHERE status IN ('queued','running','review')"
            ) or {"count": 0})["count"])
            configured = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
            if active_count >= max(1, min(MAX_PAIR_PROJECTS, configured)):
                return False
            row = self.db.one(
                """SELECT id FROM pairs WHERE status='deferred_priority'
                     AND stage IN ('repository','ready_to_start')
                     ORDER BY updated_at,created_at,id LIMIT 1"""
            )
            if not row:
                return False
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE pairs SET status='queued',updated_at=?
                         WHERE id=? AND status='deferred_priority'
                           AND stage IN ('repository','ready_to_start')""",
                    (now_iso(), row["id"]),
                ).rowcount
            if not changed:
                return False
        self.db.audit("pair.unstarted_resumed", "pair", row["id"], {
            "preservedExistingState": True,
        })
        return True

    def _resume_one_lineage_repair(self) -> bool:
        """Use the next free Pair slot for a previously false-completed delivery."""
        with self._pair_creation_lock:
            row = self.db.one(
                """SELECT id FROM pairs WHERE status='repair_pending'
                     AND stage='lineage_repair_pending' ORDER BY updated_at,created_at LIMIT 1"""
            )
            if not row:
                return False
            stamp = now_iso()
            self.db.execute(
                """UPDATE pairs SET status='running',updated_at=? WHERE id=?
                     AND status='repair_pending' AND stage='lineage_repair_pending'""",
                (stamp, row["id"]),
            )
            self._submit_auto(
                "lineage-repair-" + row["id"], self._run_lineage_repair, row["id"],
            )
            return True

    def _resume_one_recording_revalidation_pair(self) -> bool:
        """Resume recording after later artifact evidence invalidated an old GSB.

        The delivered commits and artifact checks are preserved; only the
        missing recordings and the comparison generated from the new evidence
        are rebuilt. Recording is single-flight, but it does not wait for a
        Claude development Pair slot.
        """
        with self._pair_creation_lock:
            row = self.db.one(
                """SELECT p.id,p.chain_id FROM pairs p
                    WHERE p.status='repair_pending'
                      AND p.stage='recording_revalidation_pending'
                      AND EXISTS(
                        SELECT 1 FROM arm_runs a JOIN artifact_checks c
                          ON c.pair_id=a.pair_id AND c.arm=a.arm
                         AND c.commit_sha=a.commit_sha
                         WHERE a.pair_id=p.id AND a.status='completed'
                           AND a.commit_sha<>'' AND c.status='passed'
                      )
                    ORDER BY p.updated_at,p.created_at LIMIT 1"""
            )
            if not row:
                return False
            stamp = now_iso()
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE pairs SET status='running',stage='recording',winner='',error='',
                         completed_at=NULL,updated_at=? WHERE id=? AND status='repair_pending'
                         AND stage='recording_revalidation_pending'""",
                    (stamp, row["id"]),
                ).rowcount
                if not changed:
                    return False
                conn.execute(
                    """UPDATE delivery_submissions SET status='needs_review',error='',
                         hidden_at=NULL,updated_at=? WHERE pair_id=?""",
                    (stamp, row["id"]),
                )
                if row.get("chain_id"):
                    conn.execute(
                        """UPDATE project_chains SET status='active',followup_completed=0,
                             completed_at=NULL,updated_at=? WHERE id=?""",
                        (stamp, row["chain_id"]),
                    )
        self.db.audit("artifact.revalidation_recording_resumed", "pair", row["id"], {
            "preserved": ["A_commit", "B_commit", "artifact_checks", "native_traces"],
            "nextStage": "recording",
        })
        return True

    def _resume_one_manual_full_pair_retry(self) -> bool:
        """Start an approved or prompt-integrity clean A/B rerun at a free slot.

        Both paths reset the Arms to the common baseline, use the exact
        database prompt in fresh sessions, and start with a fresh shared
        failure budget. Prompt-integrity reruns are infrastructure correction,
        not an automatic third development attempt.
        """
        with self._pair_creation_lock:
            active_count = int((self.db.one(
                "SELECT COUNT(*) count FROM development_pairs WHERE status IN ('queued','running','review')"
            ) or {"count": 0})["count"])
            configured_limit = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
            pair_limit = max(1, min(MAX_PAIR_PROJECTS, configured_limit))
            if active_count >= pair_limit:
                return False
            row = self.db.one(
                """SELECT p.id,p.chain_id,p.stage,r.local_root FROM pairs p
                     JOIN git_repositories r ON r.pair_id=p.id AND r.status='ready'
                    WHERE p.status='repair_pending'
                      AND p.stage IN ('manual_full_retry_pending','prompt_mismatch_retry_pending')
                      AND (SELECT COUNT(*) FROM arm_runs a WHERE a.pair_id=p.id)=2
                    ORDER BY p.updated_at,p.created_at LIMIT 1"""
            )
            if not row:
                return False
            stamp = now_iso()
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE pairs SET status='queued',stage='ready_to_start',winner='',error='',
                         development_failure_count=0,started_at=NULL,completed_at=NULL,updated_at=?
                         WHERE id=? AND status='repair_pending'
                           AND stage=?""",
                    (stamp, row["id"], row["stage"]),
                ).rowcount
                if not changed:
                    return False
                conn.execute(
                    """UPDATE arm_runs SET status='queued',workspace_path=CASE arm
                           WHEN 'A' THEN ? ELSE ? END,image_id='',session_id='',prompt_id='',
                         trace_path='',commit_sha='',exit_code=NULL,result='',warning_at=NULL,
                         prompt_sent_at=NULL,finished_at=NULL,attempt_no=1,error_retry_count=0,
                         api_retry_count=0,api_retry_after=NULL,last_api_error='',error='',updated_at=?
                         WHERE pair_id=?""",
                    (str(Path(row["local_root"]) / "workspaces" / "A"),
                     str(Path(row["local_root"]) / "workspaces" / "B"), stamp, row["id"]),
                )
                conn.execute("DELETE FROM recordings WHERE pair_id=?", (row["id"],))
                conn.execute("DELETE FROM artifact_checks WHERE pair_id=?", (row["id"],))
                conn.execute(
                    """UPDATE delivery_submissions SET status='not_submitted',error='',
                         hidden_at=NULL,updated_at=? WHERE pair_id=?""",
                    (stamp, row["id"]),
                )
                if row.get("chain_id"):
                    conn.execute(
                        """UPDATE project_chains SET status='active',followup_completed=0,
                             completed_at=NULL,updated_at=? WHERE id=?""",
                        (stamp, row["chain_id"]),
                    )
        event_type = (
            "claude.prompt_mismatch_retry_resumed"
            if row.get("stage") == "prompt_mismatch_retry_pending"
            else "pair.manual_full_retry_resumed"
        )
        self.db.audit(event_type, "pair", row["id"], {
            "source": "common_baseline", "freshSessions": ["A", "B"],
            "failureBudgetReset": True,
            "reason": (
                "complete_first_user_prompt_mismatch"
                if row.get("stage") == "prompt_mismatch_retry_pending"
                else "operator_approved_full_retry"
            ),
        })
        self._submit_auto("start-" + row["id"], self.start_pair, row["id"])
        return True

    def _resume_one_environment_failed_pair(self) -> bool:
        """Retry a prepared Pair after a temporary Docker daemon outage."""
        candidate = self.db.one(
            """SELECT p.id FROM pairs p
                 JOIN tasks t ON t.id=p.task_id
                 JOIN git_repositories r ON r.pair_id=p.id
                WHERE p.status='failed' AND p.stage='ready_to_start'
                  AND t.status='used' AND t.locked_by=p.id AND r.status='ready'
                  AND (LOWER(p.error) LIKE '%cannot connect to the docker daemon%'
                    OR LOWER(p.error) LIKE '%docker desktop is not running%'
                    OR LOWER(p.error) LIKE '%is the docker daemon running%'
                    OR p.error LIKE '%基线预检依赖重建端口冲突%')
                  AND NOT EXISTS (
                    SELECT 1 FROM pairs newer
                     WHERE newer.task_id=p.task_id AND newer.id<>p.id
                       AND newer.status IN ('queued','running','review','completed','repair_pending')
                  )
                ORDER BY p.updated_at DESC,p.created_at DESC LIMIT 1"""
        )
        if not candidate:
            return False
        docker = run_command(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=False, timeout=5,
        )
        if docker.returncode != 0:
            return False
        with self._pair_creation_lock:
            active_count = int((self.db.one(
                "SELECT COUNT(*) count FROM development_pairs WHERE status IN ('queued','running','review')"
            ) or {"count": 0})["count"])
            configured_limit = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
            pair_limit = max(1, min(MAX_PAIR_PROJECTS, configured_limit))
            if active_count >= pair_limit:
                return False
            stamp = now_iso()
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE pairs SET status='queued',error='',updated_at=?
                         WHERE id=? AND status='failed' AND stage='ready_to_start'""",
                    (stamp, candidate["id"]),
                ).rowcount
            if not changed:
                return False
        self.db.audit("pair.environment_start_resumed", "pair", candidate["id"], {
            "reason": "docker_daemon_available_again",
        })
        self._submit_auto("start-" + candidate["id"], self.start_pair, candidate["id"])
        return True

    def _resume_one_baseline_preflight_retry(self) -> bool:
        """Retry a false port-conflict preflight when a Pair slot is free."""
        candidate = self.db.one(
            """SELECT p.id FROM pairs p
                 JOIN tasks t ON t.id=p.task_id
                 JOIN git_repositories r ON r.pair_id=p.id
                WHERE p.status='repair_pending'
                  AND p.stage='baseline_preflight_retry_pending'
                  AND t.status='used' AND t.locked_by=p.id AND r.status='ready'
                ORDER BY p.updated_at,p.created_at LIMIT 1"""
        )
        if not candidate:
            return False
        with self._pair_creation_lock:
            active_count = int((self.db.one(
                "SELECT COUNT(*) count FROM development_pairs WHERE status IN ('queued','running','review')"
            ) or {"count": 0})["count"])
            configured_limit = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
            pair_limit = max(1, min(MAX_PAIR_PROJECTS, configured_limit))
            if active_count >= pair_limit:
                return False
            stamp = now_iso()
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE pairs SET status='queued',stage='ready_to_start',error='',updated_at=?
                         WHERE id=? AND status='repair_pending'
                           AND stage='baseline_preflight_retry_pending'""",
                    (stamp, candidate["id"]),
                ).rowcount
            if not changed:
                return False
        self.db.audit("task.baseline_preflight_retry_resumed", "pair", candidate["id"], {
            "reason": "dependency_recreation_port_conflict_fixed",
        })
        self._submit_auto("start-" + candidate["id"], self.start_pair, candidate["id"])
        return True

    def _run_lineage_repair(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        task = self.db.one("SELECT prompt FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        return self._restart_trace_invalid_arms(
            pair_id, arms, str(task.get("prompt") or ""),
            ["A/B 产物没有形成基于初始环境的有效代码提交"],
        )

    def _resume_one_reusable_pair_locked(self) -> bool:
        """Prefer finished code over consuming another task-pool entry.

        A Bug repair that was previously stopped only because its actual
        difficulty was medium becomes reusable under the current policy.  It
        resumes at recording without repeating development or Docker checks.
        An artifact failure does not erase either Git commit or trace, so it
        can start a targeted repair from the delivered commit.  A recording
        failure is deliberately excluded: after three failed automatic
        attempts it must remain stopped until an operator starts a manual
        re-recording.  Reopening it here would create endless three-attempt
        retry windows.
        """
        trace_rows = self.db.all(
            """SELECT p.id,p.task_id FROM pairs p
                 JOIN delivery_submissions d ON d.pair_id=p.id
                WHERE p.status='completed' AND d.status='needs_fix'
                ORDER BY d.updated_at,p.created_at LIMIT 10"""
        )
        for trace_row in trace_rows:
            runs = self.db.all(
                "SELECT * FROM arm_runs WHERE pair_id=? AND status='completed' ORDER BY arm",
                (trace_row["id"],),
            )
            if len(runs) != 2:
                continue
            task = self.db.one("SELECT prompt FROM tasks WHERE id=?", (trace_row["task_id"],)) or {}
            prompt = str(task.get("prompt") or "")
            first_prompts: Dict[str, str] = {}
            for run in runs:
                trace, _, _ = self._inspect_trace(run, prompt)
                if trace:
                    first_prompts[str(run["arm"])] = self._trace_first_user_prompt(trace)
            if (first_prompts.get("A") and first_prompts.get("B")
                    and not self._paired_trace_prompts_match(
                        prompt, first_prompts["A"], first_prompts["B"],
                    )):
                self._restart_trace_invalid_arms(
                    trace_row["id"], runs, prompt,
                    ["A/B 轨迹里的完整首轮 User Prompt 不一致"],
                )
                return True
        row = self.db.one(
            """SELECT p.id,p.chain_id,p.stage FROM pairs p
               WHERE p.status='failed' AND p.stage='artifact_failed'
                 AND (SELECT COUNT(*) FROM arm_runs a
                      WHERE a.pair_id=p.id AND a.status='completed' AND a.commit_sha<>'')=2
               ORDER BY p.updated_at,p.created_at LIMIT 1"""
        )
        if not row:
            return False
        stamp = now_iso()
        next_stage = "artifact_validation"
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE pairs SET status='running',stage=?,error='',
                   winner='',completed_at=NULL,updated_at=? WHERE id=?""",
                (next_stage, stamp, row["id"]),
            )
            conn.execute(
                """UPDATE delivery_submissions SET status='needs_review',error='',updated_at=?
                   WHERE pair_id=?""", (stamp, row["id"]),
            )
            if row.get("chain_id"):
                conn.execute(
                    """UPDATE project_chains SET status='active',followup_completed=0,
                       completed_at=NULL,updated_at=? WHERE id=?""",
                    (stamp, row["chain_id"]),
                )
        self.db.audit("artifact.revalidation_started", "pair", row["id"], {
            "reason": "reuse_existing_commits_before_targeted_repair",
            "preserved": ["A_commit", "B_commit", "traces"],
        })
        return True

    @staticmethod
    def _canonical_repository_url(value: Any) -> str:
        url = str(value or "").strip().casefold()
        ssh = re.fullmatch(r"git@([^:]+):(.+)", url)
        if ssh:
            url = "https://%s/%s" % (ssh.group(1), ssh.group(2))
        url = url.rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
        return url

    def _feature_project_key(self, task: Dict[str, Any]) -> str:
        repo = self._canonical_repository_url(task.get("baseline_repo_url"))
        parent_pair_id = str(task.get("parent_pair_id") or "").strip()
        if not repo and parent_pair_id:
            parent_repo = self.db.one(
                "SELECT remote_url FROM git_repositories WHERE pair_id=?", (parent_pair_id,),
            ) or {}
            repo = self._canonical_repository_url(parent_repo.get("remote_url"))
        if repo:
            return "repo:" + repo
        if parent_pair_id:
            return "pair:" + parent_pair_id
        title = " ".join(str(task.get("title") or "").casefold().split())
        return "title:" + title

    def _feature_project_rows(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        project = self._feature_project_key(task)
        rows = self.db.all(
            """SELECT id,title,baseline_repo_url,parent_pair_id,status,created_at
                 FROM tasks WHERE task_type='feature'
                  AND status IN ('candidate','ready','used','rejected')
                ORDER BY created_at,id"""
        )
        return [row for row in rows if self._feature_project_key(row) == project]

    def _feature_project_rank(self, task: Dict[str, Any]) -> int:
        # Rejected proposals are not delivered iterations and must not use up
        # the per-project quota. Keep a separate attempt cap for bad sources.
        qualified = [row for row in self._feature_project_rows(task)
                     if row["status"] != "rejected"]
        for index, row in enumerate(qualified, start=1):
            if row["id"] == task.get("id"):
                return index
        return 0

    def _feature_project_can_generate(self, task: Dict[str, Any]) -> bool:
        rows = self._feature_project_rows(task)
        qualified = sum(row["status"] != "rejected" for row in rows)
        return (qualified < MAX_FEATURE_TASKS_PER_PROJECT
                and len(rows) < MAX_FEATURE_GENERATION_ATTEMPTS_PER_PROJECT)

    def _eligible_feature_sources(self) -> List[Dict[str, Any]]:
        return self.db.all(
            """SELECT p.id,t.title,COALESCE(r.remote_url,'') baseline_repo_url
                 FROM pairs p JOIN tasks t ON t.id=p.task_id
            LEFT JOIN git_repositories r ON r.pair_id=p.id
               WHERE p.status='completed' AND t.task_type='zero_to_one'
                 AND NOT EXISTS (
                   SELECT 1 FROM audit_events e
                    WHERE e.event_type='feature.source_missing_verify'
                      AND e.entity_type='pair' AND e.entity_id=p.id
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM delivery_submissions d
                    WHERE d.pair_id=p.id AND d.status='discarded'
                 )
                 AND EXISTS (
                   SELECT 1 FROM arm_runs a
                   JOIN artifact_checks c
                     ON c.pair_id=a.pair_id AND c.arm=a.arm
                    AND c.commit_sha=a.commit_sha AND c.status='passed'
                  WHERE a.pair_id=p.id AND a.status='completed' AND a.commit_sha<>''
                    AND a.arm=CASE WHEN p.winner='B better' THEN 'B' ELSE 'A' END
                 )
               ORDER BY p.completed_at DESC,p.id DESC"""
        )

    def _retire_outdated_ready_bug_task(self, task: Dict[str, Any]) -> bool:
        if task.get("source") != "bug_discovery" or task.get("task_type") != "bugfix":
            return False
        issues = self._bugfix_prompt_issues(str(task.get("prompt") or ""))
        issues.extend(self._bug_prompt_template_issues(
            str(task.get("prompt") or ""), str(task.get("id") or ""),
        ))
        if not task_difficulty_allowed("bugfix", str(task.get("difficulty") or "")):
            issues.append("Bug 任务难度低于困难")
        if self._strict_bug_admission():
            issues.extend(self._manual_bug_candidate_issues(task))
        if not issues:
            return False
        stamp = now_iso()
        reason = "旧版 Bug 任务已停用，需按当前准入规则重新生成：" + "；".join(issues)
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE tasks SET status='rejected',rejection_reason=?,updated_at=? WHERE id=? AND status='ready'",
                (reason[-2000:], stamp, task["id"]),
            ).rowcount
            if not changed:
                return False
            conn.execute(
                """UPDATE bug_candidates SET status='reproduced',error='',updated_at=?
                     WHERE id=? AND status='converted' AND reproduce_count>=2""",
                (stamp, task.get("source_id") or ""),
            )
        self.db.audit("bug.outdated_prompt_retired", "task", task["id"], {
            "candidate_id": task.get("source_id") or "", "reason": reason,
        })
        return True

    def _retire_outdated_ready_bug_tasks(self) -> int:
        retired = 0
        for task in self.db.all(
            """SELECT * FROM tasks WHERE source='bug_discovery'
                 AND task_type='bugfix' AND status='ready'"""
        ):
            retired += int(self._retire_outdated_ready_bug_task(task))
        return retired

    def _restore_old_scale_rejected_bug_tasks(self) -> int:
        """Readmit candidates rejected only by the superseded compact envelope.

        The previous 2–3 module / 60–180 line / 40–80 minute policy was
        briefly persisted into task and candidate rejection text.  Once the
        configured envelope is migrated, those deterministic scale-only
        rejections must be reconsidered instead of forcing an already
        reproduced isolated baseline through discovery again.  Other reasons
        (notably browser-dependent reproduction) remain final.
        """
        old_scale_markers = (
            "2–3 个范围",
            "60–180 行范围",
            "40–80 分钟",
            "不得超过 90 分钟",
        )
        restored = 0
        rows = self.db.all(
            """SELECT t.*,c.status candidate_status,c.error candidate_error,
                      c.reproduce_count,c.reproduction_commands_json
                 FROM tasks t JOIN bug_candidates c ON c.id=t.source_id
                WHERE t.source='bug_discovery' AND t.task_type='bugfix'
                  AND t.status='rejected' AND c.status='difficulty_rejected'
                  AND c.reproduce_count>=2
                  AND NOT EXISTS (SELECT 1 FROM pairs p WHERE p.task_id=t.id)
                ORDER BY t.updated_at,t.id"""
        )
        for row in rows:
            rejection = str(row.get("rejection_reason") or "")
            candidate_error = str(row.get("candidate_error") or "")
            combined = rejection + "\n" + candidate_error
            if not any(marker in combined for marker in old_scale_markers):
                continue
            # Scale migration is intentionally narrow: if either persisted
            # reason names an unrelated defect, do not erase that rejection.
            remaining = combined
            for marker in old_scale_markers:
                remaining = remaining.replace(marker, "")
            remaining = re.sub(
                r"(?:旧版 Bug 任务已停用，需按当前准入规则重新生成：|"
                r"预计业务模块数不在\s*|预计有效源码改动不在\s*|"
                r"预计开发应以\s*|且含验证)",
                "", remaining,
            )
            if remaining.strip(" \n；，。："):
                continue
            candidate = dict(row)
            candidate["difficulty"] = row.get("difficulty")
            candidate["error"] = candidate_error
            if self._manual_bug_candidate_issues(candidate):
                continue
            stamp = now_iso()
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE tasks SET status='ready',rejection_reason='',locked_by='',
                         used_at=NULL,updated_at=? WHERE id=? AND status='rejected'""",
                    (stamp, row["id"]),
                ).rowcount
                if not changed:
                    continue
                conn.execute(
                    """UPDATE bug_candidates SET status='converted',error='',updated_at=?
                         WHERE id=? AND status='difficulty_rejected'""",
                    (stamp, row.get("source_id") or ""),
                )
            self._append_manual_bug_reservation(str(row["id"]))
            self.db.audit("bug.scale_rejection_restored", "task", row["id"], {
                "candidate_id": row.get("source_id") or "",
                "previous_rejection": rejection[-1000:],
                "current_envelope": {
                    "modules": [MANUAL_BUG_MIN_MODULES, MANUAL_BUG_MAX_MODULES],
                    "sourceLines": [MANUAL_BUG_MIN_SOURCE_LINES, MANUAL_BUG_MAX_SOURCE_LINES],
                    "minutes": [MANUAL_BUG_MIN_ESTIMATED_MINUTES, MANUAL_BUG_MAX_ESTIMATED_MINUTES],
                },
            })
            restored += 1
        return restored

    def _manual_bug_reserved_ids(self) -> List[str]:
        queue = self.db.setting("manual_priority_task_pause", {})
        if not isinstance(queue, dict) or not queue.get("active"):
            return []
        return [
            str(task_id) for task_id in (queue.get("reservedTaskIds") or [])
            if str(task_id)
        ]

    def _manual_bug_ready_count(self) -> int:
        """Count only unconsumed, allow-listed hard Bug tasks as reserve."""
        reserved = self._manual_bug_reserved_ids()
        if not reserved:
            return 0
        placeholders = ",".join("?" for _ in reserved)
        row = self.db.one(
            """SELECT COUNT(*) count FROM tasks t
                 WHERE t.id IN (%s) AND t.status='ready' AND t.task_type='bugfix'
                   AND t.difficulty IN ('困难','地狱')
                   AND t.estimated_module_count BETWEEN ? AND ?
                   AND t.estimated_source_lines_min>=?
                   AND t.estimated_source_lines_max<=?
                   AND t.estimated_source_lines_min<=t.estimated_source_lines_max
                   AND t.estimated_minutes_min>=?
                   AND t.estimated_minutes_min<=?
                   AND t.estimated_minutes_max<=?
                   AND t.estimated_minutes_min<=t.estimated_minutes_max
                   AND NOT EXISTS (SELECT 1 FROM pairs p WHERE p.task_id=t.id)""" % placeholders,
            (*reserved, MANUAL_BUG_MIN_MODULES, MANUAL_BUG_MAX_MODULES,
             MANUAL_BUG_MIN_SOURCE_LINES, MANUAL_BUG_MAX_SOURCE_LINES,
             MANUAL_BUG_MIN_ESTIMATED_MINUTES, MANUAL_BUG_TARGET_ESTIMATED_MINUTES,
             MANUAL_BUG_MAX_ESTIMATED_MINUTES),
        )
        return int((row or {"count": 0})["count"])

    def _append_manual_bug_reservation(self, task_id: str) -> None:
        with self.db.transaction() as conn:
            self._append_manual_bug_reservation_in_transaction(conn, task_id)

    def _append_manual_bug_reservation_in_transaction(self, conn, task_id: str) -> None:
        row = conn.execute("SELECT value_json FROM settings WHERE key='manual_priority_task_pause'").fetchone()
        queue = json.loads(row[0]) if row else {}
        if not isinstance(queue, dict):
            queue = {}
        reserved = [str(item) for item in (queue.get("reservedTaskIds") or []) if str(item)]
        if task_id not in reserved:
            reserved.append(task_id)
        queue.update({
            "active": True,
            "mode": "manual_bug_only",
            "reservedTaskIds": reserved,
            "readyTarget": int(self.db.setting("manual_bug_ready_target", 6)),
            "automaticReplenishment": True,
            "refillMode": "system_bug_pipeline",
            "updatedAt": now_iso(),
        })
        conn.execute(
            "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            ("manual_priority_task_pause", json.dumps(queue, ensure_ascii=False), now_iso()),
        )

    def _manual_bug_candidate_issues(self, candidate: Dict[str, Any]) -> List[str]:
        """Deterministic hard-Bug admission gate; patrol later audits the evidence."""
        issues: List[str] = []
        if str(candidate.get("difficulty") or "") not in ("困难", "地狱"):
            issues.append("Bug 候选只准入困难或地狱")
        modules = int(candidate.get("estimatedModuleCount") or candidate.get("estimated_module_count") or 0)
        if modules < MANUAL_BUG_MIN_MODULES or modules > MANUAL_BUG_MAX_MODULES:
            issues.append("预计业务模块数不在 2–4 个范围")
        lines_min = int(candidate.get("estimatedSourceLinesMin") or candidate.get("estimated_source_lines_min") or 0)
        lines_max = int(candidate.get("estimatedSourceLinesMax") or candidate.get("estimated_source_lines_max") or 0)
        if (lines_min < MANUAL_BUG_MIN_SOURCE_LINES
                or lines_max > MANUAL_BUG_MAX_SOURCE_LINES
                or lines_min > lines_max):
            issues.append("预计有效源码改动不在 80–250 行范围")
        minutes_min = int(candidate.get("estimatedMinutesMin") or candidate.get("estimated_minutes_min") or 0)
        minutes_max = int(candidate.get("estimatedMinutesMax") or candidate.get("estimated_minutes_max") or 0)
        if (minutes_min < MANUAL_BUG_MIN_ESTIMATED_MINUTES
                or minutes_min > MANUAL_BUG_TARGET_ESTIMATED_MINUTES
                or minutes_max > MANUAL_BUG_MAX_ESTIMATED_MINUTES
                or minutes_min > minutes_max):
            issues.append(f"预计主体开发应为 45–90 分钟，完整修复须包含本地测试及 Docker/verify 验证且不得超过 {MANUAL_BUG_MAX_ESTIMATED_MINUTES} 分钟")
        commands = candidate.get("reproductionCommands")
        if commands is None:
            try:
                commands = json.loads(str(candidate.get("reproduction_commands_json") or "[]"))
            except (TypeError, ValueError):
                commands = []
        if contains_browser_verification(commands or []):
            issues.append("复现依赖浏览器自动化")
        return issues

    @staticmethod
    def _bug_source_scan_path_ignored(relative_path: str) -> bool:
        parts = {
            part.casefold()
            for part in str(relative_path or "").replace("\\", "/").split("/")
            if part
        }
        return bool(parts.intersection(BUG_SOURCE_SCAN_IGNORED_PARTS))

    @staticmethod
    def _bug_source_browser_automation_files(workspace: Path) -> List[str]:
        """Return source files that make the Bug baseline browser-test dependent."""
        if not workspace.is_dir():
            return []
        markers = tuple(str(item).casefold() for item in TASK_PROMPT_BROWSER_VERIFY_MARKERS)
        # Only repository-owned source and validation configuration can make a
        # baseline browser-test dependent.  Local virtual environments and
        # tool caches may contain third-party metadata mentioning browser
        # packages, but they are not part of the product's verification path.
        matches: List[str] = []
        for path in workspace.rglob("*"):
            relative = str(path.relative_to(workspace))
            if not path.is_file() or PairwiseService._bug_source_scan_path_ignored(relative):
                continue
            if path.name.casefold() in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
                continue
            if path.suffix.casefold() in (".md", ".rst", ".txt"):
                continue
            try:
                if path.stat().st_size > 512_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore").casefold()
            except OSError:
                continue
            if any(marker in text for marker in markers):
                matches.append(relative)
                if len(matches) >= 8:
                    break
        return matches

    def _task_mix_policy(self) -> Dict[str, Any]:
        value = self.db.setting("task_mix_policy", {})
        return value if isinstance(value, dict) and value.get("enabled") else {}

    def _strict_bug_admission(self) -> bool:
        return bool(self.db.setting("manual_bug_only_mode", False) or self._task_mix_policy())

    def _task_mix_progress(self) -> Dict[str, Any]:
        policy = self._task_mix_policy()
        if not policy:
            return {}
        excluded = set(policy.get("excludedTaskIds") or [])
        reserved = set(self._manual_bug_reserved_ids())
        admitted = set(policy.get("qualifiedTaskIds") or [])
        target = int(policy.get("additionalBugTarget", 14))
        approved_credit = min(target, max(0, int(policy.get("approvedBugCredit", 0))))
        eligible_tasks = self.db.all(
            "SELECT * FROM tasks WHERE task_type='bugfix' AND status IN ('ready','used')"
        )
        qualified = sorted(task["id"] for task in eligible_tasks
                           if task["id"] in admitted and task["id"] not in excluded)
        # After the phase switch, retain the original admitted set unless one
        # of its tasks is rejected. Then admit only enough real replacements to
        # restore the quota; later mixed-phase Bugs do not rewrite this cohort.
        if policy.get("phase") == "bug_top_up" or len(qualified) + approved_credit < target:
            for task in sorted(eligible_tasks, key=lambda item: (item["created_at"], item["id"])):
                if task["id"] in admitted or task["id"] in excluded or task["id"] not in reserved:
                    continue
                if not self._manual_bug_candidate_issues(task):
                    qualified.append(task["id"])
                    if policy.get("phase") != "bug_top_up" and len(qualified) + approved_credit >= target:
                        break
        # An explicit policy amendment may shorten the remaining top-up quota.
        # Keep this credit separate from qualifiedTaskIds: it is never a task,
        # cannot enter the ready pool, and must not affect evidence audits.
        phase_progress = min(target, len(qualified) + approved_credit)
        return {"phase": policy.get("phase"), "target": target,
                "qualified": phase_progress, "verifiedQualified": len(qualified),
                "approvedBugCredit": approved_credit,
                "remaining": max(0, target - phase_progress),
                "qualifiedTaskIds": qualified,
                "weights": policy.get("weights", {"zero_to_one": 7, "feature": 7, "bugfix": 10})}

    def _task_mix_refill_deficit(self) -> bool:
        policy = self._task_mix_policy()
        return bool(policy.get("transitionedAt") and policy.get("phase") in (
            "zero_to_one_first", "balanced",
        ) and self._task_mix_progress()["remaining"])

    def _advance_task_mix_policy(self) -> None:
        policy = self._task_mix_policy()
        if not policy:
            return
        phase = policy.get("phase")
        progress = self._task_mix_progress()
        if phase == "bug_top_up" and progress["remaining"] == 0:
            # Persist the transition and its switches together. Restarts must
            # not reset the quota or replay the initial 0–1 handoff.
            with self.db.transaction() as conn:
                current = json.loads(conn.execute(
                    "SELECT value_json FROM settings WHERE key='task_mix_policy'"
                ).fetchone()[0])
                if current.get("phase") != "bug_top_up":
                    return
                current.update({"phase": "zero_to_one_first", "transitionedAt": now_iso(),
                                "qualifiedTaskIds": progress["qualifiedTaskIds"],
                                "excludedPairIds": [row[0] for row in conn.execute("SELECT id FROM pairs")]})
                for key, value in {
                    "task_mix_policy": current, "manual_bug_only_mode": False,
                    "auto_refill_enabled": True, "task_generation_zero_to_one_only": False,
                }.items():
                    conn.execute(
                        "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                        (key, json.dumps(value, ensure_ascii=False), now_iso()),
                    )
            self.db.audit("task.mix_transitioned", "scheduler", "task-mix", current)
        elif set(progress["qualifiedTaskIds"]) != set(policy.get("qualifiedTaskIds") or []):
            policy["qualifiedTaskIds"] = progress["qualifiedTaskIds"]
            self.db.set_setting("task_mix_policy", policy)
        elif phase == "zero_to_one_first" and self._task_mix_counts()["zero_to_one"]:
            policy["phase"] = "balanced"
            self.db.set_setting("task_mix_policy", policy)
            self.db.audit("task.mix_balancing_started", "scheduler", "task-mix", {
                "weights": policy.get("weights"), "historicalCountsExcluded": True,
            })

    def _task_mix_counts(self) -> Dict[str, int]:
        excluded = set(self._task_mix_policy().get("excludedPairIds") or [])
        counts = {kind: 0 for kind in ("zero_to_one", "feature", "bugfix")}
        for row in self.db.all("SELECT p.id,t.task_type FROM pairs p JOIN tasks t ON t.id=p.task_id"):
            if row["id"] not in excluded:
                counts[row["task_type"]] += 1
        return counts

    def _task_mix_type_order(self, include_ready: bool = False) -> List[str]:
        policy = self._task_mix_policy()
        if policy.get("phase") == "bug_top_up" or self._task_mix_refill_deficit():
            return ["bugfix"]
        if policy.get("phase") == "zero_to_one_first":
            return ["zero_to_one"]
        counts = self._task_mix_counts()
        if include_ready:
            for task in self.db.all("SELECT * FROM tasks WHERE status='ready'"):
                if not self._task_mix_task_issues(task):
                    counts[task["task_type"]] += 1
        weights = policy.get("weights", {"zero_to_one": 7, "feature": 7, "bugfix": 10})
        total = sum(counts.values()) + 1
        weight_total = sum(weights.values())
        return sorted(counts, key=lambda kind: -(weights[kind] * total - counts[kind] * weight_total))

    def _task_mix_task_issues(self, task: Dict[str, Any]) -> List[str]:
        policy = self._task_mix_policy()
        if not policy:
            return []
        kind = task.get("task_type")
        if self._task_mix_refill_deficit() and kind != "bugfix":
            return ["已计入的 Bug 后续失效，须补足真实合格 Bug 后再调度新题"]
        if (policy.get("phase") == "zero_to_one_first" and kind != "zero_to_one"
                and not self._task_mix_refill_deficit()):
            return ["本轮 Bug 补题已达标，下一道新 Pair 优先启动 0–1"]
        if kind == "bugfix":
            issues = self._manual_bug_candidate_issues(task)
            if task.get("id") not in self._manual_bug_reserved_ids():
                issues.append("Bug 尚未加入准入清单")
            return issues
        if policy.get("phase") == "bug_top_up":
            return ["本轮 Bug 补题数量尚未达标"]
        if task.get("id") in set(policy.get("excludedTaskIds") or []):
            return ["恢复后只使用按新规则生成的 0–1 和迭代题"]
        issues = generated_task_prompt_issues(str(kind), str(task.get("prompt") or ""))
        issues.extend(generated_task_estimate_issues(task))
        if task.get("source") in ("generated", "generated_followup"):
            try:
                work_items = json.loads(str(task.get("estimate_work_items_json") or "[]"))
                reviewed = summarize_reviewed_estimate(
                    work_items,
                    int(task.get("estimated_minutes_min") or 0),
                    int(task.get("estimated_minutes_max") or 0),
                )
                if reviewed["max"] > GENERATED_TASK_MAX_ESTIMATED_MINUTES:
                    issues.append("独立估时的开发、测试及 Docker/verify 完整总工时超过 180 分钟")
            except (TypeError, ValueError) as exc:
                issues.append("新题缺少可核对的完整独立估时：" + str(exc))
        if not 0 < int(task.get("estimated_minutes_max") or 0) <= GENERATED_TASK_MAX_ESTIMATED_MINUTES:
            issues.append("新题必须有包含理解、编码、测试和 Docker/verify 验证的完整有效估时")
        if task.get("difficulty") not in ("困难", "地狱"):
            issues.append("新题只准入困难或地狱")
        return issues

    def _schedule_mixed_refill_once(self) -> None:
        if self.db.setting("pipeline_drain", False) or not self.db.setting("auto_refill_enabled", True):
            return
        with self._future_lock:
            active = [key for key, future in self._futures.items()
                      if key.startswith(("generate-", "feature-", "bugs-", "bug-", "validate-"))
                      and not future.done()]
        # One slow source must not prevent the other types from preparing a
        # qualified question. Keep this below the configured worker ceiling so
        # generation does not crowd out active development or post-processing.
        slots = min(2, max(1, int(self.db.setting("task_generation_max_parallel", 2)))) - len(active)
        if slots <= 0:
            return
        ready = [task for task in self.db.all("SELECT * FROM tasks WHERE status='ready'")
                 if not self._task_mix_task_issues(task)]
        first = self._task_mix_policy().get("phase") == "zero_to_one_first"
        if first and any(task["task_type"] == "zero_to_one" for task in ready):
            return
        order = self._task_mix_type_order(include_ready=True)
        feature_target = max(0, min(20, int(self.db.setting("feature_ready_target", 0) or 0)))
        feature_ready = sum(task["task_type"] == "feature" for task in ready)
        if not first and feature_ready < feature_target:
            # A full mixed pool can still contain too few iteration tasks.
            # Keep this reserve separate from the overall pool size and the
            # 7:7:10 task-type weights; it affects preparation, not admission.
            order = ["feature"] + [kind for kind in order if kind != "feature"]
        if not first and len(ready) >= int(self.db.setting("task_pool_target_ready", 6)):
            # A full pool of the wrong type cannot satisfy the next ratio slot.
            # Prepare a missing type or an understocked feature reserve.
            order = [kind for kind in order
                     if ((kind == "feature" and feature_ready < feature_target)
                         or not any(task["task_type"] == kind for task in ready))]
        for kind in order:
            if slots <= 0:
                break
            if any(key.startswith(("generate-",) if kind == "zero_to_one" else
                                  ("feature-",) if kind == "feature" else
                                  ("bugs-", "bug-")) for key in active):
                continue
            if self._schedule_task_source(kind):
                slots -= 1

    def _next_ready_task(self, task_type: str = "") -> Optional[Dict[str, Any]]:
        policy = self._task_mix_policy()
        if policy and policy.get("phase") != "bug_top_up" and not task_type:
            order = self._task_mix_type_order()
            if policy.get("phase") == "balanced":
                # Use the deficit order when several qualified types are ready.
                # Never wait for a missing type: take the first available one.
                available = [(position, task) for position, kind in enumerate(order)
                             if (task := self._next_ready_task(kind))]
                return available[0][1] if available else None
            for kind in order:
                task = self._next_ready_task(kind)
                if task:
                    return task
            return None
        manual_bug_only = bool(self.db.setting("manual_bug_only_mode", False))
        selected_type = "bugfix" if manual_bug_only else str(task_type or "")
        if not selected_type and bool(self.db.setting("task_generation_zero_to_one_only", True)):
            selected_type = "zero_to_one"
        type_clause = " AND task_type=?" if selected_type else ""
        difficulty_clause = (
            " AND difficulty IN ('困难','地狱')"
        )
        params: Tuple[Any, ...] = (selected_type,) if selected_type else ()
        tasks = self.db.all(
            "SELECT * FROM tasks WHERE status='ready'"
            + difficulty_clause
            + type_clause
            + " ORDER BY CASE WHEN source='legacy' THEN 1 ELSE 0 END,created_at,id LIMIT 150",
            params,
        )
        if selected_type == "zero_to_one":
            preferred = set(self._preferred_zero_to_one_categories())
            if preferred:
                tasks.sort(key=lambda task: 0 if task.get("project_category") in preferred else 1)
        manual_pause = self.db.setting("manual_priority_task_pause", {})
        if isinstance(manual_pause, dict) and manual_pause.get("active"):
            reserved = [
                str(task_id) for task_id in (manual_pause.get("reservedTaskIds") or [])
                if str(task_id)
            ]
            positions = {task_id: index for index, task_id in enumerate(reserved)}
            # A manual priority run is an allow-list as well as an ordering.
            # Without this filter the normal created_at/id tie-break can skip
            # an earlier reserved task or refill from an unrelated ready task.
            tasks = [task for task in tasks if str(task.get("id") or "") in positions
                     or (policy and task.get("task_type") != "bugfix")]
            tasks.sort(key=lambda task: positions.get(str(task.get("id") or ""), len(positions)))
        for task in tasks:
            task_type = str(task.get("task_type") or "")
            if self._task_mix_task_issues(task):
                continue
            if self._retire_outdated_ready_bug_task(task):
                continue
            if task_type == "feature" and self._feature_project_rank(task) > MAX_FEATURE_TASKS_PER_PROJECT:
                reason = "同一基线项目最多保留 3 个 Feature 迭代，超出额度后应重新创建 0–1 项目"
                self.db.execute(
                    "UPDATE tasks SET status='rejected',rejection_reason=?,updated_at=? WHERE id=?",
                    (reason, now_iso(), task["id"]),
                )
                self.db.audit("feature.project_limit_rejected", "task", task["id"], {
                    "reason": reason, "project": self._feature_project_key(task),
                })
                continue
            duplicate = self._deterministic_task_duplicate(
                task, exclude_task_id=task["id"], selection=True,
            )
            if not duplicate:
                return task
            self.db.execute(
                "UPDATE tasks SET status='rejected',rejection_reason=?,updated_at=? WHERE id=?",
                (duplicate, now_iso(), task["id"]),
            )
            self.db.audit("task.selection_duplicate_rejected", "task", task["id"], {
                "reason": duplicate, "task_type": task_type,
            })
        return None

    def _schedule_any_task_source(self) -> bool:
        """Prepare an existing real task source before creating a new 0-1 task."""
        if bool(self.db.setting("manual_bug_only_mode", False)):
            return self._schedule_task_source("bugfix")
        if bool(self.db.setting("task_generation_zero_to_one_only", True)):
            return self._schedule_task_source("zero_to_one")
        pending_bug = self.db.one(
            """SELECT id FROM bug_candidates
               WHERE status IN ('reproduced','awaiting_reproduction')
               ORDER BY CASE status WHEN 'reproduced' THEN 0 ELSE 1 END,updated_at,id LIMIT 1"""
        )
        if pending_bug:
            return self._schedule_task_source("bugfix")
        feature_sources = self._eligible_feature_sources()
        for source in feature_sources:
            can_generate = self._feature_project_can_generate({
                "baseline_repo_url": source.get("baseline_repo_url", ""),
                "parent_pair_id": source["id"],
                "title": source.get("title", ""),
            })
            if can_generate:
                return self._schedule_task_source("feature")
        exhausted = self._bug_discovery_exhausted_sql("e", "a.commit_sha")
        bug_source = self.db.one(
            """SELECT p.id FROM pairs p
               WHERE p.status='completed'
                 AND NOT EXISTS (
                   SELECT 1 FROM delivery_submissions d
                    WHERE d.pair_id=p.id AND d.status='discarded'
                 )
                 AND EXISTS (
                   SELECT 1 FROM arm_runs a JOIN artifact_checks c
                     ON c.pair_id=a.pair_id AND c.arm=a.arm
                    AND c.commit_sha=a.commit_sha AND c.status='passed'
                    WHERE a.pair_id=p.id AND a.status='completed' AND a.commit_sha<>''
                      AND NOT EXISTS (
                        SELECT 1 FROM audit_events e
                         WHERE e.event_type='bug.discovery_completed'
                           AND e.entity_type='pair' AND e.entity_id=p.id
                           AND json_extract(e.detail_json,'$.arm')=a.arm
                           AND %s
                      )
                 )
               ORDER BY p.completed_at,p.id LIMIT 1""" % exhausted
        )
        if bug_source:
            return self._schedule_task_source("bugfix")
        return self._schedule_task_source("zero_to_one")

    def _schedule_task_source(self, task_type: str) -> bool:
        policy = self._task_mix_policy()
        if self._task_mix_refill_deficit() and task_type != "bugfix":
            return False
        if (policy.get("phase") == "zero_to_one_first" and task_type != "zero_to_one"
                and not self._task_mix_refill_deficit()):
            return False
        manual_bug_only = bool(self.db.setting("manual_bug_only_mode", False))
        if manual_bug_only and (
            task_type != "bugfix"
            or not bool(self.db.setting("manual_bug_auto_refill_enabled", False))
        ):
            return False
        if task_type == "zero_to_one":
            return self._submit_auto(
                "generate-zero-to-one", self.generate_tasks, 1, "zero_to_one",
            )
        if task_type == "feature":
            sources = self._eligible_feature_sources()
            for source in sources if policy else sources[:1]:
                if policy and self.db.one(
                    "SELECT 1 FROM tasks WHERE parent_pair_id=? AND task_type='feature' "
                    "AND status IN ('candidate','ready') LIMIT 1",
                    (source["id"],),
                ):
                    continue
                can_generate = self._feature_project_can_generate({
                    "baseline_repo_url": source.get("baseline_repo_url", ""),
                    "parent_pair_id": source["id"],
                    "title": source.get("title", ""),
                })
                if can_generate and self._submit_auto(
                    "feature-" + source["id"], self.generate_followup_feature, source["id"],
                ):
                    return True
            return (False if policy or self._task_mix_refill_deficit()
                    else self._schedule_task_source("zero_to_one"))
        if task_type == "bugfix":
            candidates = self.db.all(
                """SELECT id FROM bug_candidates WHERE status='reproduced'
                   AND difficulty IN ('困难','地狱')
                   ORDER BY updated_at,id"""
            )
            candidate = next((item for item in candidates if operation_ready(
                self.db, "bug-convert-" + item["id"])), None)
            if candidate:
                return self._submit_auto(
                    "bug-convert-" + candidate["id"], self.convert_bug_to_task, candidate["id"],
                )
            candidates = self.db.all(
                """SELECT id FROM bug_candidates WHERE status IN ('awaiting_reproduction','reproduction_failed')
                   AND difficulty IN ('困难','地狱')
                   ORDER BY created_at,id"""
            )
            candidate = next((item for item in candidates if operation_ready(
                self.db, "bug-reproduce-" + item["id"])), None)
            if candidate:
                return self._submit_auto(
                    "bug-reproduce-" + candidate["id"], self.reproduce_bug, candidate["id"],
                )
            if self._schedule_priority_bug_sources():
                return True
            exhausted = self._bug_discovery_exhausted_sql("e", "a.commit_sha")
            repaired_exhausted = self._bug_discovery_exhausted_sql(
                "e", "json_extract(r.detail_json,'$.baselineSha')", allow_legacy=False,
            )
            sources = self.db.all(
                """SELECT p.id FROM pairs p
                   WHERE p.status NOT IN ('queued','running','review','waiting_api_retry')
                     AND NOT EXISTS (
                       SELECT 1 FROM delivery_submissions d
                        WHERE d.pair_id=p.id AND d.status='discarded'
                     )
                     AND (EXISTS (
                       SELECT 1 FROM arm_runs a JOIN artifact_checks c
                         ON c.pair_id=a.pair_id AND c.arm=a.arm
                        AND c.commit_sha=a.commit_sha AND c.status='passed'
                        WHERE a.pair_id=p.id AND a.status='completed' AND a.commit_sha<>''
                          AND NOT EXISTS (
                            SELECT 1 FROM audit_events e
                             WHERE e.event_type='bug.discovery_completed'
                               AND e.entity_type='pair' AND e.entity_id=p.id
                               AND json_extract(e.detail_json,'$.arm')=a.arm
                               AND %s
                          )
                     ) OR EXISTS (
                       SELECT 1 FROM audit_events r
                        WHERE r.event_type='bug.source_repair_completed'
                          AND r.entity_type='pair' AND r.entity_id=p.id
                          AND NOT EXISTS (
                            SELECT 1 FROM audit_events e
                             WHERE e.event_type='bug.discovery_completed'
                               AND e.entity_type='pair' AND e.entity_id=p.id
                               AND json_extract(e.detail_json,'$.arm')=
                                   json_extract(r.detail_json,'$.sourceArm')
                               AND %s
                          )
                     ))
                   ORDER BY p.completed_at,p.id""" % (exhausted, repaired_exhausted)
            )
            source = next((item for item in sources if operation_ready(
                self.db, "bugs-" + item["id"])), None)
            if source:
                return self._submit_auto(
                    "bugs-" + source["id"], self.discover_bugs, source["id"],
                )
            if self._strict_bug_admission() and bool(self.db.setting("manual_bug_allow_failed_source_repair", True)):
                repair_sources = self._failed_bug_sources()
                repairable = next((item for item in repair_sources if operation_ready(
                    self.db, "bug-source-repair-" + item["pair_id"])), None)
                if repairable:
                    return self._submit_auto(
                        "bug-source-repair-" + repairable["pair_id"],
                        self.repair_bug_source, repairable["pair_id"],
                        repairable["arm"], repairable["commit_sha"],
                    )
            return (False if policy or self._task_mix_refill_deficit()
                    else self._schedule_task_source("zero_to_one"))
        raise ValueError("未知任务类型：" + task_type)

    def _schedule_available_task_sources(self) -> bool:
        """Start every currently usable source without enforcing a type quota.

        Idempotent operation keys keep repeated scheduler ticks from duplicating
        work. Whichever source produces a qualified task first can fill the next
        Pair slot.
        """
        scheduled = False
        for task_type in TASK_SOURCE_TYPES:
            scheduled = self._schedule_task_source(task_type) or scheduled
        return scheduled

    def _schedule_refill_once(self) -> None:
        self._advance_task_mix_policy()
        policy = self._task_mix_policy()
        if self._task_mix_refill_deficit():
            self._schedule_manual_bug_refill_once()
            return
        if policy and policy.get("phase") != "bug_top_up":
            # The mixed pool target alone can be filled entirely by Feature
            # and 0–1 tasks. Maintain the separate Bug reserve in this phase.
            self._schedule_manual_bug_refill_once()
            self._schedule_mixed_refill_once()
            return
        if bool(self.db.setting("manual_bug_only_mode", False)):
            self._schedule_manual_bug_refill_once()
            return
        zero_to_one_only = bool(self.db.setting("task_generation_zero_to_one_only", True))
        type_clause = " AND task_type='zero_to_one'" if zero_to_one_only else ""
        ready = (self.db.one(
            "SELECT COUNT(*) count FROM tasks WHERE status='ready' AND "
            + ELIGIBLE_TASK_SQL + type_clause
        ) or {"count": 0})["count"]
        minimum = int(self.db.setting("task_pool_min_ready", 6))
        target = int(self.db.setting("task_pool_target_ready", 12))
        with self._future_lock:
            active = sum(1 for key, future in self._futures.items() if key.startswith("validate-") and not future.done())
            generation_active = any(
                key.startswith(("generate-", "feature-", "bug-convert-", "bug-reproduce-", "bugs-"))
                and not future.done()
                for key, future in self._futures.items()
            )
        capacity = max(0, int(self.db.setting("task_generation_max_parallel", 6)) - active)
        if ready >= minimum:
            return
        needed = max(0, target - ready)
        candidates = self.db.all(
            """SELECT id FROM tasks WHERE status='candidate'
               AND difficulty IN ('困难','地狱')"""
            + type_clause + " ORDER BY created_at LIMIT ?",
            (min(capacity, needed),),
        )
        for row in candidates:
            self.validate_task_async(row["id"])
        if needed and capacity and not candidates and not generation_active:
            if zero_to_one_only:
                self._schedule_task_source("zero_to_one")
            else:
                self._schedule_any_task_source()

    def _schedule_manual_bug_refill_once(self) -> None:
        """Keep a hard-Bug reserve without enabling ordinary task generation."""
        if self.db.setting("pipeline_drain", False):
            return
        if self.db.setting("baseline_prewarm_enabled", True):
            queue = self.db.setting("manual_priority_task_pause", {})
            reserved = queue.get("reservedTaskIds", []) if isinstance(queue, dict) and queue.get("active") else []
            ready_tasks = {row["id"]: row for row in self.db.all(
                "SELECT * FROM tasks WHERE status='ready' AND task_type='bugfix' AND difficulty IN ('困难','地狱')"
            )}
            task = next((ready_tasks[item] for item in reserved if item in ready_tasks
                         and re.fullmatch(r"[0-9a-f]{40}", ready_tasks[item].get("baseline_sha") or "")
                         and Path(ready_tasks[item].get("baseline_path") or "/nonexistent").is_dir()), None)
            if task and task.get("baseline_sha") and task.get("baseline_path"):
                key = "baseline_warmed:" + task["baseline_sha"]
                if not self.db.setting(key, False):
                    self._submit_auto("bug-warm-" + task["baseline_sha"], self._prewarm_bug_baseline, task)
        if not bool(self.db.setting("manual_bug_auto_refill_enabled", False)):
            return
        target = max(0, int(self.db.setting("manual_bug_ready_target", 6)))
        policy = self._task_mix_policy()
        if policy.get("phase") == "bug_top_up" or self._task_mix_refill_deficit():
            target = min(target, self._task_mix_progress()["remaining"])
        ready = self._manual_bug_ready_count()
        if ready >= target:
            return
        with self._future_lock:
            active = any(
                key.startswith(("bugs-", "bug-reproduce-", "bug-convert-", "bug-source-repair-"))
                and not future.done()
                for key, future in self._futures.items()
            )
        if active:
            return
        scheduled = self._schedule_task_source("bugfix")
        self.db.audit("bug.auto_refill_checked", "scheduler", "manual-bug-reserve", {
            "ready": ready,
            "target": target,
            "scheduled": bool(scheduled),
            "mode": "system_bug_pipeline",
        })

    def _prewarm_bug_baseline(self, task: Dict[str, Any]) -> None:
        # Warming builds dependency layers only: no Pair, session, service,
        # business verification result or shared database is created.
        with DOCKER_WORK, tempfile.TemporaryDirectory(prefix="pairwise-warm-") as folder:
            root = Path(folder) / "snapshot"
            run_command(["git", "clone", "--no-hardlinks", "--no-checkout", task["baseline_path"], str(root)], timeout=180)
            run_command(["git", "checkout", "--detach", task["baseline_sha"]], cwd=root, timeout=60)
            compose = self.artifacts._compose_path(root)
            if not compose:
                raise ValueError("预热基线缺少 Compose")
            env, _ = isolated_compose_environment(compose)
            run_command(["docker", "compose", "-p", "bugwarm-" + task["baseline_sha"][:12],
                         "-f", str(compose), "--profile", "*", "build"], cwd=root, env=env, timeout=None)
        self.db.set_setting("baseline_warmed:" + task["baseline_sha"], True)
        self.db.audit("bug.baseline_warmed", "task", task["id"], {"baselineSha": task["baseline_sha"], "acceptanceSkipped": False})

    def preflight(self) -> Dict[str, Any]:
        return {
            "git": self.git.preflight(),
            "codex": self.codex.preflight(),
            "claude": self.claude.preflight(),
            "browserRecording": self.recordings.preflight(),
            "oldDb": {"ok": self.config.old_db_path.exists(), "path": str(self.config.old_db_path)},
        }

    def import_historical(self, limit: int = 500) -> Dict[str, int]:
        return import_historical_tasks(self.db, self.config.old_db_path, limit)

    def validate_task_async(self, task_id: str) -> str:
        operation = "validate-" + task_id
        self._submit(operation, self.validate_task, task_id)
        return operation

    def _task_baseline_evidence(self, task: Dict[str, Any]) -> str:
        if task.get("task_type") == "zero_to_one":
            return "0–1 从空仓库开始；必须仅依据题面判断最小正确实现的必要复杂度。"
        workspace = Path(str(task.get("baseline_path") or ""))
        baseline = str(task.get("baseline_sha") or "")
        if not workspace.is_dir() or not re.fullmatch(r"[0-9a-f]{40}", baseline):
            return json.dumps({
                "baselineReady": False,
                "path": str(workspace),
                "baselineSha": baseline,
            }, ensure_ascii=False)
        files_result = run_command(
            ["git", "ls-tree", "-r", "--name-only", baseline], cwd=workspace,
            check=False, timeout=60,
        )
        files = [line[:300] for line in files_result.stdout.splitlines()[:180]]
        readme = ""
        for name in ("README.md", "README"):
            shown = run_command(
                ["git", "show", "%s:%s" % (baseline, name)], cwd=workspace,
                check=False, timeout=60,
            )
            if shown.returncode == 0 and shown.stdout.strip():
                readme = shown.stdout[:8000]
                break
        return json.dumps({
            "baselineReady": files_result.returncode == 0,
            "baselineSha": baseline,
            "files": files,
            "readme": readme,
            "instruction": "需要时直接在当前目录用 git show <baselineSha>:<path> 查看准确基线源码。",
        }, ensure_ascii=False)

    @staticmethod
    def _normalized_task_text(value: Any) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())

    @classmethod
    def _task_text_similarity(cls, left: Any, right: Any) -> float:
        left_text = cls._normalized_task_text(left)
        right_text = cls._normalized_task_text(right)
        if not left_text or not right_text:
            return 0.0
        if left_text == right_text:
            return 1.0
        width = 3
        left_parts = {left_text[index:index + width] for index in range(max(1, len(left_text) - width + 1))}
        right_parts = {right_text[index:index + width] for index in range(max(1, len(right_text) - width + 1))}
        return (2.0 * len(left_parts & right_parts)) / max(1, len(left_parts) + len(right_parts))

    @classmethod
    def _shared_prompt_fragment(cls, left: Any, right: Any, width: int = 36) -> str:
        """Return a repeated normalized passage that whole-document similarity can hide."""
        left_text = cls._normalized_task_text(left)
        right_text = cls._normalized_task_text(right)
        if len(left_text) < width or len(right_text) < width:
            return ""
        right_parts = {
            right_text[index:index + width]
            for index in range(len(right_text) - width + 1)
        }
        for index in range(len(left_text) - width + 1):
            part = left_text[index:index + width]
            if part in right_parts:
                return part
        return ""

    @staticmethod
    def _task_business_text(value: Any) -> str:
        """Exclude generic deployment clauses from the long-fragment check.

        Exact prompt and title matches are still checked against the complete
        text. Delivery requirements are common to unrelated questions and are
        not evidence that their engineering cores are duplicates.
        """
        sentences = re.split(r"[。！？!?]+", str(value or ""))
        delivery_markers = ("dockerfile", "docker compose", "compose", "verify")
        return "。".join(sentence for sentence in sentences
                         if sentence.strip() and not any(
                             marker in sentence.casefold() for marker in delivery_markers
                         ))

    @staticmethod
    def _bug_repeats_parent_failure(title: str, parent_title: str) -> bool:
        """Catch a repaired Bug reissued with different boundary numbers.

        This deliberately compares the short failure claims, not full prompts:
        the latter contain shared regression and Compose wording.  A common
        business axis plus the same false-acceptance symptom is required.
        """
        left, right = str(title or ""), str(parent_title or "")
        axes = ("载荷", "力矩", "容量", "流量", "成本", "代价", "库存", "权限")
        same_axis = any(axis in left and axis in right for axis in axes)
        false_accept = re.compile(r"超限|超载|越界|未超限|误放行|错误放行")
        accepted = re.compile(r"可行|放行|接受|误判|当作|判为")
        return bool(same_axis and all(false_accept.search(text) and accepted.search(text)
                                      for text in (left, right)))

    def _historical_task_catalog(self, exclude_task_id: str = "") -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = [
            {
                "key": str(row.get("id") or ""), "source": "本系统题库",
                "title": str(row.get("title") or ""), "taskType": str(row.get("task_type") or ""),
                "prompt": str(row.get("prompt") or ""), "status": str(row.get("status") or ""),
                "createdAt": str(row.get("created_at") or ""),
            }
            for row in self.db.all(
                "SELECT id,title,task_type,prompt,status,created_at FROM tasks WHERE id<>? ORDER BY created_at DESC LIMIT 1500",
                (exclude_task_id,),
            )
        ]
        old_path = Path(self.config.old_db_path)
        if old_path.is_file():
            try:
                source = sqlite3.connect("file:%s?mode=ro" % old_path, uri=True)
                source.row_factory = sqlite3.Row
                try:
                    table = source.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='solo_qa_prompt_history'"
                    ).fetchone()
                    if table:
                        for row in source.execute(
                            """SELECT remote_submission_id,repo_name,prompt,task_type,remote_status
                                 FROM solo_qa_prompt_history WHERE trim(prompt)<>''
                                 ORDER BY COALESCE(remote_updated_at,submitted_at,last_synced_at) DESC LIMIT 1000"""
                        ).fetchall():
                            rows.append({
                                "key": "remote-" + str(row["remote_submission_id"] or ""),
                                "source": "历史提交题库", "title": str(row["repo_name"] or ""),
                                "taskType": str(row["task_type"] or ""), "prompt": str(row["prompt"] or ""),
                                "status": str(row["remote_status"] or ""), "createdAt": "",
                            })
                finally:
                    source.close()
            except sqlite3.Error:
                pass
        unique: List[Dict[str, Any]] = []
        seen = set()
        for row in rows:
            normalized = self._normalized_task_text(row.get("prompt"))
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(row)
        return unique

    def _task_duplicate_context(self, task: Dict[str, Any], exclude_task_id: str = "") -> List[Dict[str, Any]]:
        prompt = str(task.get("prompt") or "")
        title = str(task.get("title") or "")
        ranked = []
        catalog = self._historical_task_catalog(exclude_task_id)
        for index, row in enumerate(catalog):
            prompt_score = self._task_text_similarity(prompt, row.get("prompt"))
            title_score = self._task_text_similarity(title, row.get("title"))
            ranked.append((max(prompt_score, title_score * 0.75), index, row))
        selected = sorted(ranked, key=lambda item: (-item[0], item[1]))[:28]
        selected_keys = {item[2]["key"] for item in selected}
        selected.extend(
            (0.0, index, row) for index, row in enumerate(catalog[:20])
            if row["key"] not in selected_keys
        )
        return [
            {
                "source": row["source"], "title": row["title"], "taskType": row["taskType"],
                "summary": row["prompt"][:360], "similarityHint": round(score, 3),
            }
            for score, _, row in selected[:40]
        ]

    def _task_generation_context(self) -> List[Dict[str, Any]]:
        catalog = self._historical_task_catalog()
        current = [row for row in catalog if row["source"] == "本系统题库"][:100]
        historical = [row for row in catalog if row["source"] == "历史提交题库"][:40]
        return [
            {
                "source": row["source"], "title": row["title"], "taskType": row["taskType"],
                "status": row["status"], "summary": row["prompt"][:220],
            }
            for row in current + historical
        ]

    def _preferred_zero_to_one_categories(self) -> List[str]:
        """Return the optional user-selected project-shape preference.

        This is a preference rather than a fixed quota: ready tasks outside
        these categories remain usable as a fallback, so generation cannot
        stall when a preferred candidate fails admission.
        """
        value = self.db.setting("zero_to_one_preferred_categories", [])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        allowed = ("纯前端", "全栈", "纯后端")
        return [item for item in allowed if item in value]

    def _preferred_zero_to_one_generation_category(self) -> str:
        preferred = self._preferred_zero_to_one_categories()
        if not preferred:
            return ""
        rows = self.db.all(
            """SELECT project_category,COUNT(*) count FROM tasks
                 WHERE task_type='zero_to_one'
                 GROUP BY project_category"""
        )
        counts = {
            str(row.get("project_category") or ""): int(row.get("count") or 0)
            for row in rows
        }
        order = {category: index for index, category in enumerate(preferred)}
        return min(preferred, key=lambda category: (counts.get(category, 0), order[category]))

    def _deterministic_task_duplicate(self, task: Dict[str, Any], exclude_task_id: str = "",
                                      selection: bool = False) -> str:
        prompt = str(task.get("prompt") or "")
        title = str(task.get("title") or "")
        normalized = self._normalized_task_text(prompt)
        if not normalized:
            return ""
        if any(fragment in normalized for fragment in A9_REJECTED_PROMPT_FRAGMENTS):
            return "题面沿用了已被质检平台 A-9 判定为模板换皮的 Bug 固定骨架，必须重新出题"
        parent_pair_id = str(task.get("parent_pair_id") or "")
        if str(task.get("task_type") or "") == "bugfix" and parent_pair_id:
            parent_task = self.db.one(
                """SELECT t.title,t.task_type FROM tasks t JOIN pairs p ON p.task_id=t.id
                     WHERE p.id=?""", (parent_pair_id,),
            ) or {}
            if (parent_task.get("task_type") == "bugfix"
                    and self._bug_repeats_parent_failure(title, parent_task.get("title", ""))):
                return "新 Bug 与来源 Pair 正在修复的同一业务越界缺陷重复，不能只更换数值尺度再出题"
        allow_previous_period_reuse = str(task.get("source") or "") == "legacy"
        for row in self._historical_task_catalog(exclude_task_id):
            if allow_previous_period_reuse and row["source"] == "历史提交题库":
                continue
            if selection and row["source"] == "本系统题库" and row.get("status") != "used":
                older_ready = (
                    row.get("status") == "ready"
                    and (str(row.get("createdAt") or ""), str(row.get("key") or ""))
                    < (str(task.get("created_at") or ""), str(task.get("id") or ""))
                )
                if not older_ready:
                    continue
            other_prompt = str(row.get("prompt") or "")
            other_normalized = self._normalized_task_text(other_prompt)
            business_prompt = self._task_business_text(prompt)
            other_business = self._task_business_text(other_prompt)
            score = self._task_text_similarity(business_prompt, other_business)
            same_title = bool(
                self._normalized_task_text(title)
                and self._normalized_task_text(title) == self._normalized_task_text(row.get("title"))
            )
            if same_title:
                return "题目标题与%s中的“%s”重复，需要更换核心问题" % (
                    row["source"], row["title"] or row["key"],
                )
            if normalized == other_normalized:
                return "题面与%s中的“%s”完全重复" % (row["source"], row["title"] or row["key"])
            shared_fragment = (
                self._shared_prompt_fragment(business_prompt, other_business)
                if len(normalized) >= 120 and len(other_normalized) >= 120 else ""
            )
            if shared_fragment:
                return "题面与%s中的“%s”存在重复长骨架，必须改换任务组织和专用验收表达" % (
                    row["source"], row["title"] or row["key"],
                )
            # SOLO-QA also rejects a reused delivery/verify passage as an A-9
            # prompt scaffold even when the two business problems differ.
            if self._shared_prompt_fragment(prompt, other_prompt, width=40):
                return "题面与%s中的“%s”复用了交付或验收长句，存在 A-9 模板换皮风险" % (
                    row["source"], row["title"] or row["key"],
                )
            if len(normalized) >= 120 and score >= 0.82:
                return "题面与%s中的“%s”高度相似（%.0f%%），需要更换核心问题和验收机制" % (
                    row["source"], row["title"] or row["key"], score * 100,
                )
        return ""

    @classmethod
    def _relocate_bug_compatibility_clause(cls, prompt: str) -> str:
        """Move an already-written Compose clause off a prompt boundary.

        This only reorders the model's words. It does not invent acceptance
        requirements or waive the normal prompt and duplicate checks.
        """
        sentences = [part.strip() for part in re.findall(r"[^。！？!?]+[。！？!?]?", prompt)
                     if part.strip()]
        if len(sentences) < 3:
            return prompt
        moved: List[str] = []
        for index in (len(sentences) - 1, 0):
            body = sentences[index].rstrip("。！？!?")
            clauses = [part.strip() for part in re.split(r"[；;]", body) if part.strip()]
            if not clauses:
                continue
            kept = []
            for clause in clauses:
                compact = cls._normalized_task_text(clause)
                if "compose" in compact and any(marker in compact for marker in (
                    "verify", "健康", "启动", "验收链路", "运行",
                )):
                    moved.append(clause)
                else:
                    kept.append(clause)
            if len(kept) != len(clauses):
                sentences[index] = "；".join(kept) + "。" if kept else ""
        if not moved:
            return prompt
        sentences = [part for part in sentences if part]
        if len(sentences) < 3:
            return prompt
        # Attach to a business sentence, never as a standalone template line.
        sentences[1] = sentences[1].rstrip("。！？!?") + "；" + "；".join(moved) + "。"
        return "".join(sentences)

    @classmethod
    def _bugfix_prompt_issues(cls, prompt: str, *, new_task: bool = False) -> List[str]:
        text = str(prompt or "").strip()
        normalized = cls._normalized_task_text(text)
        issues: List[str] = []
        if len(text) < 200:
            issues.append("题面少于 200 字，未完整说明复现和验收")
        if len(text) > 1200:
            issues.append("题面超过 1200 字，应只保留问题、关键输入、正确行为和回归范围")
        if "```" in text:
            issues.append("公开题面不得包含代码块或完整复现命令")
        if re.search(r"(?im)^\s*(?:\$\s*)?(?:docker\s+compose\s+(?:exec|run|build|up)|curl\b|wget\b|psql\b|python(?:3)?\s+-c\b|node\s+-e\b)", text):
            issues.append("公开题面不得粘贴 Shell、SQL 或内联程序命令")
        if re.search(r"(?m)^\s*(?:#{1,6}\s*)?(?:前置条件|复现步骤|实际结果|预期结果)\s*[：:]", text):
            issues.append("仍在使用前置条件、复现步骤、实际结果、预期结果固定分段")
        if re.search(r"(?m)^\s*(?:需要修复|缺陷场景)\s*[：:]", text):
            issues.append("仍在使用已经停用的 Bug 固定开头")
        if re.search(r"(?:两次|双次)[^。！？\n]{0,20}(?:清洁|独立)[^。！？\n]{0,20}Docker", text, re.I):
            issues.append("公开题面不得叙述内部双次清洁 Docker 复现过程，只保留可观察结果")
        if any(cls._normalized_task_text(fragment) in normalized for fragment in RETIRED_BUG_PROMPT_FRAGMENTS):
            issues.append("仍在使用已经停用的 Bug 固定句式")
        if any(fragment in normalized for fragment in A9_REJECTED_PROMPT_FRAGMENTS):
            issues.append("仍在使用 A-9 已拒绝的固定结尾")
        # Private expected results may contain a repair plan. Do not expose
        # internal validation timing or fallback paths in the public prompt.
        if (re.search(r"(?:用于[^。！？\n]{0,24}前|(?:裁决|判定|验收)前)[^。！？\n]{0,36}(?:识别|检测|校验|验证)[^。！？\n]{0,36}(?:侧车|索引|摘要)", text)
                or re.search(r"(?:改由|回退到|回退为|转而使用)[^。！？\n]{0,40}(?:DER|侧车|索引|清单|源码)", text, re.I)):
            issues.append("公开题面泄露内部校验时机或回退路径，属于指定修法")
        if re.search(r"(?:可以|应|需要|须|必须)[^。！？\n]{0,20}(?:整体|全局)平移[^。！？\n]{0,16}(?:链|段|基准)", text):
            issues.append("公开题面指定了全局平移等实现方法，应只描述可观察的预期结果")
        if re.search(r"(?:唯一正确|唯一可行)[^。！？\n]{0,30}(?:选择|方案)[^。！？\n]{0,30}(?:每轮|逐轮)[^。！？\n]{0,25}(?:取|选择)", text):
            issues.append("公开题面给出了逐轮精确答案，应只描述可观察的总量与结果")
        if re.search(r"(?:不得|不能|避免)[^。！？\n]{0,24}(?:数值表示|浮点|比较误差|舍入误差|精度丢失|类型转换)", text):
            issues.append("公开题面指出了内部数值机制，应只描述输入边界和可观察结果")
        body_parts = [
            part.strip() for part in re.split(r"[。！？!?\n]+", text)
            if part.strip() and not re.match(r"^#{1,6}\s+", part.strip())
        ]
        for position, part in (("开头", body_parts[0] if body_parts else ""),
                               ("结尾", body_parts[-1] if body_parts else "")):
            compact = cls._normalized_task_text(part)
            if ("compose" in compact
                    and any(marker in compact for marker in (
                        "verify", "健康", "启动", "验收链路", "运行",
                    ))):
                issues.append(f"Compose/verify 兼容描述不得放在{position}句")
            if ("dockercompose" in compact and "verify" in compact
                    and any(marker in compact for marker in (
                        "仍须正常", "仍需正常", "保持正常", "继续正常", "正常运行", "正常退出",
                    ))):
                issues.append(f"Docker Compose/verify 兼容要求不得作为固定{position}或独立收尾")
        for part in (clause.strip() for sentence in body_parts
                     for clause in re.split(r"[；;]", sentence)):
            compact = cls._normalized_task_text(part)
            if re.fullmatch(
                r"(?:处理这一场景时)?(?:现有|既有)dockercompose启动(?:方式)?"
                r"(?:健康检查和)?(?:一次性)?verify验收链路(?:仍)?须(?:继续)?可用",
                compact,
            ):
                issues.append("Docker Compose/verify 兼容要求是独立模板句或分号子句，须融入具体业务回归")
                break
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        if len(paragraphs) > 1:
            closing_first_sentence = re.split(r"[。！？!?]", paragraphs[-1], maxsplit=1)[0].strip()
            if re.match(
                r"^(?:修复后|完成后|改动后|提交后)?[^。！？]{0,14}"
                r"(?:现有|既有)\s*Docker\s*Compose[^。！？]{0,80}"
                r"(?:启动|运行|验收|verify|链路|流程)",
                closing_first_sentence,
                re.I,
            ):
                issues.append("Docker Compose 兼容要求不得作为独立收尾段的通用前导句")
        browser_marker = task_prompt_browser_policy_marker(text)
        if browser_marker:
            issues.append(
                "Bug 题面不得包含浏览器自动化要求；验收只使用代码测试、构建检查、"
                "API/HTTP 冒烟或直接业务模块调用"
            )
        return issues

    def _bug_prompt_template_issues(self, prompt: str, exclude_task_id: str = "") -> List[str]:
        """Reject repeated natural-language openings/endings even without headings."""
        normalized = self._normalized_task_text(prompt)
        if len(normalized) < 160:
            return []
        opening = normalized[:32]
        closing = normalized[-48:]
        # A unique bug title can hide a repeated second-sentence scaffold.
        # Keep Compose/verify compatibility inside the business regression,
        # not as the same generic lead-in after every title.
        def compatibility_lead(value: str) -> bool:
            sentences = re.split(r"[。！？!?]", str(value or ""), maxsplit=2)
            if len(sentences) < 2:
                return False
            second = self._normalized_task_text(sentences[1])
            return bool(re.match(
                r"^(?:保留|保持|沿用)(?:现有|既有)dockercompose(?:启动|运行)",
                second,
            ) and "健康检查" in second and "verify" in second)
        repeated_compatibility_lead = compatibility_lead(prompt)
        issues: List[str] = []
        for row in self.db.all(
            """SELECT id,prompt FROM tasks
                 WHERE task_type='bugfix' AND id<>? AND prompt<>''
                 ORDER BY created_at DESC LIMIT 300""",
            (exclude_task_id,),
        ):
            other = self._normalized_task_text(row.get("prompt"))
            if len(other) < 160:
                continue
            if opening == other[:32]:
                issues.append("题面复用了已有 Bug 的固定开头")
            if repeated_compatibility_lead and compatibility_lead(row.get("prompt")):
                issues.append("题面复用了已有 Bug 的 Compose/verify 开头骨架")
            if closing == other[-48:]:
                issues.append("题面复用了已有 Bug 的固定结尾")
            if issues:
                break
        return issues

    @staticmethod
    def _bugfix_public_title(prompt: str, fallback: str = "Bug 修复") -> str:
        """Build the public title from sanitized copy instead of discovery notes."""
        text = str(prompt or "").strip()
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
            if heading:
                title = heading.group(1).strip().strip("#").strip()
                if title:
                    return title[:160]
            break
        first = re.split(r"[。！？!?\n]", text, maxsplit=1)[0].strip().lstrip("#").strip()
        if first:
            return first[:160]
        return str(fallback or "Bug 修复").strip()[:160] or "Bug 修复"

    def _generate_bugfix_task_prompt(self, candidate: Dict[str, Any], arm: Dict[str, Any],
                                     source_task: Dict[str, Any]) -> str:
        raw_results = json.loads(str(candidate.get("reproduction_results_json") or "[]"))
        reproduction_results = []
        for raw_attempt in raw_results[:2] if isinstance(raw_results, list) else []:
            if not isinstance(raw_attempt, dict):
                continue
            compact_attempt = {
                "attempt": raw_attempt.get("attempt"),
                "passed": bool(raw_attempt.get("passed")),
                "startExitCode": raw_attempt.get("startExitCode"),
                "commands": [],
            }
            for raw_command in (raw_attempt.get("commands") or [])[:8]:
                if not isinstance(raw_command, dict):
                    continue
                compact_attempt["commands"].append({
                    "composeArgs": raw_command.get("composeArgs") or [],
                    "exitCode": raw_command.get("exitCode"),
                    "expectedExitCode": raw_command.get("expectedExitCode"),
                    "expectedOutputContains": raw_command.get("expectedOutputContains") or "",
                    "matched": bool(raw_command.get("matched")),
                    "outputTail": str(raw_command.get("output") or "")[-1800:],
                })
            reproduction_results.append(compact_attempt)
        evidence = {
            "title": candidate.get("title"),
            "preconditions": candidate.get("preconditions"),
            "steps": json.loads(str(candidate.get("reproduction_steps_json") or "[]")),
            "reproductionCommands": json.loads(str(candidate.get("reproduction_commands_json") or "[]")),
            "reproductionResults": reproduction_results,
            "actual": candidate.get("actual_result"),
            "expected": candidate.get("expected_result"),
            "difficulty": candidate.get("difficulty"),
            "difficultyEvidence": json.loads(str(candidate.get("difficulty_evidence_json") or "[]")),
            "estimatedModuleCount": int(candidate.get("estimated_module_count") or 0),
            "estimatedSourceLines": [
                int(candidate.get("estimated_source_lines_min") or 0),
                int(candidate.get("estimated_source_lines_max") or 0),
            ],
            "estimatedMinutes": [
                int(candidate.get("estimated_minutes_min") or 0),
                int(candidate.get("estimated_minutes_max") or 0),
            ],
            "complexityAxes": json.loads(str(candidate.get("complexity_axes_json") or "[]")),
        }
        seed = {
            "source": "bug_discovery", "task_type": "bugfix",
            "title": str(candidate.get("title") or ""),
            "prompt": "\n".join(str(evidence.get(key) or "") for key in (
                "title", "preconditions", "actual", "expected",
            )),
        }
        prior_task = self.db.one(
            """SELECT id FROM tasks WHERE source='bug_discovery' AND source_id=?
                 ORDER BY created_at DESC,id DESC LIMIT 1""",
            (candidate["id"],),
        ) or {}
        exclude_task_id = str(prior_task.get("id") or "")
        existing = self._task_duplicate_context(seed, exclude_task_id=exclude_task_id)
        previous = ""
        correction = ""
        duplicate = ""
        workspace = Path(str(arm.get("workspace_path") or ""))
        for attempt in (1, 2):
            result = self.codex.run(
                "bug_task_generation",
                bugfix_task_prompt(
                    json.dumps(evidence, ensure_ascii=False, indent=2),
                    json.dumps(existing, ensure_ascii=False, indent=2),
                    previous, correction,
                ),
                BUG_TASK_PROMPT_SCHEMA,
                cwd=workspace if workspace.is_dir() else None,
                pair_id=str(candidate.get("source_pair_id") or ""),
                task_id=str(source_task.get("id") or ""),
                timeout=1200,
            )
            draft = str(result.get("prompt") or "").strip()
            prompt = self._relocate_bug_compatibility_clause(draft)
            issues = self._bugfix_prompt_issues(prompt, new_task=True)
            # Moving a stock closing sentence cannot make a repeated template
            # acceptable; ask the generator for a genuinely different draft.
            issues.extend(issue for issue in self._bugfix_prompt_issues(draft, new_task=True)
                          if "固定结尾" in issue)
            issues.extend(self._bug_prompt_template_issues(prompt, exclude_task_id))
            duplicate = self._deterministic_task_duplicate({
                "source": "bug_discovery", "task_type": "bugfix",
                "parent_pair_id": str(candidate.get("source_pair_id") or ""),
                "title": self._bugfix_public_title(prompt, str(candidate.get("title") or "")),
                "prompt": prompt,
            }, exclude_task_id=exclude_task_id)
            if duplicate:
                issues.append(duplicate)
            if not issues:
                self.db.audit("bug.prompt_generated", "bug_candidate", candidate["id"], {
                    "attempt": attempt,
                    "evidenceUsed": result.get("evidenceUsed") or [],
                    "promptLength": len(prompt),
                })
                return prompt
            previous = prompt
            correction = "；".join(issues)
        stamp = now_iso()
        # Two completed drafts have failed deterministic public-prompt checks.
        # This candidate must leave the admission queue as rejected, rather
        # than looking like a transient generation failure eligible for retry.
        status = "duplicate_rejected" if duplicate else "rejected"
        self.db.execute(
            "UPDATE bug_candidates SET status=?,error=?,updated_at=? WHERE id=?",
            (status, correction[-2000:], stamp, candidate["id"]),
        )
        self.db.audit("bug.prompt_generation_failed", "bug_candidate", candidate["id"], {
            "status": status, "reason": correction,
        })
        raise ValueError(correction)

    def _task_duration_calibration(self, task: Dict[str, Any]) -> str:
        """Give the blind reviewer comparable clean development durations."""
        rows = self.db.all(
            """SELECT a.prompt_sent_at,a.finished_at FROM arm_runs a
                 JOIN pairs p ON p.id=a.pair_id JOIN tasks t ON t.id=p.task_id
                WHERE a.status='completed' AND a.prompt_sent_at IS NOT NULL
                  AND a.finished_at IS NOT NULL AND a.api_retry_count=0
                  AND a.last_api_error='' AND t.task_type=? AND t.project_category=?
                ORDER BY a.finished_at DESC LIMIT 80""",
            (task.get("task_type"), task.get("project_category")),
        )
        durations: List[int] = []
        for row in rows:
            try:
                start = datetime.fromisoformat(str(row["prompt_sent_at"]).replace("Z", "+00:00"))
                finish = datetime.fromisoformat(str(row["finished_at"]).replace("Z", "+00:00"))
                minutes = round((finish - start).total_seconds() / 60)
            except (ValueError, TypeError):
                continue
            if 10 <= minutes <= 300:
                durations.append(minutes)
        if len(durations) < 5:
            return "同类型与系统类别的清洁完成样本不足 5 条；请独立逐项估时。"
        durations.sort()
        median = durations[(len(durations) - 1) // 2]
        upper = durations[round((len(durations) - 1) * 0.75)]
        return (
            f"同类型、同系统类别、无 API 重试的 {len(durations)} 条已完成 Arm："
            f"从题面发送到结束的中位数 {median} 分钟、75 分位 {upper} 分钟。"
            "这些是历史墙钟时间而非本题工时，不可代替本题的独立工作项估算。"
        )

    def validate_task(self, task_id: str) -> Dict[str, Any]:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise KeyError("任务不存在")
        if task.get("task_type") == "feature" and self._feature_project_rank(task) > MAX_FEATURE_TASKS_PER_PROJECT:
            reason = "同一基线项目最多保留 3 个 Feature 迭代，超出额度后应重新创建 0–1 项目"
            result = {
                "accepted": False, "difficulty": task.get("difficulty") or "困难",
                "difficultyEvidence": json.loads(task.get("difficulty_evidence_json") or "[]"),
                "banned": False, "duplicate": True,
                "baselineReady": bool(task.get("baseline_path") and task.get("baseline_sha")),
                "reason": reason,
            }
            self.db.execute(
                "UPDATE tasks SET status='rejected',rejection_reason=?,updated_at=? WHERE id=?",
                (reason, now_iso(), task_id),
            )
            self.db.audit("feature.project_limit_rejected", "task", task_id, {
                "reason": reason, "project": self._feature_project_key(task),
            })
            return {"taskId": task_id, "status": "rejected", "result": result}
        if (task.get("source") in ("generated", "generated_followup")
                and task.get("task_type") in ("zero_to_one", "feature")):
            try:
                acceptance = json.loads(task.get("acceptance_json") or "[]")
            except (TypeError, ValueError):
                acceptance = []
            scope_issues = generated_task_prompt_issues(
                str(task.get("task_type") or ""), str(task.get("prompt") or ""), acceptance,
            )
            scope_issues.extend(generated_task_estimate_issues(task))
            if scope_issues:
                reason = "题面范围或表达未通过本地校验：" + "；".join(scope_issues)
                result = {
                    "accepted": False,
                    "difficulty": task.get("difficulty") or "困难",
                    "difficultyEvidence": json.loads(task.get("difficulty_evidence_json") or "[]"),
                    "banned": False,
                    "duplicate": False,
                    "baselineReady": task.get("task_type") == "zero_to_one" or bool(
                        task.get("baseline_path") and task.get("baseline_sha")
                    ),
                    "reason": reason,
                }
                self.db.execute(
                    "UPDATE tasks SET status='rejected',rejection_reason=?,updated_at=? WHERE id=?",
                    (reason[-2000:], now_iso(), task_id),
                )
                self.db.audit("task.scope_rejected", "task", task_id, {"issues": scope_issues})
                return {"taskId": task_id, "status": "rejected", "result": result}
        titles = self._task_duplicate_context(task, exclude_task_id=task_id)
        recent_rejections = self.db.all(
            """SELECT t.title,t.task_type,d.assessed_difficulty,d.reason
                 FROM difficulty_reviews d JOIN pairs p ON p.id=d.pair_id
                 JOIN tasks t ON t.id=p.task_id
                WHERE d.status='rejected' ORDER BY d.reviewed_at DESC LIMIT 12"""
        )
        payload = dict(task)
        payload["acceptance"] = json.loads(task.get("acceptance_json") or "[]")
        generated = (task.get("source") in ("generated", "generated_followup")
                     and task.get("task_type") in ("zero_to_one", "feature"))
        if generated:
            # The reviewer must estimate from the requirements and baseline,
            # not anchor on the generator's self-reported range or rationale.
            for field in ("difficulty", "estimated_minutes_min", "estimated_minutes_max",
                          "estimated_module_count", "estimated_source_lines_min",
                          "estimated_source_lines_max", "difficulty_evidence_json"):
                payload.pop(field, None)
        baseline_evidence = self._task_baseline_evidence(task)
        prompt = task_validation_prompt(
            json.dumps(payload, ensure_ascii=False, indent=2),
            json.dumps(titles, ensure_ascii=False),
            baseline_evidence,
            json.dumps(recent_rejections, ensure_ascii=False),
            self._task_duration_calibration(task) if generated else "",
            generated,
        )
        cwd = Path(str(task.get("baseline_path") or ""))
        result = self.codex.run(
            "task_validation", prompt,
            ESTIMATED_VALIDATION_SCHEMA if generated else VALIDATION_SCHEMA,
            cwd=cwd if cwd.is_dir() else None, task_id=task_id,
        )
        reviewed_estimate: Dict[str, Any] = {}
        if generated:
            try:
                reviewed_estimate = summarize_reviewed_estimate(
                    result.get("workItems"),
                    int(task.get("estimated_minutes_min") or 0),
                    int(task.get("estimated_minutes_max") or 0),
                )
                if reviewed_estimate["max"] > GENERATED_TASK_MAX_ESTIMATED_MINUTES:
                    result["accepted"] = False
                    result["reason"] = f"独立估时显示完整开发与 Docker/verify 验证超过 {GENERATED_TASK_MAX_ESTIMATED_MINUTES} 分钟，应缩小范围重新生成"
            except ValueError as exc:
                result["accepted"] = False
                result["reason"] = "独立估时未通过：" + str(exc)
            result["reviewedEstimate"] = reviewed_estimate
        duplicate = self._deterministic_task_duplicate(task, exclude_task_id=task_id)
        if duplicate:
            result["accepted"] = False
            result["duplicate"] = True
            result["reason"] = duplicate
        if not task_difficulty_allowed(str(task.get("task_type") or ""), str(result["difficulty"])):
            result["accepted"] = False
            result["reason"] = (
                "独立复核的最小正确实现低于困难，题目不准入："
                + str(result.get("reason") or "缺少困难主轴的可核对证据")
            )[:500]
        if result["accepted"] and len({
            str(item).strip() for item in (result.get("difficultyEvidence") or [])
            if str(item).strip()
        }) < 2:
            result["accepted"] = False
            result["reason"] = "独立复核未给出两条不同的困难难度依据，不能仅凭难度标签准入"
        accepted = bool(
            result["accepted"] and not result["banned"] and not result["duplicate"]
            and result["baselineReady"]
            and task_difficulty_allowed(str(task.get("task_type") or ""), str(result["difficulty"]))
        )
        status = "ready" if accepted else "rejected"
        reason = "" if accepted else str(result.get("reason") or "未通过题目准入")
        self.db.execute(
            """UPDATE tasks SET status=?,difficulty=?,difficulty_evidence_json=?,rejection_reason=?,
                 reviewed_minutes_min=?,reviewed_minutes_max=?,estimate_work_items_json=?,estimate_risk=?,
                 updated_at=? WHERE id=?""",
            (status, result["difficulty"], json.dumps(result["difficultyEvidence"], ensure_ascii=False),
             reason, int(reviewed_estimate.get("min") or 0), int(reviewed_estimate.get("max") or 0),
             json.dumps(reviewed_estimate.get("workItems") or [], ensure_ascii=False),
             str(reviewed_estimate.get("risk") or ""), now_iso(), task_id),
        )
        self.db.audit("task.validated", "task", task_id, {"status": status, "result": result})
        return {"taskId": task_id, "status": status, "result": result}

    def generate_tasks_async(self, count: int = 1, task_type: str = "zero_to_one") -> str:
        operation = "generate-" + uuid.uuid4().hex[:12]
        self._submit(operation, self.generate_tasks, count, task_type)
        return operation

    def generate_tasks(self, count: int = 1, task_type: str = "zero_to_one") -> Dict[str, Any]:
        if bool(self.db.setting("manual_bug_only_mode", False)):
            raise ValueError("当前为人工 Bug-only 模式，已停用自动出题")
        batch_id = "batch-" + uuid.uuid4().hex[:16]
        count = min(20, max(1, int(count)))
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO generation_batches(id,status,requested_count,created_at,updated_at) VALUES(?,?,?,?,?)",
            (batch_id, "running", count, stamp, stamp),
        )
        accepted: List[str] = []
        rejected = 0
        try:
            for _ in range(count):
                if self.db.setting("pipeline_drain", False):
                    break
                existing = self._task_generation_context()
                preferred_category = (
                    self._preferred_zero_to_one_generation_category()
                    if task_type == "zero_to_one" else ""
                )
                prompt = task_generation_prompt(
                    json.dumps(existing, ensure_ascii=False), task_type, preferred_category,
                )
                result = self.codex.run("task_generation", prompt, TASK_SCHEMA)
                if result.get("taskType") != task_type:
                    rejected += 1
                    continue
                original_prompt = str(result.get("prompt") or "")
                repaired_prompt = repair_generated_task_punctuation(
                    task_type, original_prompt, result.get("acceptance"), result,
                )
                if repaired_prompt != original_prompt:
                    result = {**result, "prompt": repaired_prompt}
                    self.db.audit("task.generated_format_repaired", "task", "", {
                        "title": result.get("title", ""), "taskType": task_type,
                        "beforeLength": len(original_prompt), "afterLength": len(repaired_prompt),
                    })
                result_category = normalize_project_category(
                    result.get("projectCategory"), result.get("stack"), result.get("prompt"),
                )
                if preferred_category and result_category != preferred_category:
                    rejected += 1
                    self.db.audit("task.generated_category_rejected", "task", "", {
                        "title": result.get("title", ""),
                        "expected": preferred_category,
                        "actual": result_category,
                    })
                    continue
                scope_issues = generated_task_prompt_issues(
                    task_type, str(result.get("prompt") or ""), result.get("acceptance"), result,
                )
                if scope_issues:
                    rejected += 1
                    self.db.audit("task.generated_scope_rejected", "task", "", {
                        "title": result.get("title", ""), "issues": scope_issues,
                    })
                    continue
                duplicate = self._deterministic_task_duplicate(result)
                if duplicate:
                    rejected += 1
                    self.db.audit("task.generated_duplicate_rejected", "task", "", {
                        "title": result.get("title", ""), "reason": duplicate,
                    })
                    continue
                task_id = "task-" + uuid.uuid4().hex[:16]
                key = fingerprint(result["taskType"], result["prompt"], "")
                if self.db.one("SELECT id FROM tasks WHERE fingerprint=?", (key,)):
                    rejected += 1
                    continue
                stamp = now_iso()
                self.db.execute(
                    """INSERT INTO tasks(id,source,task_type,title,prompt,stack,project_category,acceptance_json,difficulty,
                       difficulty_evidence_json,estimated_minutes_min,estimated_minutes_max,
                       fingerprint,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, "generated", result["taskType"], result["title"], result["prompt"], normalize_stack(result["stack"]),
                     result_category,
                     json.dumps(result["acceptance"], ensure_ascii=False), result["difficulty"],
                     json.dumps(result["difficultyEvidence"], ensure_ascii=False),
                     int(result.get("estimatedMinutesMin") or 0),
                     int(result.get("estimatedMinutesMax") or 0),
                     key, "candidate", stamp, stamp),
                )
                validation = self.validate_task(task_id)
                if validation["status"] == "ready":
                    accepted.append(task_id)
                else:
                    rejected += 1
            self.db.execute(
                """UPDATE generation_batches SET status='completed',generated_count=?,accepted_count=?,rejected_count=?,
                   finished_at=?,updated_at=? WHERE id=?""",
                (len(accepted) + rejected, len(accepted), rejected, now_iso(), now_iso(), batch_id),
            )
            return {"batchId": batch_id, "accepted": accepted, "rejected": rejected}
        except Exception as exc:
            self.db.execute("UPDATE generation_batches SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?", (str(exc)[-3000:], now_iso(), now_iso(), batch_id))
            raise

    def generate_followup_feature_async(self, pair_id: str) -> str:
        operation = "feature-" + pair_id
        self._submit(operation, self.generate_followup_feature, pair_id)
        return operation

    def generate_followup_feature(self, pair_id: str) -> Dict[str, Any]:
        if bool(self.db.setting("manual_bug_only_mode", False)):
            raise ValueError("当前为人工 Bug-only 模式，已停用 Feature 迭代出题")
        pair = self._pair(pair_id)
        if pair["status"] != "completed":
            raise ValueError("只有已完成 GSB 的 Pair 才能生成 Feature 迭代")
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        if task.get("task_type") != "zero_to_one":
            raise ValueError("Feature 迭代只能从已完成的 0–1 Pair 生成")
        existing_followup = self.db.one(
            "SELECT * FROM tasks WHERE parent_pair_id=? AND task_type='feature' AND status IN ('candidate','ready') ORDER BY created_at DESC LIMIT 1",
            (pair_id,),
        )
        if existing_followup:
            return existing_followup
        repository = self.db.one("SELECT remote_url FROM git_repositories WHERE pair_id=?", (pair_id,)) or {}
        project_seed = {
            "baseline_repo_url": repository.get("remote_url", ""),
            "parent_pair_id": pair_id,
            "title": task.get("title", ""),
        }
        if not self._feature_project_can_generate(project_seed):
            raise ValueError("此项目的有效 Feature 数量或候选尝试次数已到上限，请换用其他来源")
        selected = "B" if pair["winner"] == "B better" else "A"
        arm = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm=? AND status='completed'", (pair_id, selected))
        check = self.db.one("SELECT * FROM artifact_checks WHERE pair_id=? AND arm=? AND status='passed' ORDER BY created_at DESC LIMIT 1", (pair_id, selected))
        if not arm or not check or not arm.get("commit_sha"):
            raise ValueError("获胜产物缺少固定提交或 Docker 验收证据")
        checks = json.loads(check.get("checks_json") or "[]")
        if not any(
            isinstance(item, dict) and item.get("name") == "verify_service_present"
            and item.get("passed") is True for item in checks
        ):
            self.db.audit("feature.source_missing_verify", "pair", pair_id, {
                "arm": selected, "commit_sha": arm["commit_sha"],
                "reason": "来源固定提交缺少已验收的 verify 服务",
            })
            raise ValueError("来源固定提交缺少已验收的 verify 服务，不能生成新规则 Feature")
        workspace = Path(arm["workspace_path"])
        files = [str(path.relative_to(workspace)) for path in sorted(workspace.rglob("*"))
                 if path.is_file() and ".git" not in path.parts and not any(part in (".venv", "node_modules", "__pycache__") for part in path.parts)][:160]
        readme = next((p for p in (workspace / "README.md", workspace / "README") if p.exists()), None)
        readme_text = readme.read_text(encoding="utf-8", errors="ignore")[:10000] if readme else ""
        summary = json.dumps({
            "selectedArm": selected, "commitSha": arm["commit_sha"], "files": files,
            "readme": readme_text, "dockerCheck": json.loads(check.get("checks_json") or "[]"),
        }, ensure_ascii=False)
        known = self._task_generation_context()
        last_error = ""
        for _ in range(3):
            if self.db.setting("pipeline_drain", False):
                raise RuntimeError("流水线已暂停；Feature 出题将在恢复后重试")
            result = self.codex.run(
                "task_generation",
                feature_generation_prompt(task.get("prompt", ""), summary, json.dumps(known, ensure_ascii=False),
                                          task.get("project_category", "")),
                TASK_SCHEMA, cwd=workspace, pair_id=pair_id, task_id=pair["task_id"], timeout=1800,
            )
            original_prompt = str(result.get("prompt") or "")
            repaired_prompt = repair_generated_task_punctuation(
                "feature", original_prompt, result.get("acceptance"), result,
            )
            if repaired_prompt != original_prompt:
                result = {**result, "prompt": repaired_prompt}
                self.db.audit("task.generated_format_repaired", "pair", pair_id, {
                    "title": result.get("title", ""), "taskType": "feature",
                    "beforeLength": len(original_prompt), "afterLength": len(repaired_prompt),
                })
            if result.get("taskType") != "feature" or result.get("difficulty") not in ("困难", "地狱"):
                last_error = "生成结果不是困难或地狱 Feature"
                continue
            scope_issues = generated_task_prompt_issues(
                "feature", str(result.get("prompt") or ""), result.get("acceptance"), result,
            )
            if scope_issues:
                last_error = "；".join(scope_issues)
                continue
            duplicate = self._deterministic_task_duplicate(result)
            if duplicate:
                last_error = duplicate
                continue
            task_id = "task-" + uuid.uuid4().hex[:16]
            key = fingerprint("feature", result["prompt"], arm["commit_sha"])
            if self.db.one("SELECT id FROM tasks WHERE fingerprint=?", (key,)):
                last_error = "生成结果与已有 Feature 重复"
                continue
            stamp = now_iso()
            self.db.execute(
                """INSERT INTO tasks(id,source,source_id,task_type,title,prompt,stack,project_category,acceptance_json,difficulty,
                   difficulty_evidence_json,estimated_minutes_min,estimated_minutes_max,
                   baseline_path,baseline_repo_url,baseline_sha,parent_pair_id,fingerprint,
                   status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, "generated_followup", pair_id, "feature", result["title"], result["prompt"], normalize_stack(result["stack"]),
                 normalize_project_category(task.get("project_category"), result["stack"], result["prompt"]),
                 json.dumps(result["acceptance"], ensure_ascii=False), result["difficulty"],
                 json.dumps(result["difficultyEvidence"], ensure_ascii=False),
                 int(result.get("estimatedMinutesMin") or 0),
                 int(result.get("estimatedMinutesMax") or 0), str(workspace),
                 repository.get("remote_url", ""),
                 arm["commit_sha"], pair_id, key, "candidate", stamp, stamp),
            )
            validation = self.validate_task(task_id)
            if validation["status"] == "ready":
                self.db.audit("feature.followup_ready", "task", task_id, {"source_pair_id": pair_id, "source_arm": selected, "baseline_sha": arm["commit_sha"]})
                return self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,)) or {}
            last_error = str(validation["result"].get("reason") or "Feature 准入未通过")
        raise RuntimeError("未能生成可进入 A/B 的困难 Feature：%s" % last_error)

    def create_pair(self, task_id: str) -> Dict[str, Any]:
        # Capacity checks and insertion must be one operation.  The scheduler
        # and failed-task replacement path can otherwise both observe the same
        # free slot and create a fourth Pair concurrently.
        with self._pair_creation_lock:
            return self._create_pair_locked(task_id)

    def _new_pair_model_assignment(self) -> tuple:
        """Pin the A/B model names before either Arm or repository is prepared.

        Submissions, not failed development attempts, drive the rolling 1:2
        target. Pending new Pairs are counted too, so concurrent starts do not
        all claim the same one-in-three slot. Existing Pairs keep their models.
        """
        policy = self.db.setting("ab_submission_model_policy", {})
        default_model = str(self.db.setting("claude_model", self.config.claude_model))
        if not isinstance(policy, dict) or not policy.get("enabled"):
            return "legacy", default_model, default_model, {}
        started_at = str(policy.get("startedAt") or "")
        legacy_model = str(policy.get("legacyModel") or "")
        a_model = str(policy.get("aModel") or "")
        b_model = str(policy.get("bModel") or "")
        if not started_at or not legacy_model or not a_model or not b_model or a_model == b_model:
            raise ValueError("A/B 模型配比设置不完整或新方案两侧模型相同")
        counts = self.db.one(
            """SELECT COUNT(*) total,
                      COALESCE(SUM(CASE WHEN p.model_scheme='cross_model' THEN 1 ELSE 0 END),0) mixed
                 FROM pairs p LEFT JOIN delivery_submissions d ON d.pair_id=p.id
                WHERE (p.created_at>=? AND p.status NOT IN ('failed','cancelled')
                       AND COALESCE(d.status,'')<>'discarded')
                   OR (d.remote_id<>'' AND d.submitted_at>=?)""",
            (started_at, started_at),
        ) or {"total": 0, "mixed": 0}
        total, mixed = int(counts["total"]), int(counts["mixed"])
        # Mixed, legacy, legacy, then repeat; later failures or old-Pair
        # submissions are reflected in the next assignment's deficit.
        cross_model = mixed * 3 <= total
        return (
            "cross_model" if cross_model else "legacy",
            a_model if cross_model else legacy_model,
            b_model if cross_model else legacy_model,
            {"counted": total, "mixed": mixed, "policyStartedAt": started_at},
        )

    def _create_pair_locked(self, task_id: str) -> Dict[str, Any]:
        self._advance_task_mix_policy()
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise KeyError("任务不存在")
        policy_issues = self._task_mix_task_issues(task)
        if policy_issues:
            raise ValueError("；".join(policy_issues))
        if bool(self.db.setting("manual_bug_only_mode", False)):
            if task.get("task_type") != "bugfix":
                raise ValueError("当前为人工 Bug-only 模式，只允许创建人工准入的 Bug Pair")
            if task.get("difficulty") not in ("困难", "地狱"):
                raise ValueError("当前为人工 Bug-only 困难模式，只允许创建困难或地狱 Bug Pair")
            manual_queue = self.db.setting("manual_priority_task_pause", {})
            reserved = {
                str(item) for item in (manual_queue.get("reservedTaskIds") or [])
            } if isinstance(manual_queue, dict) and manual_queue.get("active") else set()
            if task_id not in reserved:
                raise ValueError("该 Bug 尚未加入人工准入队列，不能创建 Pair")
        if task.get("task_type") == "bugfix":
            browser_marker = task_prompt_browser_policy_marker(task.get("prompt"))
            if browser_marker:
                raise ValueError(
                    "Bug 题面包含浏览器自动化要求，不能启动；请改为代码测试、构建检查、"
                    "API/HTTP 冒烟或直接业务模块验收"
                )
        if task["status"] != "ready" or not task_difficulty_allowed(task["task_type"], task["difficulty"]):
            raise ValueError("所有新 Pair 只允许困难或地狱题目")
        active_count = (self.db.one("SELECT COUNT(*) count FROM development_pairs WHERE status IN ('queued','running','review')") or {"count": 0})["count"]
        configured_limit = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
        pair_limit = max(1, min(MAX_PAIR_PROJECTS, configured_limit))
        if active_count >= pair_limit:
            raise ValueError(
                "已达到 Pair 并发上限：最多 %d 个 Pair；开发终端并发另行限流"
                % pair_limit
            )
        scheme, model_a, model_b, model_counts = self._new_pair_model_assignment()
        pair_id = "pair-" + uuid.uuid4().hex[:16]
        if task["task_type"] == "zero_to_one":
            chain_id = "chain-" + uuid.uuid4().hex[:16]
            stamp = now_iso()
            self.db.execute(
                "INSERT INTO project_chains(id,root_task_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                (chain_id, task_id, "active", stamp, stamp),
            )
        else:
            parent = self.db.one("SELECT chain_id FROM pairs WHERE id=?", (task["parent_pair_id"],)) if task["parent_pair_id"] else None
            if parent:
                chain_id = parent["chain_id"]
            elif task["source"] == "legacy" and task["baseline_path"] and task["baseline_sha"]:
                # A reusable historical Feature has a verified task-time
                # baseline, but no Pair id in this new database. Keep it as a
                # self-contained imported chain rather than inventing lineage.
                chain_id = "chain-" + uuid.uuid4().hex[:16]
                stamp = now_iso()
                self.db.execute(
                    """INSERT INTO project_chains(id,root_task_id,status,followup_required,followup_completed,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (chain_id, task_id, "active", 0, 1, stamp, stamp),
                )
            else:
                raise ValueError("Feature 或 Bug 任务缺少来源项目链")
        stamp = now_iso()
        with self.db.transaction() as conn:
            claimed = conn.execute(
                """UPDATE tasks SET status='used',locked_by=?,used_at=?,updated_at=?
                     WHERE id=? AND status='ready'""",
                (pair_id, stamp, stamp, task_id),
            )
            if claimed.rowcount != 1:
                raise ValueError("题目已被其他 Pair 使用，不能重复创建")
            if conn.execute("SELECT id FROM pairs WHERE task_id=? LIMIT 1", (task_id,)).fetchone():
                raise ValueError("题目已存在 Pair，不能重复创建")
            conn.execute(
                """INSERT INTO pairs(id,task_id,chain_id,status,stage,model_scheme,model_a,model_b,
                   created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (pair_id, task_id, chain_id, "queued", "repository", scheme, model_a, model_b,
                 stamp, stamp),
            )
        self.db.audit("pair.created", "pair", pair_id, {"task_id": task_id, "chain_id": chain_id})
        self.db.audit("pair.models_assigned", "pair", pair_id, {
            "scheme": scheme, "A": model_a, "B": model_b, **model_counts,
        })
        self._advance_task_mix_policy()
        return self.pair_detail(pair_id)

    def prepare_pair_repository_async(self, pair_id: str) -> str:
        operation = "repo-" + pair_id
        self._submit(operation, self.prepare_pair_repository, pair_id)
        return operation

    def prepare_pair_repository(self, pair_id: str) -> Dict[str, Any]:
        # Replacement and scheduler paths may discover the same repository
        # stage at once. Serialize work for this Pair so a second caller sees
        # the first caller's ready repository instead of running `git remote
        # add origin` against the same baseline concurrently.
        with self._repository_locks_lock:
            repository_lock = self._repository_locks.setdefault(pair_id, threading.Lock())
        with repository_lock:
            pair = self._pair(pair_id)
            task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
            try:
                repo = self.git.create_pair_repository(pair, task)
                for arm in ("A", "B"):
                    self.claude.prepare_arm(pair, arm, Path(repo["local_root"]) / "workspaces" / arm)
                self.db.execute(
                    "UPDATE pairs SET stage='ready_to_start',error='',updated_at=? WHERE id=?",
                    (now_iso(), pair_id),
                )
                return self.pair_detail(pair_id)
            except Exception as exc:
                error = redact(str(exc))[-3000:]
                self.db.execute(
                    """UPDATE pairs SET status='failed',stage='repository_failed',error=?,updated_at=?
                       WHERE id=? AND stage='repository'""",
                    (error, now_iso(), pair_id),
                )
                self.db.audit("pair.repository_failed", "pair", pair_id, {"error": error})
                raise

    def start_pair_async(self, pair_id: str) -> str:
        operation = "start-" + pair_id
        self._submit(operation, self.start_pair, pair_id)
        return operation

    def start_pair(self, pair_id: str) -> Dict[str, Any]:
        # Replacement workers and the automatic scheduler can both observe a
        # freshly prepared Pair. Only one of them may launch its two Arms.
        with self._start_locks_lock:
            start_lock = self._start_locks.setdefault(pair_id, threading.Lock())
        with start_lock:
            return self._start_pair_locked(pair_id)

    def _start_pair_locked(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if pair["status"] not in ("queued", "running", "review"):
            raise ValueError("Pair 已停止，不能重新启动 A/B")
        # A replacement worker and the automatic scheduler can both queue a
        # start while the Pair is moving from ready_to_start to development.
        # The per-Pair lock serializes them; make the second caller a harmless
        # idempotent success instead of recording a false automation failure.
        if pair["stage"] == "development":
            runs = self.db.all("SELECT status FROM arm_runs WHERE pair_id=?", (pair_id,))
            if len(runs) == 2 and all(
                    run.get("status") in (
                        "queued", "running", "developing", "waiting_retry", "checkpointing",
                        "exported", "completed",
                    )
                    for run in runs):
                self._schedule_pending_arm_retries(pair_id)
                return self.pair_detail(pair_id)
        if pair["stage"] != "ready_to_start":
            raise ValueError("Pair 尚未完成仓库与 A/B 工作区准备")
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        repo = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,))
        if not repo or repo["status"] != "ready":
            raise RuntimeError("Pair 仓库尚未准备完成")
        preflight_already_passed = bool(self.db.one(
            """SELECT id FROM audit_events
                 WHERE event_type='task.baseline_preflight_passed'
                   AND entity_type='pair' AND entity_id=?
                 ORDER BY id DESC LIMIT 1""",
            (pair_id,),
        ))
        if task.get("task_type") != "zero_to_one" and not preflight_already_passed:
            cutoff = str(self.db.setting("dockerless_task_policy_started_at", "") or "")
            allow_adapter = bool(cutoff and str(task.get("created_at") or "") >= cutoff
                                 and not re.search(r"\b(?:docker|compose|dockerfile)\b", str(task.get("prompt") or ""), re.I))
            baseline_check = self.artifacts.preflight(
                Path(repo["local_root"]) / "A", pair_id,
                allow_system_adapter=allow_adapter,
            )
            if baseline_check.get("status") != "passed":
                reason = "开发前 Docker 基线预检失败：%s" % (
                    baseline_check.get("error") or "Compose 或依赖路径不可用"
                )
                if self._baseline_preflight_environment_failure(baseline_check):
                    self.db.execute(
                        "UPDATE pairs SET error=?,updated_at=? WHERE id=?",
                        (reason[-3000:], now_iso(), pair_id),
                    )
                    self.db.audit("task.baseline_preflight_environment_error", "pair", pair_id, {
                        "reason": reason, "checks": baseline_check.get("checks", []),
                        "action": "retry_without_starting_claude",
                    })
                    raise RuntimeError(reason)
                self._reject_pair_before_development(pair_id, reason, baseline_check)
                return self.pair_detail(pair_id)
            self.db.audit("task.baseline_preflight_passed", "pair", pair_id, {
                "task_id": task.get("id"), "compose_file": baseline_check.get("compose_file", ""),
            })
        # Also migrates pre-fix queued rows whose workspace pointed directly at
        # the non-empty canonical clone.
        for arm in ("A", "B"):
            self.claude.prepare_arm(pair, arm, Path(repo["local_root"]) / "workspaces" / arm)
        runs = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        if len(runs) != 2:
            raise RuntimeError("A/B Arm 不完整")
        started: List[Dict[str, Any]] = []
        try:
            for run in runs:
                self.claude.reset_unsent_arm(run)
            runs = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
            for run in runs:
                if self._launch_arm_if_capacity(run) is not True:
                    continue
                started.append(self.db.one(
                    "SELECT * FROM arm_runs WHERE id=?", (run["id"],),
                ) or run)
            if not started:
                self.db.audit("claude.pair_start_deferred_for_capacity", "pair", pair_id, {
                    "terminalLimit": self._development_arm_limit(),
                    "activeTerminals": self._active_development_arm_count(),
                })
                return self.pair_detail(pair_id)
            for run in started:
                self.claude.wait_until_ready(run)
            for run in started:
                self.claude.materialize_repository(
                    run, Path(repo["local_root"]) / run["arm"], pair["baseline_sha"]
                )
            # Mark development before the first prompt. Any unstarted side
            # remains queued and the scheduler fills it when a terminal slot
            # becomes available.
            self.db.execute(
                "UPDATE pairs SET status='running',stage='development',error='',started_at=?,updated_at=? WHERE id=?",
                (now_iso(), now_iso(), pair_id),
            )
            prompt = task["prompt"]
            for run in started:
                self._send_prompt_with_pair_stagger(pair_id, run, prompt)
            for run in started:
                self._submit_monitor("monitor-" + run["id"], self._monitor_arm, pair_id, run["id"], prompt)
            deferred = [run["arm"] for run in runs if run["id"] not in {item["id"] for item in started}]
            if deferred:
                self.db.audit("claude.pair_arm_deferred_for_capacity", "pair", pair_id, {
                    "arms": deferred,
                    "terminalLimit": self._development_arm_limit(),
                    "activeTerminals": self._active_development_arm_count(),
                    "automaticResume": True,
                })
            return self.pair_detail(pair_id)
        except Exception as exc:
            error = redact(str(exc))[-3000:]
            if self._is_prompt_delivery_error(error):
                stamp = now_iso()
                self.db.execute(
                    "UPDATE pairs SET status='running',stage='development',error=?,updated_at=? WHERE id=?",
                    (("题面投递基础设施重试中：" + error)[-3000:], stamp, pair_id),
                )
                started_ids = {run["id"] for run in started}
                for current in self.db.all(
                    "SELECT id,prompt_sent_at,status FROM arm_runs WHERE pair_id=?",
                    (pair_id,),
                ):
                    if (current["id"] in started_ids and not current.get("prompt_sent_at")
                            and current.get("status") in (
                                "running", "developing", "waiting_retry",
                            )):
                        self.db.execute(
                            "UPDATE arm_runs SET status='waiting_retry',error=?,updated_at=? WHERE id=?",
                            (error, stamp, current["id"]),
                        )
                for current in self.db.all("SELECT * FROM arm_runs WHERE pair_id=?", (pair_id,)):
                    if current.get("prompt_sent_at"):
                        self._submit_monitor(
                            "monitor-" + current["id"], self._monitor_arm,
                            pair_id, current["id"], task["prompt"],
                        )
                self._schedule_pending_arm_retries(pair_id)
                return self.pair_detail(pair_id)
            self.db.execute(
                "UPDATE pairs SET status='failed',error=?,updated_at=? WHERE id=?",
                (error, now_iso(), pair_id),
            )
            # Never destroy a successfully started arm here. Its terminal remains available for safe export/recovery.
            raise

    @staticmethod
    def _baseline_preflight_environment_failure(check: Dict[str, Any]) -> bool:
        text = str(check.get("error") or "") + "\n" + "\n".join(
            str(item.get("detail") or "") for item in check.get("checks", [])
            if not item.get("passed")
        )
        lowered = text.casefold()
        return any(marker in lowered for marker in (
            "cannot connect to the docker daemon", "is the docker daemon running",
            "docker desktop is not running", "command not found: docker",
            "no such file or directory: 'docker'", "context deadline exceeded",
        ))

    def _reject_pair_before_development(self, pair_id: str, reason: str,
                                        check: Dict[str, Any]) -> None:
        pair = self._pair(pair_id)
        stamp = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET status='rejected',rejection_reason=?,updated_at=? WHERE id=?",
                (reason[-2000:], stamp, pair["task_id"]),
            )
            conn.execute(
                """UPDATE pairs SET status='failed',stage='baseline_preflight_failed',
                   error=?,updated_at=? WHERE id=?""",
                (reason[-3000:], stamp, pair_id),
            )
            conn.execute(
                """UPDATE arm_runs SET status='failed',error=?,finished_at=?,updated_at=?
                   WHERE pair_id=? AND status='queued'""",
                (reason[-2000:], stamp, stamp, pair_id),
            )
            conn.execute(
                """UPDATE delivery_submissions SET status='discarded',error=?,updated_at=?
                   WHERE pair_id=?""",
                (reason[-2000:], stamp, pair_id),
            )
        self.db.audit("task.baseline_preflight_failed", "pair", pair_id, {
            "task_id": pair["task_id"], "reason": reason,
            "checks": check.get("checks", []), "claude_started": False,
        })
        self._submit("replace-task-" + pair_id, self._start_replacement_pair, pair_id)

    def generate_gsb_async(self, pair_id: str) -> str:
        operation = "gsb-" + pair_id
        self._submit(operation, self.generate_gsb, pair_id)
        return operation

    def generate_delivery_assessment_async(self, pair_id: str) -> str:
        operation = "delivery-assessment-" + pair_id
        self._submit(operation, self.generate_delivery_assessment, pair_id)
        return operation

    def reassess_actual_difficulty_async(self, pair_id: str) -> str:
        operation = "difficulty-" + pair_id
        self._submit(operation, self.reassess_actual_difficulty, pair_id)
        return operation

    def edit_pair_difficulty(self, pair_id: str, difficulty: str,
                             note: str = "") -> Dict[str, Any]:
        """Apply an operator correction while preserving automatic review evidence."""
        difficulty = str(difficulty or "").strip()
        note = str(note or "").strip()[:800]
        if difficulty not in ("中等", "困难", "地狱"):
            raise ValueError("难度只能选择中等、困难或地狱")
        with self._pair_failure_lock(pair_id):
            pair = self._require_local_pair_edit(pair_id)
            task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
            if not task_difficulty_allowed(str(task.get("task_type") or ""), difficulty):
                raise ValueError("所有类型的新提交题目只允许困难或地狱；实际中等不能改标放行")
            review = self.db.one(
                "SELECT * FROM difficulty_reviews WHERE pair_id=?", (pair_id,),
            ) or {}
            if review.get("status") == "running":
                raise ValueError("实际难度复评仍在运行，请等待结束后再人工调整")

            previous = str(task.get("difficulty") or "")
            previous_reason = str(review.get("reason") or review.get("error") or "").strip()
            reason_parts = ["人工将难度从%s调整为%s" % (previous or "未记录", difficulty)]
            if note:
                reason_parts.append("调整说明：" + note)
            if previous_reason:
                reason_parts.append("原自动复评：" + previous_reason)
            reason = "；".join(reason_parts)[:2000]
            stamp = now_iso()
            resume_rejected = bool(review and pair.get("stage") == "difficulty_rejected")
            next_stage = ""
            next_error = ""
            if resume_rejected:
                checks = self._require_artifact_results(pair_id)
                next_stage, next_error = self._post_difficulty_stage(pair_id, checks)

            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE tasks SET difficulty=?,updated_at=? WHERE id=?",
                    (difficulty, stamp, pair["task_id"]),
                )
                if review:
                    # Keep a_difficulty, b_difficulty, evidence_json and commit
                    # references untouched.  The manual correction is an
                    # explicit overlay, not a rewrite of the automatic facts.
                    conn.execute(
                        """UPDATE difficulty_reviews SET assessed_difficulty=?,reason=?,
                           status='passed',error='',reviewed_at=?,updated_at=? WHERE pair_id=?""",
                        (difficulty, reason, stamp, stamp, pair_id),
                    )
                if resume_rejected:
                    conn.execute(
                        "UPDATE pairs SET status='running',stage=?,error=?,updated_at=? WHERE id=?",
                        (next_stage, next_error, stamp, pair_id),
                    )
                    conn.execute(
                        """UPDATE delivery_submissions SET status='needs_review',error='',
                           hidden_at=NULL,payload_sha256='',updated_at=? WHERE pair_id=?""",
                        (stamp, pair_id),
                    )
                    if pair.get("chain_id"):
                        conn.execute(
                            """UPDATE project_chains SET status='active',followup_completed=0,
                               completed_at=NULL,updated_at=? WHERE id=?""",
                            (stamp, pair["chain_id"]),
                        )
                else:
                    conn.execute(
                        "UPDATE delivery_submissions SET payload_sha256='',updated_at=? WHERE pair_id=?",
                        (stamp, pair_id),
                    )
            self.db.audit("difficulty.manually_edited", "pair", pair_id, {
                "from": previous,
                "to": difficulty,
                "note": note,
                "automaticReviewPreserved": bool(review),
                "aDifficulty": review.get("a_difficulty") or "",
                "bDifficulty": review.get("b_difficulty") or "",
                "automaticReason": previous_reason,
                "resumedRejectedPair": resume_rejected,
            })
        return self.pair_detail(pair_id)

    def repair_trace_prompt_async(self, pair_id: str, arm: str) -> str:
        operation = "trace-repair-%s-%s" % (pair_id, arm.lower())
        self._submit(operation, self.repair_trace_prompt, pair_id, arm)
        return operation

    def repair_trace_prompt(self, pair_id: str, arm: str) -> Dict[str, Any]:
        if arm not in ("A", "B"):
            raise ValueError("arm must be A or B")
        pair = self._pair(pair_id)
        task = self.db.one("SELECT prompt FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        prompt = str(task.get("prompt") or "")
        pair_runs = self.db.all(
            "SELECT * FROM arm_runs WHERE pair_id=? AND status='completed' ORDER BY arm",
            (pair_id,),
        )
        if len(pair_runs) == 2:
            pair_prompts: Dict[str, str] = {}
            for item in pair_runs:
                trace, _, _ = self._inspect_trace(item, prompt)
                if trace:
                    pair_prompts[str(item["arm"])] = self._trace_first_user_prompt(trace)
            if (pair_prompts.get("A") and pair_prompts.get("B")
                    and not self._paired_trace_prompts_match(
                        prompt, pair_prompts["A"], pair_prompts["B"],
                    )):
                return self._restart_trace_invalid_arms(
                    pair_id, pair_runs, prompt,
                    ["A/B 轨迹里的完整首轮 User Prompt 不一致"],
                )
        run = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm=?", (pair_id, arm))
        if not run or run.get("status") != "completed":
            raise ValueError("只有已完成且轨迹不合格的 Arm 才能按原题面重跑")
        _, _, issues = self._inspect_trace(run, prompt)
        if not any("首轮 User Prompt" in issue for issue in issues):
            raise ValueError("该 Arm 没有首轮题面逐字不一致问题")
        return self._restart_trace_invalid_arms(
            pair_id, [run], prompt, issues,
        )

    @staticmethod
    def _bug_discovery_exhausted_sql(alias: str = "e", source_sha_sql: str = "",
                                     allow_legacy: bool = True) -> str:
        """Return the predicate for a source Arm whose Bug search is exhausted.

        A discovery that produced a usable candidate must not retire the whole
        project: the same fixed product can contain several independent Bugs.
        Older audit rows did not persist ``exhausted``; for those rows an empty
        candidate list remains the backwards-compatible exhaustion signal.
        """
        source_match = ""
        latest_source_match = ""
        if source_sha_sql:
            source_match = " AND (json_extract({a}.detail_json,'$.sourceSha')={sha}{legacy})".format(
                a=alias, sha=source_sha_sql,
                legacy=(" OR json_type({a}.detail_json,'$.sourceSha') IS NULL".format(a=alias)
                        if allow_legacy else ""),
            )
            latest_source_match = " AND (json_extract(latest.detail_json,'$.sourceSha')={sha}{legacy})".format(
                sha=source_sha_sql,
                legacy=(" OR json_type(latest.detail_json,'$.sourceSha') IS NULL"
                        if allow_legacy else ""),
            )
        return """(
            json_extract(%s.detail_json,'$.exhausted')=1
            OR (
                json_type(%s.detail_json,'$.exhausted') IS NULL
                AND COALESCE(json_array_length(
                    json_extract(%s.detail_json,'$.candidateIds')
                ),0)=0
            )
        ){source_match} AND {a}.id=(
            SELECT MAX(latest.id) FROM audit_events latest
             WHERE latest.event_type='bug.discovery_completed'
               AND latest.entity_id={a}.entity_id
               AND json_extract(latest.detail_json,'$.arm')=json_extract({a}.detail_json,'$.arm')
               {latest_source_match}
        )""".format(a=alias, source_match=source_match,
                     latest_source_match=latest_source_match) % (alias, alias, alias)

    def _bug_source_arm_exhausted(self, pair_id: str, arm: str, source_sha: str = "") -> bool:
        if source_sha:
            row = self.db.one(
                """SELECT detail_json FROM audit_events
                     WHERE event_type='bug.discovery_completed' AND entity_type='pair'
                       AND entity_id=? AND json_extract(detail_json,'$.arm')=?
                       AND json_extract(detail_json,'$.sourceSha')=?
                     ORDER BY id DESC LIMIT 1""", (pair_id, arm, source_sha),
            )
            if not row:
                return False
            detail = json.loads(row["detail_json"])
            return bool(detail.get("exhausted", not detail.get("candidateIds")))
        predicate = self._bug_discovery_exhausted_sql("e")
        return bool(self.db.one(
            """SELECT 1 matched FROM audit_events e
                 WHERE e.event_type='bug.discovery_completed'
                   AND e.entity_type='pair' AND e.entity_id=?
                   AND json_extract(e.detail_json,'$.arm')=?
                   AND %s LIMIT 1""" % predicate,
            (pair_id, arm),
        ))

    def _bug_source_arm_scanned(self, pair_id: str, arm: str) -> bool:
        """Compatibility alias: a source counts as scanned only when exhausted."""
        return self._bug_source_arm_exhausted(pair_id, arm)

    def _bug_source_candidate_rows(self, pair_id: str, arm: str,
                                   source_sha: str) -> List[Dict[str, Any]]:
        return self.db.all(
            """WITH RECURSIVE lineage(pair_id, task_id, depth) AS (
                   SELECT p.id,p.task_id,0 FROM pairs p WHERE p.id=?
                   UNION ALL
                   SELECT source.id,source.task_id,lineage.depth+1
                     FROM lineage JOIN tasks t ON t.id=lineage.task_id
                     JOIN bug_candidates origin ON origin.id=t.source_id
                     JOIN pairs source ON source.id=origin.source_pair_id
                    WHERE lineage.depth<12
                 )
                 SELECT id,title,actual_result,expected_result,source_paths_json,status,created_at
                   FROM bug_candidates
                  WHERE source_pair_id=?
                     OR source_pair_id IN (SELECT pair_id FROM lineage WHERE depth>0)
                  ORDER BY created_at,id""",
            (pair_id, pair_id),
        )

    @classmethod
    def _bug_cost_input_rounding_signature(cls, body: str, paths: set) -> bool:
        """Recognize the same text-cost precision loss across changed examples/Arms."""
        if not any(path.endswith("/draft.ts") for path in paths):
            return False
        normalized = cls._normalized_task_text(body)
        return (
            "parsedraft" in normalized and "number" in normalized
            and any(word in normalized for word in ("成本", "代价"))
            and any(word in normalized for word in ("精度", "尾数", "尾差", "小数", "十进制"))
            and any(word in normalized for word in ("更贵", "较贵", "较高", "更高成本"))
            and any(word in normalized for word in ("稳定", "序号"))
        )

    @classmethod
    def _bug_triple_line_as_points_signature(
        cls, title: str, actual: str, expected: str,
    ) -> bool:
        """Identify one zero-area triple-contact defect across different Arms.

        The implementations and source paths can differ, but reporting a
        continuous triple-covered line as only its endpoint points is the same
        observable failure and repair objective.
        """
        body = cls._normalized_task_text(" ".join((title, actual, expected)))
        observed = cls._normalized_task_text(actual)
        desired = cls._normalized_task_text(expected)
        return (
            any(word in body for word in ("三重", "triple", "multiplicity3"))
            and any(word in body for word in ("零面积", "面积为0", "triplearea0"))
            and any(word in observed for word in ("两个端点", "两个点", "pointtriples", "point风险", "点风险"))
            and any(word in desired for word in ("连续线段", "整段", "线段风险", "tripleboundary"))
        )

    def _bug_source_candidate_context(self, rows: List[Dict[str, Any]]) -> str:
        compact = []
        for row in rows:
            try:
                paths = json.loads(str(row.get("source_paths_json") or "[]"))
            except (TypeError, ValueError):
                paths = []
            compact.append({
                "id": row.get("id"),
                "title": row.get("title"),
                "observableFailure": str(row.get("actual_result") or "")[:500],
                "correctBehavior": str(row.get("expected_result") or "")[:500],
                "sourcePaths": [str(path)[:240] for path in paths[:8]],
                "status": row.get("status"),
            })
        return json.dumps(compact, ensure_ascii=False, indent=2)

    def _bug_source_candidate_duplicate(
        self, candidate: Dict[str, Any], rows: List[Dict[str, Any]],
    ) -> str:
        """Reject the same defect rediscovered while allowing sibling Bugs."""
        title = str(candidate.get("title") or "")
        body = "\n".join((
            title,
            str(candidate.get("actual") or ""),
            str(candidate.get("expected") or ""),
        ))
        candidate_paths = {
            str(path).split(":", 1)[0]
            for path in (candidate.get("sourcePaths") or []) if str(path)
        }
        for row in rows:
            other_title = str(row.get("title") or "")
            if (self._normalized_task_text(title)
                    and self._normalized_task_text(title) == self._normalized_task_text(other_title)):
                return "与该产物已有候选标题相同：%s" % other_title
            other = "\n".join((
                other_title,
                str(row.get("actual_result") or ""),
                str(row.get("expected_result") or ""),
            ))
            if (
                self._bug_triple_line_as_points_signature(
                    title, str(candidate.get("actual") or ""), str(candidate.get("expected") or ""),
                )
                and self._bug_triple_line_as_points_signature(
                    other_title, str(row.get("actual_result") or ""),
                    str(row.get("expected_result") or ""),
                )
            ):
                return "与已有候选同为零面积三重覆盖线误报成端点：%s" % other_title
            try:
                other_paths = {
                    str(path).split(":", 1)[0]
                    for path in json.loads(str(row.get("source_paths_json") or "[]"))
                    if str(path)
                }
            except (TypeError, ValueError):
                other_paths = set()
            if (self._bug_cost_input_rounding_signature(body, candidate_paths)
                    and self._bug_cost_input_rounding_signature(other, other_paths)):
                return "与已有候选同为文本成本经 Number 转换丢失十进制尾差：%s" % other_title
            similarity = self._task_text_similarity(body, other)
            if similarity >= 0.86 or (
                similarity >= 0.72 and candidate_paths and other_paths
                and bool(candidate_paths & other_paths)
            ):
                return "与该产物已有候选属于同一缺陷：%s" % other_title
        return ""

    def _failed_bug_sources(self, pair_id: str = "", arm: str = "",
                            source_sha: str = "") -> List[Dict[str, Any]]:
        # The artifact check, not the mutable current Arm pointer, owns the
        # source identity. A successful peer must never hide this failed side.
        return self.db.all(
            """SELECT a.workspace_path,a.status,c.pair_id,c.arm,c.commit_sha,
                      c.compose_file,c.status check_status,c.error check_error,c.checks_json
                 FROM artifact_checks c JOIN pairs p ON p.id=c.pair_id
                 LEFT JOIN arm_runs a ON a.pair_id=c.pair_id AND a.arm=c.arm
                WHERE c.status IN ('failed','observed_failed') AND length(c.commit_sha)=40
                  AND p.status NOT IN ('queued','running','review','waiting_api_retry')
                  AND (?='' OR c.pair_id=?) AND (?='' OR c.arm=?)
                  AND (?='' OR c.commit_sha=?)
                  AND NOT EXISTS (
                    SELECT 1 FROM audit_events e WHERE e.entity_type='pair'
                     AND e.entity_id=c.pair_id
                     AND e.event_type IN ('bug.source_repair_completed','bug.source_repair_failed',
                                          'bug.source_repair_skipped_browser')
                     AND json_extract(e.detail_json,'$.sourceArm')=c.arm
                     AND json_extract(e.detail_json,'$.sourceSha')=c.commit_sha
                  )
                ORDER BY CASE a.status WHEN 'completed' THEN 0 ELSE 1 END,c.updated_at,c.pair_id,c.arm""",
            (pair_id, pair_id, arm, arm, source_sha, source_sha),
        )

    def _schedule_priority_bug_sources(self) -> bool:
        if not self._strict_bug_admission() or not self.db.setting("manual_bug_allow_failed_source_repair", True):
            return False
        queue = self.db.setting("manual_bug_source_priority", [])
        if not isinstance(queue, list):
            return False
        for entry in queue:
            if not isinstance(entry, dict):
                continue
            pair_id, arm, sha = (str(entry.get(key) or "") for key in ("pairId", "arm", "sourceSha"))
            if arm not in ("A", "B") or not re.fullmatch(r"[0-9a-f]{40}", sha):
                continue
            pair = self.db.one("SELECT status FROM pairs WHERE id=?", (pair_id,))
            if not pair or pair["status"] in ("queued", "running", "review", "waiting_api_retry"):
                continue
            repaired = self._repaired_bug_source(pair_id, source_arm=arm, original_sha=sha,
                                                  unscanned_only=True)
            if repaired:
                operation = "bugs-" + pair_id
                if operation_ready(self.db, operation):
                    return self._submit_auto(operation, self.discover_bugs, pair_id,
                                             arm, repaired["commit_sha"])
            elif self._failed_bug_sources(pair_id, arm, sha):
                operation = "bug-source-repair-" + pair_id
                if operation_ready(self.db, operation):
                    return self._submit_auto(operation, self.repair_bug_source, pair_id, arm, sha)
        return False

    @staticmethod
    def _resolve_failed_bug_workspace(source: Dict[str, Any]) -> Path:
        sha = str(source.get("commit_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("失败产物缺少准确提交")
        paths = [source.get("workspace_path")]
        if source.get("compose_file"):
            paths.append(str(Path(source["compose_file"]).parent))
        for value in paths:
            if not value:
                continue
            path = Path(value).resolve()
            if not path.is_dir():
                continue
            root = run_command(["git", "rev-parse", "--show-toplevel"], cwd=path, check=False, timeout=30)
            if root.returncode or Path(root.stdout.strip()).resolve() != path:
                continue
            exists = run_command(["git", "cat-file", "-e", sha + "^{commit}"],
                                 cwd=path, check=False, timeout=30)
            if exists.returncode == 0:
                return path
        raise ValueError("失败产物记录的准确提交已无法在当前或历史工作区找到，未使用未提交文件")

    def _repaired_bug_source(
        self, pair_id: str, source_sha: str = "", unscanned_only: bool = False,
        source_arm: str = "", original_sha: str = "",
    ) -> Optional[Dict[str, Any]]:
        rows = self.db.all(
            """SELECT detail_json FROM audit_events
                 WHERE event_type='bug.source_repair_completed'
                   AND entity_type='pair' AND entity_id=?
                 ORDER BY id DESC""",
            (pair_id,),
        )
        for row in rows:
            try:
                detail = json.loads(str(row.get("detail_json") or "{}"))
            except (TypeError, ValueError):
                continue
            baseline_sha = str(detail.get("baselineSha") or "")
            workspace = Path(str(detail.get("workspacePath") or ""))
            repaired_arm = str(detail.get("sourceArm") or "A")
            if source_arm and repaired_arm != source_arm:
                continue
            if original_sha and detail.get("sourceSha") != original_sha:
                continue
            if source_sha and baseline_sha != source_sha:
                continue
            if unscanned_only and self._bug_source_arm_exhausted(pair_id, repaired_arm, baseline_sha):
                continue
            if (workspace.is_dir() and re.fullmatch(r"[0-9a-f]{40}", baseline_sha)
                    and (detail.get("validation") or {}).get("status") == "passed"):
                return {
                    "arm": repaired_arm,
                    "workspace_path": str(workspace),
                    "commit_sha": baseline_sha,
                    "repairEvidence": detail.get("validation") or {},
                }
        return None

    def _bug_candidate_source_arm(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        arm = self.db.one(
            "SELECT * FROM arm_runs WHERE pair_id=? AND arm=? AND commit_sha=?",
            (candidate["source_pair_id"], candidate["source_arm"], candidate["source_sha"]),
        )
        if arm:
            return arm
        repaired = self._repaired_bug_source(
            str(candidate.get("source_pair_id") or ""), str(candidate.get("source_sha") or ""),
        )
        if repaired:
            return repaired
        raise ValueError("找不到候选对应的固定提交工作区")

    def repair_bug_source(self, pair_id: str, source_arm: str = "", source_sha: str = "") -> Dict[str, Any]:
        """Repair a failed artifact only in an isolated copy, then admit it as a source."""
        if not bool(self.db.setting("manual_bug_allow_failed_source_repair", True)):
            raise ValueError("未启用验收失败产物的隔离副本修复")
        pair = self._pair(pair_id)
        if pair["status"] in ("queued", "running", "review", "waiting_api_retry"):
            raise ValueError("运行中的 Pair 不能作为修复来源")
        rows = self._failed_bug_sources(pair_id, source_arm, source_sha)
        if not rows:
            raise ValueError("该 Pair 没有可修复的失败产物提交")
        source = rows[0]
        source_arm = str(source.get("arm") or "A")
        try:
            source["workspace_path"] = str(self._resolve_failed_bug_workspace(source))
            workspace, _ = self._create_isolated_bug_baseline(
                {"source_sha": source["commit_sha"]}, source,
                "source-repair-%s-%s-%s" % (pair_id[-16:], source_arm.lower(), source["commit_sha"][:12]),
            )
        except Exception as exc:
            self.db.audit("bug.source_repair_failed", "pair", pair_id, {
                "sourceArm": source_arm, "sourceSha": source["commit_sha"],
                "error": redact(str(exc))[-2000:],
            })
            raise
        browser_files = self._bug_source_browser_automation_files(workspace)
        if browser_files:
            summary = "失败产物的现有验收链路依赖浏览器自动化，已在 Docker 修复验收前跳过"
            self.db.audit("bug.discovery_completed", "pair", pair_id, {
                "arm": source_arm, "searchSummary": summary, "candidateIds": [],
                "browserRejected": browser_files, "sourceSha": source["commit_sha"],
            })
            self.db.audit("bug.source_repair_skipped_browser", "pair", pair_id, {
                "sourceArm": source_arm, "sourceSha": source["commit_sha"],
                "files": browser_files,
            })
            return {
                "sourceArm": source_arm, "sourceSha": source["commit_sha"],
                "skipped": True, "reason": summary, "browserRejected": browser_files,
            }
        before = self.artifacts.preflight(workspace, "repair-before-%s" % pair_id[-12:])
        try:
            if before.get("status") != "passed":
                repair = self.codex.run(
                    "bug_source_repair",
                    "你在一个与原产物完全隔离的代码副本中工作。先阅读现有实现和 Docker 验收失败证据，"
                    "只修复让项目无法启动或业务冒烟失败的问题，使现有 Docker Compose、健康检查和一次性 verify 正常通过。"
                    "不得新增或修改测试、复现脚本、说明文件，不得删减业务功能，也不要设计新的 Bug。"
                    "仅使用代码、构建、API/HTTP 或业务模块调用进行验证。"
                    "完成后只按 Schema 总结实际修改的源码文件。\n\n失败证据：\n%s"
                    % json.dumps({
                        "error": source.get("check_error") or "",
                        "checks": json.loads(str(source.get("checks_json") or "[]")),
                        "isolatedPreflight": before,
                    }, ensure_ascii=False, indent=2),
                    BUG_SOURCE_REPAIR_SCHEMA,
                    cwd=workspace, pair_id=pair_id, timeout=5400,
                    sandbox="workspace-write",
                )
                changed = [
                    line[3:].strip() for line in run_command(
                        ["git", "status", "--porcelain"], cwd=workspace, timeout=30,
                    ).stdout.splitlines() if len(line) > 3
                ]
                forbidden = [
                    path for path in changed
                    if re.search(r"(^|/)(tests?|specs?)(/|$)|(^|/)(readme|docs?)(\.|/|$)|repro|playwright|selenium|cypress",
                                 path, re.IGNORECASE)
                ]
                if forbidden:
                    raise ValueError("来源副本修复修改了禁止文件：%s" % "、".join(forbidden))
                if not changed:
                    raise ValueError("来源副本验收失败，但修复过程没有产生源码改动")
            else:
                repair = {"summary": "隔离副本复验已通过，无需修改源码", "changedFiles": []}
            after = self.artifacts.preflight(workspace, "repair-after-%s" % pair_id[-12:])
            if after.get("status") != "passed":
                raise ValueError("隔离副本修复后仍未通过 Docker 验收：%s" % (after.get("error") or "未知错误"))
            if run_command(["git", "status", "--porcelain"], cwd=workspace, timeout=30).stdout.strip():
                run_command(["git", "add", "-A"], cwd=workspace, timeout=60)
                run_command(["git", "commit", "-m", "Repair source product for Bug discovery"], cwd=workspace, timeout=120)
            baseline_sha = run_command(["git", "rev-parse", "HEAD"], cwd=workspace, timeout=30).stdout.strip()
            detail = {
                "sourceArm": source_arm,
                "sourceSha": source["commit_sha"],
                "workspacePath": str(workspace),
                "baselineSha": baseline_sha,
                "repairSummary": repair.get("summary") or "",
                "changedFiles": repair.get("changedFiles") or [],
                "validation": after,
            }
            self.db.audit("bug.source_repair_completed", "pair", pair_id, detail)
            return detail
        except Exception as exc:
            self.db.audit("bug.source_repair_failed", "pair", pair_id, {
                "sourceArm": source_arm, "sourceSha": source["commit_sha"],
                "workspacePath": str(workspace), "error": redact(str(exc))[-2000:],
            })
            raise

    def discover_bugs_async(self, pair_id: str) -> str:
        operation = "bugs-" + pair_id
        self._submit(operation, self.discover_bugs, pair_id)
        return operation

    def discover_bugs(self, pair_id: str, source_arm: str = "", source_sha: str = "") -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if pair["status"] in ("queued", "running", "review", "waiting_api_retry"):
            raise ValueError("仍在运行的 Pair 不能进入后续 Bug 搜索")
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        preferred = "B" if pair["winner"] == "B better" else "A"
        exhausted = self._bug_discovery_exhausted_sql("e", "a.commit_sha")
        sources = self.db.all(
            """SELECT a.*,c.id check_id,c.checks_json,c.error check_error
                 FROM arm_runs a JOIN artifact_checks c
                   ON c.pair_id=a.pair_id AND c.arm=a.arm
                  AND c.commit_sha=a.commit_sha AND c.status='passed'
                WHERE a.pair_id=? AND a.status='completed' AND a.commit_sha<>''
                  AND NOT EXISTS (
                    SELECT 1 FROM audit_events e
                     WHERE e.event_type='bug.discovery_completed'
                       AND e.entity_type='pair' AND e.entity_id=a.pair_id
                       AND json_extract(e.detail_json,'$.arm')=a.arm
                       AND %s
                  )
                ORDER BY CASE WHEN a.arm=? THEN 0 ELSE 1 END,a.arm""" % exhausted,
            (pair_id, preferred),
        )
        arm = next(iter(sources), None)
        if source_arm or source_sha:
            arm = next((item for item in sources if item["arm"] == source_arm
                        and item["commit_sha"] == source_sha), None)
        if arm:
            selected = str(arm.get("arm") or "")
            check = self.db.one("SELECT * FROM artifact_checks WHERE id=?", (arm.get("check_id"),))
        else:
            arm = self._repaired_bug_source(pair_id, source_sha, unscanned_only=True,
                                             source_arm=source_arm)
            selected = str(arm.get("arm") or preferred) if arm else preferred
            check = {
                "status": "passed",
                "checks_json": json.dumps(arm.get("repairEvidence") or {}, ensure_ascii=False),
                "error": "",
            } if arm else None
        if not arm or not check:
            raise ValueError("该 Pair 没有可用于找 Bug 的已通过产物；需先在隔离副本修复验收")
        source_commit = str(arm.get("commit_sha") or "")
        workspace = Path(str(arm.get("workspace_path") or ""))
        if not workspace.is_dir() or not re.fullmatch(r"[0-9a-f]{40}", source_commit):
            raise ValueError("找 Bug 来源工作区或准确提交不可用")
        # A commit already searched to exhaustion has identical code even if
        # it is referenced by another Pair or the peer Arm. Do not spend a
        # second Codex run on that unchanged snapshot; an earlier productive
        # scan remains eligible to find further independent Bugs.
        prior_scan = self.db.one(
            """SELECT entity_id,detail_json FROM audit_events
                 WHERE event_type='bug.discovery_completed'
                   AND json_extract(detail_json,'$.sourceSha')=?
                 ORDER BY id DESC LIMIT 1""",
            (source_commit,),
        )
        if prior_scan:
            prior_detail = json.loads(str(prior_scan.get("detail_json") or "{}"))
            if prior_detail.get("exhausted", not prior_detail.get("candidateIds")):
                summary = "相同提交的 Bug 搜索已完成，跳过重复分析"
                self.db.audit("bug.discovery_completed", "pair", pair_id, {
                    "arm": selected, "sourceSha": source_commit,
                    "searchSummary": summary, "candidateIds": [], "exhausted": True,
                    "duplicateSourcePairId": prior_scan.get("entity_id") or "",
                })
                return {"pairId": pair_id, "arm": selected, "searchSummary": summary,
                        "candidateIds": [], "exhausted": True}
        browser_files = self._bug_source_browser_automation_files(Path(str(arm["workspace_path"])))
        if browser_files:
            summary = "来源产物的现有验收链路依赖浏览器自动化，不适合作为当前 Bug-only 基线"
            self.db.audit("bug.discovery_completed", "pair", pair_id, {
                "arm": selected, "searchSummary": summary, "candidateIds": [],
                "sourceSha": arm.get("commit_sha", ""),
                "browserRejected": browser_files, "exhausted": True,
            })
            return {
                "pairId": pair_id, "arm": selected, "searchSummary": summary,
                "candidateIds": [], "browserRejected": browser_files,
            }
        existing_rows = self._bug_source_candidate_rows(
            pair_id, selected, str(arm.get("commit_sha") or ""),
        )
        result = self.codex.run(
            "bug_discovery",
            bug_discovery_prompt(
                task.get("prompt", ""), selected, arm.get("commit_sha", ""),
                json.dumps(check, ensure_ascii=False),
                hard_only=self._strict_bug_admission(),
                existing_candidates=self._bug_source_candidate_context(existing_rows),
            ),
            BUG_DISCOVERY_SCHEMA,
            cwd=Path(arm["workspace_path"]), pair_id=pair_id, task_id=pair["task_id"], timeout=2400,
        )
        created = []
        eligible_created = []
        browser_rejected = []
        duplicate_rejected = []
        for candidate in result["candidates"]:
            if contains_browser_verification(candidate.get("reproductionCommands") or []):
                browser_rejected.append(str(candidate.get("title") or "未命名候选"))
                continue
            duplicate = self._bug_source_candidate_duplicate(candidate, existing_rows)
            if duplicate:
                duplicate_rejected.append({
                    "title": str(candidate.get("title") or "未命名候选"),
                    "reason": duplicate,
                })
                continue
            candidate_id = "bug-" + uuid.uuid4().hex[:16]
            difficulty = candidate["difficulty"]
            admission_issues = (
                self._manual_bug_candidate_issues(candidate)
                if self._strict_bug_admission() else []
            )
            if not task_difficulty_allowed("bugfix", difficulty) and not admission_issues:
                admission_issues.append("Bug 候选难度低于困难")
            status = (
                "awaiting_reproduction"
                if task_difficulty_allowed("bugfix", difficulty) and not admission_issues
                else "difficulty_rejected"
            )
            source_lines_min, source_lines_max = sorted((
                int(candidate["estimatedSourceLinesMin"]),
                int(candidate["estimatedSourceLinesMax"]),
            ))
            minutes_min, minutes_max = sorted((
                int(candidate["estimatedMinutesMin"]),
                int(candidate["estimatedMinutesMax"]),
            ))
            stamp = now_iso()
            self.db.execute(
                """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,preconditions,
                   reproduction_steps_json,reproduction_commands_json,actual_result,expected_result,difficulty,
                   difficulty_evidence_json,source_paths_json,estimated_module_count,
                   estimated_source_lines_min,estimated_source_lines_max,estimated_minutes_min,
                   estimated_minutes_max,complexity_axes_json,status,error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id, pair_id, selected, arm["commit_sha"], candidate["title"], candidate["preconditions"],
                 json.dumps(candidate["steps"], ensure_ascii=False), json.dumps(candidate["reproductionCommands"], ensure_ascii=False),
                 candidate["actual"], candidate["expected"],
                 difficulty, json.dumps(candidate["difficultyEvidence"], ensure_ascii=False),
                 json.dumps(candidate["sourcePaths"], ensure_ascii=False), int(candidate["estimatedModuleCount"]),
                 source_lines_min, source_lines_max, minutes_min, minutes_max,
                 json.dumps(candidate["complexityAxes"], ensure_ascii=False), status,
                 "；".join(admission_issues), stamp, stamp),
            )
            created.append(candidate_id)
            self.db.execute("UPDATE bug_candidates SET repair_verification_json=? WHERE id=?", (
                json.dumps(candidate.get("repairVerificationCommands") or [], ensure_ascii=False), candidate_id,
            ))
            existing_rows.append({
                "id": candidate_id,
                "title": candidate.get("title"),
                "actual_result": candidate.get("actual"),
                "expected_result": candidate.get("expected"),
                "source_paths_json": json.dumps(candidate.get("sourcePaths") or [], ensure_ascii=False),
                "status": status,
            })
            if status == "awaiting_reproduction":
                eligible_created.append(candidate_id)
        exhausted_source = not eligible_created
        self.db.audit("bug.discovery_completed", "pair", pair_id, {
            "arm": selected, "searchSummary": result["searchSummary"], "candidateIds": created,
            "sourceSha": arm.get("commit_sha", ""),
            "eligibleCandidateIds": eligible_created,
            "browserRejected": browser_rejected,
            "duplicateRejected": duplicate_rejected,
            "exhausted": exhausted_source,
        })
        return {
            "pairId": pair_id, "arm": selected, "searchSummary": result["searchSummary"],
            "candidateIds": created, "eligibleCandidateIds": eligible_created,
            "browserRejected": browser_rejected,
            "duplicateRejected": duplicate_rejected,
            "exhausted": exhausted_source,
        }

    def reproduce_bug_async(self, candidate_id: str) -> str:
        operation = "bug-reproduce-" + candidate_id
        self._submit_auto(operation, self.reproduce_bug, candidate_id)
        return operation

    def reproduce_bug(self, candidate_id: str) -> Dict[str, Any]:
        return self._reproduce_bug_snapshot(candidate_id)

    def _reproduce_bug_snapshot(self, candidate_id: str) -> Dict[str, Any]:
        candidate = self.db.one("SELECT * FROM bug_candidates WHERE id=?", (candidate_id,))
        if not candidate:
            raise KeyError("Bug 候选不存在")
        if candidate["status"] not in ("awaiting_reproduction", "reproduction_failed", "not_reproduced"):
            return candidate
        issues = self._manual_bug_candidate_issues(candidate) if self._strict_bug_admission() else []
        if not task_difficulty_allowed("bugfix", str(candidate.get("difficulty") or "")):
            issues.append("Bug 候选难度低于困难")
        if issues:
            self.db.execute("UPDATE bug_candidates SET status='difficulty_rejected',error=?,updated_at=? WHERE id=?",
                            ("；".join(issues), now_iso(), candidate_id))
            return self.db.one("SELECT * FROM bug_candidates WHERE id=?", (candidate_id,))
        arm = self._bug_candidate_source_arm(candidate)
        commands = json.loads(candidate["reproduction_commands_json"] or "[]")
        repair = json.loads(candidate.get("repair_verification_json") or "[]")
        try:
            validate_specs(commands)
            if repair:
                validate_specs(repair, repair=True)
                if not {"original", "boundary", "regression"} <= {spec.get("scenario") for spec in repair}:
                    raise ValueError("修复验证必须覆盖原始缺陷、邻近边界和正常回归")
            if contains_browser_verification(commands + repair):
                raise ValueError("私有业务验证不能依赖浏览器自动化")
        except (ValueError, TypeError) as exc:
            reason = "私有复现命令不合规：" + redact(str(exc))[-1800:]
            self.db.execute(
                "UPDATE bug_candidates SET status='rejected',error=?,updated_at=? WHERE id=?",
                (reason, now_iso(), candidate_id),
            )
            self.db.audit("bug.invalid_verification_rejected", "bug_candidate", candidate_id, {
                "reason": reason,
            })
            return self.db.one("SELECT * FROM bug_candidates WHERE id=?", (candidate_id,))
        with self.db.transaction() as conn:
            changed = conn.execute("UPDATE bug_candidates SET status='reproducing',updated_at=? WHERE id=? "
                                   "AND status IN ('awaiting_reproduction','reproduction_failed','not_reproduced')",
                                   (now_iso(), candidate_id)).rowcount
        if not changed:
            return self.db.one("SELECT * FROM bug_candidates WHERE id=?", (candidate_id,))
        attempts = []
        try:
            for attempt in (1, 2):
                result = clean_commands(Path(arm["workspace_path"]), candidate["source_sha"],
                                        "bugrep-%s-%d-%s" % (candidate_id[-8:], attempt, uuid.uuid4().hex[:6]), commands, db=self.db)
                result["attempt"] = attempt
                attempts.append(result)
            if repair:
                original = [spec for spec in repair if spec.get("scenario") == "original"]
                if not original:
                    raise ValueError("修复验证缺少原始缺陷场景")
                reference = clean_commands(Path(arm["workspace_path"]), candidate["source_sha"],
                                           "bugoracle-" + uuid.uuid4().hex[:12], original, repair=True, db=self.db)
                attempts[0]["repairOracleBaseline"] = reference
                if not all(row["businessFailed"] for row in reference["commands"]):
                    reason = "私有修复判据未能在缺陷基线确认业务失败，禁止准入"
                    self.db.execute(
                        "UPDATE bug_candidates SET status='rejected',error=?,reproduction_results_json=?,updated_at=? WHERE id=?",
                        (reason, json.dumps(attempts, ensure_ascii=False), now_iso(), candidate_id),
                    )
                    self.db.audit("bug.invalid_repair_oracle_rejected", "bug_candidate", candidate_id, {
                        "reason": reason, "sourceSha": candidate["source_sha"],
                    })
                    return self.db.one("SELECT * FROM bug_candidates WHERE id=?", (candidate_id,))
            passed = len(attempts) == 2 and all(item["passed"] for item in attempts)
            status = "reproduced" if passed else "not_reproduced"
            self.db.execute("UPDATE bug_candidates SET status=?,reproduce_count=?,reproduction_results_json=?,error='',updated_at=? WHERE id=?",
                            (status, sum(item["passed"] for item in attempts), json.dumps(attempts, ensure_ascii=False), now_iso(), candidate_id))
            self.db.audit("bug.reproduction_finished", "bug_candidate", candidate_id, {"status": status, "attempts": attempts})
        except Exception as exc:
            self.db.execute("UPDATE bug_candidates SET status='reproduction_failed',error=?,reproduction_results_json=?,updated_at=? WHERE id=?",
                            (redact(str(exc))[-2000:], json.dumps(attempts, ensure_ascii=False), now_iso(), candidate_id))
            raise
        return self.db.one("SELECT * FROM bug_candidates WHERE id=?", (candidate_id,))

    def _create_isolated_bug_baseline(self, candidate: Dict[str, Any],
                                      arm: Dict[str, Any], task_id: str) -> Tuple[Path, str]:
        """Copy the exact source commit into a new one-commit Bug repository."""
        source = Path(str(arm.get("workspace_path") or "")).resolve()
        source_sha = str(candidate.get("source_sha") or "")
        if not source.is_dir() or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise ValueError("Bug 来源缺少可核对的固定提交")
        exists = run_command(
            ["git", "-C", str(source), "cat-file", "-e", "%s^{commit}" % source_sha],
            check=False, timeout=30,
        )
        if exists.returncode != 0:
            raise ValueError("Bug 来源目录中找不到记录的固定提交")
        source_tree = run_command(["git", "rev-parse", source_sha + "^{tree}"], cwd=source, timeout=30).stdout.strip()

        root = (self.config.data_dir / "manual-bug-baselines").resolve()
        root.mkdir(parents=True, exist_ok=True)
        target = (root / task_id).resolve()
        if target.parent != root:
            raise ValueError("Bug 隔离基线路径越界")
        if target.exists():
            head = run_command(
                ["git", "-C", str(target), "rev-parse", "HEAD"], check=False, timeout=30,
            )
            count = run_command(
                ["git", "-C", str(target), "rev-list", "--count", "HEAD"], check=False, timeout=30,
            )
            clean = run_command(
                ["git", "-C", str(target), "status", "--porcelain"], check=False, timeout=30,
            )
            if (head.returncode == 0 and count.stdout.strip() == "1"
                    and clean.returncode == 0 and not clean.stdout.strip()):
                tree = run_command(["git", "rev-parse", "HEAD^{tree}"], cwd=target, timeout=30).stdout.strip()
                if tree == source_tree:
                    return target, head.stdout.strip()
            raise ValueError("已有隔离基线与来源提交不一致，已保留现场，禁止覆盖")

        building = (root / (".%s-building-%s" % (task_id, uuid.uuid4().hex[:8]))).resolve()
        if building.parent != root:
            raise ValueError("Bug 隔离基线临时路径越界")
        try:
            run_command(["git", "clone", "--no-hardlinks", str(source), str(building)], timeout=180)
            run_command(["git", "checkout", "--detach", source_sha], cwd=building, timeout=60)
            shutil.rmtree(building / ".git")
            run_command(["git", "init", "-b", "main"], cwd=building, timeout=30)
            author_name = str(self.db.setting("git_author_name", self.config.git_author_name) or "A/B Console")
            author_email = str(self.db.setting("git_author_email", self.config.git_author_email) or "ab-console@localhost")
            run_command(["git", "config", "user.name", author_name], cwd=building, timeout=30)
            run_command(["git", "config", "user.email", author_email], cwd=building, timeout=30)
            run_command(["git", "add", "--force", "-A"], cwd=building, timeout=60)
            run_command(
                ["git", "commit", "--allow-empty", "-m", "Initialize isolated Bug baseline"],
                cwd=building, timeout=120,
            )
            baseline_sha = run_command(["git", "rev-parse", "HEAD"], cwd=building, timeout=30).stdout.strip()
            baseline_tree = run_command(["git", "rev-parse", "HEAD^{tree}"], cwd=building, timeout=30).stdout.strip()
            if baseline_tree != source_tree:
                raise ValueError("独立初始提交的内容与已复现来源不一致")
            building.rename(target)
            return target, baseline_sha
        except Exception:
            if building.exists():
                shutil.rmtree(building, ignore_errors=True)
            raise

    def convert_bug_to_task(self, candidate_id: str) -> Dict[str, Any]:
        # HTTP and automatic conversion share the same claim; repeated HTTP
        # requests reuse the existing result rather than generating again.
        with self._bug_conversion_lock:
            existing = self.db.one(
                "SELECT * FROM tasks WHERE source='bug_discovery' AND source_id=? "
                "AND status<>'rejected' ORDER BY created_at LIMIT 1", (candidate_id,),
            )
            if existing:
                if not task_difficulty_allowed("bugfix", str(existing.get("difficulty") or "")):
                    raise ValueError("已转换的 Bug 任务难度低于困难，不能重新准入")
                if self._strict_bug_admission():
                    self._append_manual_bug_reservation(existing["id"])
                return existing
            return self._convert_bug_to_task_locked(candidate_id)

    def _independent_bug_difficulty_review(
            self, candidate: Dict[str, Any], task_id: str, title: str,
            prompt: str, category: str, baseline_path: Path,
            baseline_sha: str) -> Dict[str, Any]:
        """Review the exact isolated baseline without the discovery model's grade."""
        draft = {
            "source": "bug_discovery", "source_id": candidate["id"],
            "task_type": "bugfix", "title": title, "prompt": prompt,
            "project_category": category,
            "baseline_path": str(baseline_path), "baseline_sha": baseline_sha,
            "parent_pair_id": candidate["source_pair_id"],
        }
        recent = self.db.all(
            """SELECT t.title,t.task_type,d.assessed_difficulty,d.reason
                 FROM difficulty_reviews d JOIN pairs p ON p.id=d.pair_id
                 JOIN tasks t ON t.id=p.task_id
                WHERE d.status='rejected' ORDER BY d.reviewed_at DESC LIMIT 12"""
        )
        review = self.codex.run(
            "task_validation",
            task_validation_prompt(
                json.dumps(draft, ensure_ascii=False, indent=2),
                json.dumps(self._task_duplicate_context(draft, exclude_task_id=task_id), ensure_ascii=False),
                self._task_baseline_evidence(draft),
                json.dumps(recent, ensure_ascii=False),
            ),
            VALIDATION_SCHEMA, cwd=baseline_path,
            pair_id=candidate["source_pair_id"], task_id=task_id,
        )
        evidence = {
            str(item).strip() for item in (review.get("difficultyEvidence") or [])
            if str(item).strip()
        }
        difficulty_ok = task_difficulty_allowed(
            "bugfix", str(review.get("difficulty") or ""),
        ) and len(evidence) >= 2
        accepted = bool(
            review.get("accepted") and not review.get("banned")
            and not review.get("duplicate") and review.get("baselineReady")
            and difficulty_ok
        )
        if not accepted:
            reason = (
                ("独立基线复核未证明困难难度：" if not difficulty_ok else "独立基线复核未通过：")
                + str(review.get("reason") or "难度、基线或具体依据不足")
            )[:2000]
            stamp = now_iso()
            self.db.execute(
                "UPDATE bug_candidates SET status=?,error=?,updated_at=? WHERE id=?",
                ("difficulty_rejected" if not difficulty_ok else "rejected",
                 reason, stamp, candidate["id"]),
            )
            self.db.audit("bug.independent_admission_rejected", "bug_candidate", candidate["id"], {
                "baselineSha": baseline_sha, "review": review, "reason": reason,
            })
            raise ValueError(reason)
        self.db.audit("bug.independent_admission_passed", "bug_candidate", candidate["id"], {
            "baselineSha": baseline_sha, "review": review,
        })
        return review

    def _convert_bug_to_task_locked(self, candidate_id: str) -> Dict[str, Any]:
        candidate = self.db.one("SELECT * FROM bug_candidates WHERE id=?", (candidate_id,))
        if not candidate:
            raise KeyError("Bug 候选不存在")
        if candidate["status"] != "reproduced" or candidate["reproduce_count"] < 2 or not task_difficulty_allowed("bugfix", candidate["difficulty"]):
            raise ValueError("只有双次复现且难度为困难或地狱的 Bug 才能创建任务")
        manual_bug_only = self._strict_bug_admission()
        admission_issues = self._manual_bug_candidate_issues(candidate) if manual_bug_only else []
        if admission_issues:
            reason = "；".join(admission_issues)
            self.db.execute(
                "UPDATE bug_candidates SET status='difficulty_rejected',error=?,updated_at=? WHERE id=?",
                (reason, now_iso(), candidate_id),
            )
            raise ValueError(reason)
        try:
            source_paths = json.loads(str(candidate.get("source_paths_json") or "[]"))
        except (TypeError, ValueError):
            source_paths = []
        earlier_candidates = [
            row for row in self._bug_source_candidate_rows(
                str(candidate["source_pair_id"]), str(candidate["source_arm"]),
                str(candidate["source_sha"]),
            )
            if (str(row.get("created_at") or ""), str(row.get("id") or ""))
            < (str(candidate.get("created_at") or ""), candidate_id)
        ]
        duplicate_candidate = self._bug_source_candidate_duplicate({
            "title": candidate["title"], "actual": candidate["actual_result"],
            "expected": candidate["expected_result"], "sourcePaths": source_paths,
        }, earlier_candidates)
        if duplicate_candidate:
            self.db.execute(
                "UPDATE bug_candidates SET status='duplicate_rejected',error=?,updated_at=? WHERE id=?",
                (duplicate_candidate[-2000:], now_iso(), candidate_id),
            )
            self.db.audit("bug.duplicate_rejected", "bug_candidate", candidate_id, {
                "reason": duplicate_candidate, "title": candidate["title"],
            })
            raise ValueError(duplicate_candidate)
        arm = self._bug_candidate_source_arm(candidate)
        source_task = self.db.one(
            """SELECT t.* FROM tasks t JOIN pairs p ON p.task_id=t.id WHERE p.id=?""",
            (candidate["source_pair_id"],),
        ) or {}
        prompt = self._generate_bugfix_task_prompt(candidate, arm, source_task)
        public_title = self._bugfix_public_title(prompt, str(candidate.get("title") or ""))
        prior_task = self.db.one(
            """SELECT t.id,t.status,EXISTS(SELECT 1 FROM pairs p WHERE p.task_id=t.id) has_pair
                 FROM tasks t WHERE t.source='bug_discovery' AND t.source_id=?
                 ORDER BY created_at DESC,id DESC LIMIT 1""", (candidate_id,),
        ) or {}
        duplicate = self._deterministic_task_duplicate({
            "source": "bug_discovery", "task_type": "bugfix",
            "parent_pair_id": str(candidate.get("source_pair_id") or ""),
            "title": public_title, "prompt": prompt,
        }, exclude_task_id=str(prior_task.get("id") or ""))
        if duplicate:
            stamp = now_iso()
            self.db.execute(
                "UPDATE bug_candidates SET status='duplicate_rejected',error=?,updated_at=? WHERE id=?",
                (duplicate[-2000:], stamp, candidate_id),
            )
            self.db.audit("bug.duplicate_rejected", "bug_candidate", candidate_id, {
                "reason": duplicate, "title": candidate["title"],
            })
            raise ValueError(duplicate)
        # A model/API retry must reuse the same immutable isolated baseline.
        task_id = "task-" + hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:16]
        stamp = now_iso()
        stack = infer_project_stack(arm.get("workspace_path"), source_task.get("stack"))
        category = normalize_project_category(
            source_task.get("project_category"), source_task.get("stack"), source_task.get("prompt"),
        )
        if prior_task.get("status") == "rejected" and not prior_task.get("has_pair"):
            task_id = str(prior_task["id"])
        baseline_path, baseline_sha = self._create_isolated_bug_baseline(
            candidate, arm, task_id,
        )
        independent = self._independent_bug_difficulty_review(
            candidate, task_id, public_title, prompt, category,
            baseline_path, baseline_sha,
        )
        reviewed_difficulty = str(independent["difficulty"])
        reviewed_evidence = json.dumps(independent["difficultyEvidence"], ensure_ascii=False)
        key = fingerprint("bugfix", prompt, baseline_sha)
        with self.db.transaction() as conn:
            if prior_task.get("status") == "rejected" and not prior_task.get("has_pair"):
                conn.execute(
                    """UPDATE tasks SET title=?,prompt=?,stack=?,project_category=?,difficulty=?,
                       difficulty_evidence_json=?,estimated_module_count=?,estimated_source_lines_min=?,
                       estimated_source_lines_max=?,estimated_minutes_min=?,estimated_minutes_max=?,
                       complexity_axes_json=?,baseline_path=?,baseline_sha=?,parent_pair_id=?,fingerprint=?,
                       status='ready',rejection_reason='',used_at=NULL,updated_at=? WHERE id=?""",
                    (public_title, prompt, stack, category, reviewed_difficulty,
                     reviewed_evidence, candidate.get("estimated_module_count", 0),
                     candidate.get("estimated_source_lines_min", 0), candidate.get("estimated_source_lines_max", 0),
                     candidate.get("estimated_minutes_min", 0), candidate.get("estimated_minutes_max", 0),
                     candidate.get("complexity_axes_json", "[]"), str(baseline_path), baseline_sha,
                     candidate["source_pair_id"], key, stamp, task_id),
                )
            else:
                conn.execute(
                    """INSERT INTO tasks(id,source,source_id,task_type,title,prompt,stack,project_category,difficulty,difficulty_evidence_json,
                       estimated_module_count,estimated_source_lines_min,estimated_source_lines_max,
                       estimated_minutes_min,estimated_minutes_max,complexity_axes_json,
                       baseline_path,baseline_sha,parent_pair_id,fingerprint,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, "bug_discovery", candidate_id, "bugfix", public_title, prompt,
                     stack, category, reviewed_difficulty, reviewed_evidence,
                     candidate.get("estimated_module_count", 0), candidate.get("estimated_source_lines_min", 0),
                     candidate.get("estimated_source_lines_max", 0), candidate.get("estimated_minutes_min", 0),
                     candidate.get("estimated_minutes_max", 0), candidate.get("complexity_axes_json", "[]"),
                     str(baseline_path), baseline_sha, candidate["source_pair_id"],
                     key, "ready", stamp, stamp),
                )
            conn.execute("UPDATE bug_candidates SET status='converted',updated_at=? WHERE id=?", (stamp, candidate_id))
            conn.execute("UPDATE tasks SET repair_verification_json=? WHERE id=?",
                            (candidate.get("repair_verification_json") or "[]", task_id))
            if manual_bug_only:
                self._append_manual_bug_reservation_in_transaction(conn, task_id)
        self.db.audit("bug.converted_to_task", "bug_candidate", candidate_id, {
            "task_id": task_id,
            "source_sha": candidate["source_sha"],
            "isolated_baseline_path": str(baseline_path),
            "isolated_baseline_sha": baseline_sha,
            "root_commit_count": 1,
            "manual_reserved": manual_bug_only,
        })
        self._advance_task_mix_policy()
        return self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,)) or {}

    def start_recording(self, pair_id: str, arm: str, x: int = 0, y: int = 0, manual: bool = False,
                        demo_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if pair.get("stage") in ("difficulty_review", "difficulty_rejected"):
            raise ValueError("实际难度复评通过后才能录制")
        task = self.db.one("SELECT task_type FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        if task.get("task_type") == "bugfix":
            fix = self._bug_fix_evidence(pair_id, arm)
            if fix.get("status") != "passed":
                run = self.db.one("SELECT commit_sha FROM arm_runs WHERE pair_id=? AND arm=?", (pair_id, arm)) or {}
                check = self.db.one(
                    """SELECT status FROM artifact_checks WHERE pair_id=? AND arm=? AND commit_sha=?
                       ORDER BY created_at DESC,id DESC LIMIT 1""",
                    (pair_id, arm, run.get("commit_sha")),
                ) or {}
                if check.get("status") != "observed_failed":
                    raise ValueError("指定 Bug 尚未通过独立修复验证，不能录制合格功能录像")
                # A failed private Bug check remains failed. When the Docker
                # application itself passed every check, the recorder may
                # demonstrate its real functional page instead of a log page.
        return self.recordings.start(pair_id, arm, x, y, manual=manual, demo_override=demo_override)

    def stop_recording(self, pair_id: str, arm: str) -> Dict[str, Any]:
        row = self.recordings.stop(pair_id, arm)
        # The process updates validation asynchronously. The UI refresh exposes
        # its recording/passed or recording/failed result.
        return row

    def refresh_recording_stage(self, pair_id: str) -> None:
        pair = self._pair(pair_id)
        if pair["stage"] != "recording":
            return
        checks = self._current_artifact_checks(pair_id)
        if len(checks) != 2 or any(
            row.get("status") not in ("passed", "observed_failed") for row in checks
        ):
            return
        passed_arms = {
            str(row.get("arm") or "") for row in checks if row.get("status") == "passed"
        }
        if not passed_arms:
            self.db.execute(
                "UPDATE pairs SET stage='gsb_ready',updated_at=? WHERE id=?",
                (now_iso(), pair_id),
            )
            return
        rows = self.db.all(
            """SELECT r.arm,r.status FROM recordings r
                 JOIN arm_runs a ON a.pair_id=r.pair_id AND a.arm=r.arm AND a.commit_sha=r.commit_sha
                WHERE r.pair_id=? AND r.commit_match=1""",
            (pair_id,),
        )
        recorded_arms = {str(row.get("arm") or "") for row in rows if row.get("status") == "passed"}
        if passed_arms <= recorded_arms:
            self.db.execute(
                "UPDATE pairs SET stage='gsb_ready',updated_at=? WHERE id=?",
                (now_iso(), pair_id),
            )

    def _gsb_evidence_ready(self, pair_id: str) -> bool:
        """Wait for both final artifact results and every required recording."""
        checks = self._current_artifact_checks(pair_id)
        by_arm = {str(row.get("arm") or ""): row for row in checks}
        if any((by_arm.get(arm) or {}).get("status") not in ("passed", "observed_failed")
               for arm in ("A", "B")):
            return False
        passed_arms = {arm for arm in ("A", "B") if by_arm[arm]["status"] == "passed"}
        if not passed_arms:
            return True
        recordings = self.db.all(
            """SELECT r.arm FROM recordings r JOIN arm_runs a
                 ON a.pair_id=r.pair_id AND a.arm=r.arm AND a.commit_sha=r.commit_sha
                WHERE r.pair_id=? AND r.status='passed' AND r.commit_match=1""",
            (pair_id,),
        )
        recorded_arms = {str(row["arm"]) for row in recordings}
        if passed_arms <= recorded_arms:
            return True
        self.db.execute(
            """UPDATE pairs SET stage='recording',updated_at=?
                 WHERE id=? AND stage='gsb_ready'""",
            (now_iso(), pair_id),
        )
        self.db.audit("gsb.missing_recordings_requeued", "pair", pair_id, {
            "arms": sorted(passed_arms - recorded_arms),
        })
        return False

    def _post_difficulty_stage(self, pair_id: str,
                               checks: List[Dict[str, Any]]) -> tuple:
        passed = [str(check.get("arm") or "") for check in checks if check.get("status") == "passed"]
        failed = [str(check.get("arm") or "") for check in checks if check.get("status") != "passed"]
        if passed:
            error = (
                "Docker 验收失败侧作为最终 GSB 证据保留并跳过录像；通过侧继续录像："
                + "、".join(passed)
                if failed else ""
            )
            return "recording", error
        return "gsb_ready", "A/B Docker 验收均失败，已保留原提交并直接进入 GSB"

    def _quarantine_non_bug_preflight_recoveries(self) -> int:
        """Undo legacy preflight recovery that conflicts with Bug-only mode.

        This is deliberately limited to Pairs carrying our recovery audit and
        with no Claude prompt/session/commit.  It therefore cannot interrupt
        a healthy or completed development Arm.
        """
        if not bool(self.db.setting("manual_bug_only_mode", False)):
            return 0
        rows = self.db.all(
            """SELECT DISTINCT p.id,p.stage,t.task_type FROM pairs p
                 JOIN tasks t ON t.id=p.task_id
                 JOIN audit_events e ON e.entity_id=p.id
                WHERE e.event_type IN (
                      'task.baseline_preflight_recovered',
                      'task.baseline_preflight_retry_resumed')
                  AND t.task_type<>'bugfix'
                  AND p.status IN ('queued','running','review','repair_pending')
                  AND NOT EXISTS (
                      SELECT 1 FROM arm_runs a WHERE a.pair_id=p.id
                       AND (a.prompt_sent_at IS NOT NULL OR a.session_id<>'' OR a.commit_sha<>'')
                  )
                ORDER BY p.updated_at,p.id"""
        )
        quarantined = 0
        for row in rows:
            stamp = now_iso()
            reason = "人工 Bug-only 模式已禁止恢复历史 %s 任务" % (
                str(row.get("task_type") or "非 Bug"),
            )
            with self.db.transaction() as conn:
                changed = conn.execute(
                    """UPDATE pairs SET status='failed',stage='paused_manual_bug_only',
                         error=?,updated_at=? WHERE id=?
                         AND status IN ('queued','running','review','repair_pending')""",
                    (reason, stamp, row["id"]),
                ).rowcount
                if changed:
                    conn.execute(
                        """UPDATE arm_runs SET status='failed',error=?,finished_at=?,updated_at=?
                             WHERE pair_id=? AND status IN ('queued','manual_waiting')""",
                        (reason, stamp, stamp, row["id"]),
                    )
            if not changed:
                continue
            self.db.audit("task.baseline_preflight_recovery_quarantined", "pair", row["id"], {
                "taskType": row.get("task_type") or "",
                "previousStage": row.get("stage") or "",
                "claudeStarted": False,
                "reason": reason,
            })
            quarantined += 1
        return quarantined

    def _current_artifact_checks(self, pair_id: str) -> List[Dict[str, Any]]:
        return self.db.all(
            """SELECT c.* FROM artifact_checks c
                 JOIN arm_runs a ON a.pair_id=c.pair_id AND a.arm=c.arm AND a.commit_sha=c.commit_sha
                WHERE c.pair_id=? AND c.id=(
                  SELECT latest.id FROM artifact_checks latest
                   WHERE latest.pair_id=c.pair_id AND latest.arm=c.arm
                     AND latest.commit_sha=c.commit_sha
                   ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1
                ) ORDER BY c.arm""",
            (pair_id,),
        )

    def _require_artifact_results(self, pair_id: str) -> List[Dict[str, Any]]:
        checks = self._current_artifact_checks(pair_id)
        by_arm = {row.get("arm"): row for row in checks}
        missing = [arm for arm in ("A", "B") if arm not in by_arm]
        if missing:
            raise ValueError("A/B 必须先完成 Docker 产物验收；缺少结果：" + "、".join(missing))
        return checks

    def _require_passed_artifacts(self, pair_id: str) -> List[Dict[str, Any]]:
        checks = self._current_artifact_checks(pair_id)
        by_arm = {row.get("arm"): row for row in checks}
        failed = [arm for arm in ("A", "B") if (by_arm.get(arm) or {}).get("status") != "passed"]
        if failed:
            raise ValueError("A/B 必须先通过 Docker 产物验收；未通过：" + "、".join(failed))
        return checks

    def _require_evaluated_artifacts(self, pair_id: str) -> List[Dict[str, Any]]:
        checks = self._current_artifact_checks(pair_id)
        by_arm = {row.get("arm"): row for row in checks}
        missing = [
            arm for arm in ("A", "B")
            if (by_arm.get(arm) or {}).get("status") not in ("passed", "observed_failed")
        ]
        if missing:
            raise ValueError("A/B 必须先完成 Docker 产物验收；尚未完成：" + "、".join(missing))
        return checks

    def _difficulty_arm_evidence(self, pair: Dict[str, Any], arm: Dict[str, Any],
                                 check: Dict[str, Any]) -> Dict[str, Any]:
        workspace = Path(str(arm.get("workspace_path") or ""))
        baseline = str(pair.get("baseline_sha") or "")
        commit = str(arm.get("commit_sha") or "")
        diff_range = "%s..%s" % (baseline, commit) if baseline and commit else commit
        code: Dict[str, Any] = {"range": diff_range, "stat": "", "files": []}
        if workspace.is_dir() and diff_range:
            stat = run_command(
                ["git", "diff", "--stat", "--find-renames", diff_range],
                cwd=workspace, timeout=60, check=False,
            )
            names = run_command(
                ["git", "diff", "--name-status", "--find-renames", diff_range],
                cwd=workspace, timeout=60, check=False,
            )
            code["stat"] = self._trace_value_text(stat.stdout or stat.stderr, 1800)
            code["files"] = [line[:300] for line in (names.stdout or "").splitlines()[:120]]
            if stat.returncode or names.returncode:
                code["error"] = self._trace_value_text(stat.stderr or names.stderr, 500)
            if baseline and commit:
                try:
                    delivery = self.git.delivery_diff_summary(workspace, baseline, commit)
                    code["sourceFiles"] = delivery.get("source_files", [])
                    code["sourceLineChanges"] = int(delivery.get("source_line_changes") or 0)
                    code["effectiveBusinessSource"] = self.git.effective_business_source_diff(
                        workspace, baseline, commit,
                    )
                except Exception as exc:
                    code["deliverySummaryError"] = self._trace_value_text(exc, 500)
        try:
            check_items = json.loads(str(check.get("checks_json") or "[]"))
        except ValueError:
            check_items = []
        compact_checks = []
        for item in check_items[:20]:
            compact_checks.append({
                "name": str(item.get("name") or ""),
                "passed": bool(item.get("passed")),
                "detail": self._trace_value_text(item.get("detail"), 500),
            })
        trace = self._trace_action_evidence(arm)
        trace_events = list(trace.get("events") or [])
        if len(trace_events) > 100:
            trace["events"] = trace_events[:30] + trace_events[-70:]
            trace["omittedForDifficultyReview"] = len(trace_events) - 100
        return {
            "arm": arm.get("arm"),
            "commit": commit,
            "developmentResult": self._trace_value_text(arm.get("result"), 1600),
            "codeChange": code,
            "docker": {
                "status": check.get("status"),
                "checks": compact_checks,
                "error": self._trace_value_text(check.get("error"), 600),
            },
            "traceEvidence": trace,
        }

    def reassess_actual_difficulty(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if pair.get("status") == "completed" or pair.get("stage") == "completed":
            raise ValueError("已完成或已质检的数据不执行开发后难度回写")
        if pair.get("stage") != "difficulty_review":
            raise ValueError("只有 A/B 开发和 Docker 验收完成后才能复评实际难度")
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        if len(arms) != 2 or any(arm.get("status") != "completed" or not arm.get("commit_sha") for arm in arms):
            raise ValueError("A/B 两侧必须都已完成并形成提交")
        checks = self._require_artifact_results(pair_id)
        arm_by_name = {str(arm["arm"]): arm for arm in arms}
        check_by_name = {str(check["arm"]): check for check in checks}
        commits = {name: str(arm_by_name[name].get("commit_sha") or "") for name in ("A", "B")}
        existing = self.db.one("SELECT * FROM difficulty_reviews WHERE pair_id=?", (pair_id,))
        if existing and existing.get("a_commit_sha") == commits["A"] and existing.get("b_commit_sha") == commits["B"]:
            if existing.get("status") == "passed":
                next_stage, next_error = self._post_difficulty_stage(pair_id, checks)
                self.db.execute(
                    "UPDATE pairs SET status='running',stage=?,error=?,updated_at=? WHERE id=?",
                    (next_stage, next_error, now_iso(), pair_id),
                )
                return existing
            if existing.get("status") == "rejected":
                return existing
        review_id = str(existing.get("id") if existing else "") or "difficulty-" + uuid.uuid4().hex[:16]
        original = str(existing.get("original_difficulty") if existing else task.get("difficulty") or "")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO difficulty_reviews(
                 id,pair_id,original_difficulty,a_commit_sha,b_commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'running',?,?)
               ON CONFLICT(pair_id) DO UPDATE SET a_difficulty='',b_difficulty='',assessed_difficulty='',
                 reason='',evidence_json='[]',a_commit_sha=excluded.a_commit_sha,
                 b_commit_sha=excluded.b_commit_sha,status='running',error='',reviewed_at=NULL,
                 updated_at=excluded.updated_at""",
            (review_id, pair_id, original, commits["A"], commits["B"], stamp, stamp),
        )
        a_evidence = self._difficulty_arm_evidence(pair, arm_by_name["A"], check_by_name["A"])
        b_evidence = self._difficulty_arm_evidence(pair, arm_by_name["B"], check_by_name["B"])
        estimate = {
            "moduleCount": int(task.get("estimated_module_count") or 0),
            "sourceLineRange": [
                int(task.get("estimated_source_lines_min") or 0),
                int(task.get("estimated_source_lines_max") or 0),
            ],
            "minuteRange": [
                int(task.get("estimated_minutes_min") or 0),
                int(task.get("estimated_minutes_max") or 0),
            ],
            "complexityAxes": json.loads(str(task.get("complexity_axes_json") or "[]")),
        }
        prompt = actual_difficulty_review_prompt(
            str(task.get("prompt") or ""), original,
            json.dumps(a_evidence, ensure_ascii=False),
            json.dumps(b_evidence, ensure_ascii=False),
            str(task.get("task_type") or "zero_to_one"),
            json.dumps(estimate, ensure_ascii=False),
        )
        repo = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,)) or {}
        cwd = Path(str(repo.get("local_root") or arm_by_name["A"].get("workspace_path") or self.config.data_dir))
        try:
            result = self.codex.run(
                "difficulty_reassessment", prompt, ACTUAL_DIFFICULTY_SCHEMA,
                cwd=cwd, pair_id=pair_id, task_id=pair["task_id"], timeout=1800,
            )
        except Exception as exc:
            error = redact(str(exc))[-2000:]
            self.db.execute(
                "UPDATE difficulty_reviews SET status='failed',error=?,updated_at=? WHERE pair_id=?",
                (error, now_iso(), pair_id),
            )
            self.db.execute(
                "UPDATE pairs SET status='running',stage='difficulty_review',error=?,updated_at=? WHERE id=?",
                ("实际难度复评失败，将自动重试：" + error, now_iso(), pair_id),
            )
            raise
        raw_assessed = str(result.get("difficulty") or "")
        task_type = str(task.get("task_type") or "")
        accepted = task_difficulty_allowed(task_type, raw_assessed)
        # Keep the actual rating even when it fails the hard-only threshold;
        # never promote medium work to hard just to retain the Pair.
        assessed = raw_assessed
        status = "passed" if accepted else "rejected"
        reason = str(result.get("reason") or "").strip()[:800]
        evidence = [str(value)[:300] for value in list(result.get("evidence") or [])[:10]]
        if task_type == "bugfix":
            for name, arm_evidence in (("A", a_evidence), ("B", b_evidence)):
                actual = arm_evidence.get("codeChange", {}).get("effectiveBusinessSource")
                if actual is not None:
                    evidence.append("%s实际有效源码增删：%s" % (
                        name, json.dumps(actual, ensure_ascii=False, separators=(",", ":")),
                    ))
        stamp = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE difficulty_reviews SET a_difficulty=?,b_difficulty=?,assessed_difficulty=?,
                   reason=?,evidence_json=?,status=?,error='',reviewed_at=?,updated_at=? WHERE pair_id=?""",
                (str(result.get("aDifficulty") or ""), str(result.get("bDifficulty") or ""),
                 assessed, reason, json.dumps(evidence, ensure_ascii=False), status, stamp, stamp, pair_id),
            )
            if accepted:
                conn.execute(
                    "UPDATE tasks SET difficulty=?,difficulty_evidence_json=?,updated_at=? WHERE id=?",
                    (assessed, json.dumps(evidence, ensure_ascii=False), stamp, pair["task_id"]),
                )
                next_stage, next_error = self._post_difficulty_stage(pair_id, checks)
                conn.execute(
                    "UPDATE pairs SET status='running',stage=?,error=?,updated_at=? WHERE id=?",
                    (next_stage, next_error, stamp, pair_id),
                )
            else:
                threshold = "困难/地狱"
                message = "实际难度复评为%s，低于%s准入线，已停止当前 Pair 并等待自动补位：%s" % (
                    assessed or "未知", threshold, reason,
                )
                conn.execute(
                    "UPDATE pairs SET status='failed',stage='difficulty_rejected',error=?,updated_at=? WHERE id=?",
                    (message[-3000:], stamp, pair_id),
                )
                conn.execute(
                    """INSERT INTO delivery_submissions(id,pair_id,status,error,created_at,updated_at)
                       VALUES(?,?,'discarded',?,?,?) ON CONFLICT(pair_id) DO UPDATE SET
                         status='discarded',error=excluded.error,updated_at=excluded.updated_at""",
                    ("delivery-" + uuid.uuid4().hex[:16], pair_id, message[-2000:], stamp, stamp),
                )
        self.db.audit(
            "difficulty.passed" if accepted else "difficulty.rejected",
            "pair", pair_id,
            {"original": original, "assessed": assessed, "raw_assessed": raw_assessed, "a": result.get("aDifficulty"),
             "b": result.get("bDifficulty"), "commits": commits},
        )
        return self.db.one("SELECT * FROM difficulty_reviews WHERE pair_id=?", (pair_id,)) or {}

    def _current_process_events(self, pair_id: str) -> List[Dict[str, Any]]:
        arms = self.db.all(
            "SELECT id,prompt_sent_at FROM arm_runs WHERE pair_id=?", (pair_id,),
        )
        cutoffs = {str(arm["id"]): str(arm.get("prompt_sent_at") or "") for arm in arms}
        pair_cutoff = max(cutoffs.values(), default="")
        rows = self.db.all(
            """SELECT event_type,entity_id,detail_json,created_at FROM audit_events
               WHERE (entity_id=? OR entity_id LIKE ? OR detail_json LIKE ?)
                 AND (event_type LIKE 'claude.%' OR event_type LIKE 'artifact.%' OR event_type LIKE 'recording.%')
               ORDER BY id""",
            (pair_id, pair_id + "-%", "%" + pair_id + "%"),
        )
        current = []
        for row in rows:
            cutoff = cutoffs.get(str(row.get("entity_id") or ""), pair_cutoff)
            if not cutoff or str(row.get("created_at") or "") >= cutoff:
                # Process events are sent to a blind GSB reviewer. Keep the
                # factual event, but never expose a prior verdict/reason that
                # may have been recorded while invalidating an old review.
                clean = dict(row)
                try:
                    detail = json.loads(str(row.get("detail_json") or "{}"))
                    clean["detail_json"] = json.dumps(
                        self._blind_process_detail(detail), ensure_ascii=False,
                    )
                except (TypeError, ValueError):
                    clean["detail_json"] = "{}"
                current.append(clean)
        return current

    @staticmethod
    def _blind_process_detail(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: PairwiseService._blind_process_detail(item)
                for key, item in value.items()
                if not re.search(r"verdict|winner|review|conclusion|reason", str(key), re.I)
            }
        if isinstance(value, list):
            return [PairwiseService._blind_process_detail(item) for item in value]
        return value

    @staticmethod
    def _trace_value_text(value: Any, limit: int = 700) -> str:
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif not isinstance(item, dict):
                    parts.append(str(item))
            text = " ".join(parts)
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            text = str(value or "")
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) <= limit:
            return text
        head = max(120, limit // 3)
        return text[:head] + " … " + text[-(limit - head - 3):]

    def _trace_action_evidence(self, arm: Dict[str, Any]) -> Dict[str, Any]:
        """Extract visible actions/results without exposing assistant reasoning."""
        trace_root = Path(str(arm.get("trace_path") or ""))
        if not trace_root.is_dir():
            return {"available": False, "events": []}
        files = sorted(trace_root.rglob("*.jsonl"))
        if len(files) != 1:
            return {"available": False, "events": [], "error": "轨迹文件数量不是 1"}
        events: List[Dict[str, Any]] = []
        try:
            lines = files[0].read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return {"available": False, "events": [], "error": redact(str(exc))}
        for step, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    name = str(item.get("name") or "")
                    inputs = item.get("input") if isinstance(item.get("input"), dict) else {}
                    if name == "Bash":
                        detail = inputs.get("command") or ""
                    elif name in ("Read", "Write", "Edit"):
                        detail = inputs.get("file_path") or inputs.get("path") or ""
                    elif name in ("Glob", "Grep"):
                        detail = "%s %s" % (inputs.get("pattern") or "", inputs.get("path") or "")
                    else:
                        detail = inputs
                    events.append({
                        "step": step, "kind": "tool", "tool": name,
                        "detail": self._trace_value_text(detail, 600),
                    })
                elif item.get("type") == "tool_result":
                    result_text = self._trace_value_text(item.get("content"), 800)
                    if result_text:
                        events.append({
                            "step": step, "kind": "tool_result",
                            "isError": bool(item.get("is_error")), "detail": result_text,
                        })
        omitted = max(0, len(events) - 200)
        if omitted:
            events = events[:60] + events[-140:]
        return {
            "available": True, "traceFile": files[0].name,
            "events": events, "omittedEvents": omitted,
            "stepRule": "step 是 JSONL 内部记录号，只用于定位证据，公开评价不输出第几步",
        }

    def _bug_evidence(self, pair_id: str, arm: str) -> List[Dict[str, Any]]:
        return self.db.all(
            """SELECT title,preconditions,reproduction_steps_json,reproduction_commands_json,
                      reproduction_results_json,actual_result,expected_result,reproduce_count,
                      difficulty,status,error
                 FROM bug_candidates WHERE source_pair_id=? AND source_arm=? ORDER BY created_at""",
            (pair_id, arm),
        )

    def _bug_fix_evidence(self, pair_id: str, arm: str) -> Dict[str, Any]:
        task = self.db.one("SELECT t.task_type,t.repair_verification_json FROM tasks t JOIN pairs p ON p.task_id=t.id WHERE p.id=?", (pair_id,)) or {}
        if task.get("task_type") != "bugfix":
            return {"status": "not_applicable"}
        version = hashlib.sha256(("v1:" + (task.get("repair_verification_json") or "[]")).encode()).hexdigest()
        row = self.db.one(
            "SELECT v.* FROM bug_verification_results v JOIN arm_runs a "
            "ON a.pair_id=v.pair_id AND a.arm=v.arm AND a.commit_sha=v.commit_sha "
            "WHERE v.pair_id=? AND v.arm=? AND v.verifier_hash=? ORDER BY v.created_at DESC LIMIT 1", (pair_id, arm, version),
        )
        if row:
            return {"status": row["status"], "commitSha": row["commit_sha"],
                    "evidence": json.loads(row["evidence_json"])}
        return {"status": "not_verified", "reason": "没有指定 Bug 的独立修复验证；Docker 通过不等于缺陷已修复"}

    def _verify_fixed_bug(self, pair_id: str, arm: Dict[str, Any]) -> str:
        task = self.db.one("SELECT t.* FROM tasks t JOIN pairs p ON p.task_id=t.id WHERE p.id=?", (pair_id,)) or {}
        if task.get("task_type") != "bugfix":
            return "not_applicable"
        raw = task.get("repair_verification_json") or "[]"
        version = hashlib.sha256(("v1:" + raw).encode()).hexdigest()
        prior = self.db.one("SELECT status FROM bug_verification_results WHERE pair_id=? AND arm=? AND commit_sha=? AND verifier_hash=?",
                            (pair_id, arm["arm"], arm["commit_sha"], version))
        if prior and prior["status"] != "not_verified":
            status = str(prior["status"])
            if status == "observed_failed":
                self.db.execute(
                    """UPDATE artifact_checks SET status='observed_failed',updated_at=?
                       WHERE pair_id=? AND arm=? AND commit_sha=? AND status='passed'""",
                    (now_iso(), pair_id, arm["arm"], arm["commit_sha"]),
                )
            return status
        status = "not_verified"
        evidence = {"reason": "历史任务未绑定私有修复判据；不能依据 Docker 通过声称修好"}
        specs = json.loads(raw)
        if specs:
            try:
                if contains_browser_verification(specs):
                    raise ValueError("修复验证不能使用浏览器自动化")
                evidence = clean_commands(Path(arm["workspace_path"]), arm["commit_sha"],
                                          "bugfix-" + uuid.uuid4().hex[:12], specs, repair=True, db=self.db)
                if evidence["passed"]:
                    status = "passed"
                elif any(row["businessFailed"] for row in evidence["commands"]):
                    status = "observed_failed"
            except Exception as exc:
                evidence = {"reason": redact(str(exc))[-2000:], "classification": "verification_unavailable"}
        self.db.execute("INSERT OR REPLACE INTO bug_verification_results VALUES(?,?,?,?,?,?,?)",
                        (pair_id, arm["arm"], arm["commit_sha"], version, status,
                         json.dumps(evidence, ensure_ascii=False), now_iso()))
        self.db.audit("bug.fix_verification_finished", "arm_run", arm["id"], {
            "pairId": pair_id, "arm": arm["arm"], "status": status, "commitSha": arm["commit_sha"],
        })
        if status == "observed_failed":
            self.db.execute(
                """UPDATE artifact_checks SET status='observed_failed',updated_at=?
                   WHERE pair_id=? AND arm=? AND commit_sha=? AND status='passed'""",
                (now_iso(), pair_id, arm["arm"], arm["commit_sha"]),
            )
        return status

    @staticmethod
    def _clean_gsb_part(value: Any, limit: int) -> str:
        text = re.sub(r"[`\r\n]+", " ", str(value or "")).strip()
        # Keep the underlying action and result while removing internal JSONL
        # line numbers from public prose. Handle the common "failed at step X,
        # fixed at step Y" form first so the sentence remains natural.
        paired = re.compile(
            GSB_STEP_REFERENCE.pattern
            + r"(?P<middle>[^。；]{0,100}?)已(?:在|于)\s*"
            + GSB_STEP_REFERENCE.pattern
            + r"(?P<verb>修正|修复|修好|解决|通过|完成)"
        )

        def replace_pair(match: re.Match) -> str:
            middle = str(match.group("middle") or "").lstrip("的")
            return middle + "后来已" + str(match.group("verb") or "")

        text = paired.sub(replace_pair, text)
        text = re.sub(GSB_STEP_REFERENCE.pattern + r"\s*(?:及|和|与)\s*", "", text)
        text = GSB_STEP_REFERENCE.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]

    @staticmethod
    def _compose_gsb_reason(a_reason: str, b_reason: str) -> str:
        return "A：%s B：%s" % (a_reason, b_reason)

    @staticmethod
    def _gsb_has_locator(value: str) -> bool:
        """Return whether a public reason contains one reviewable evidence locator."""
        text = str(value or "")
        patterns = (
            r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]+",
            r"\b[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|kt|rb|php|sh|yml|yaml|json|toml|md)\b",
            # Python/JS module and package locators such as app.verify are
            # still reviewable after a conversational rewrite removes the
            # underlying file suffix.
            r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b",
            r"\b(?:docker\s+compose|pytest|npm\s+(?:test|run)|pnpm\s+(?:test|run)|yarn\s+(?:test|run)|python3?\s+|curl\s+|git\s+)[^，。；]*",
            # Natural public wording can identify a concrete verification
            # without exposing an internal path or a literal shell command.
            # These named tools/protocols plus an observed outcome are still
            # reviewable evidence, e.g. “Docker 验收通过” or “Range 返回 206”.
            r"(?:Docker|Compose|Playwright|Vitest|pytest|Go\s*测试|Range|浏览器|接口|API)[^。；]{0,100}(?:验收|测试|通过|返回|状态|报错|错误|失败|一致|正确)",
            r"\b(?:[1-5]\d\d|[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\b",
            r"(?:函数|方法|接口)\s*[A-Za-z_][A-Za-z0-9_]*",
            r"(?:报错|错误|冲突|失败)",
        )
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)

    @classmethod
    def _gsb_locator_issues(cls, a_reason: str, b_reason: str) -> List[str]:
        issues = []
        if not cls._gsb_has_locator(a_reason):
            issues.append("A 评价缺少可核对的具体证据（文件/函数、命令、接口状态或报错）")
        if not cls._gsb_has_locator(b_reason):
            issues.append("B 评价缺少可核对的具体证据（文件/函数、命令、接口状态或报错）")
        return issues

    @staticmethod
    def _gsb_conversational_issues(a_reason: str, b_reason: str) -> List[str]:
        """Flag narrow, mechanical patterns without penalizing useful detail."""
        issues: List[str] = []
        numbered_cases = re.compile(
            r"第\s*\d+\s*(?:[、，,]\s*\d+){2,}(?:\s*(?:至|到|-)\s*\d+)?\s*(?:项|条|次)?"
        )
        test_count_pile = re.compile(
            r"\d+\s*个(?:单元测试|单测|端到端测试|e2e)[^。；]{0,80}"
            r"\d+\s*个(?:单元测试|单测|端到端测试|e2e)",
            flags=re.IGNORECASE,
        )
        # A string of raw, successful API metrics is hard to read as a public
        # judgment. Keep exact values when they *are* the defect or the
        # comparison; otherwise describe the checked dimensions and outcome.
        metrics = re.compile(r"(?:正边|总量|总数|计数|向量|范围|到达值)")
        material_number = re.compile(
            r"(?:错误|不符|相差|应为|却|但|偏差|超过|低于|少于|多于|边界|临界|阈值|"
            r"精度|溢出|不同|不一致|失败|歧义|唯一|差异|本应|反而)"
        )
        for label, value in (("A", a_reason), ("B", b_reason)):
            text = str(value or "")
            if GSB_STEP_REFERENCE.search(text):
                issues.append(label + " 评价包含轨迹步骤号，应改写为实际操作或验证场景")
            if numbered_cases.search(text):
                issues.append(label + " 评价机械罗列测试编号，应改写为实际验证的业务场景")
            if test_count_pile.search(text):
                issues.append(label + " 评价堆叠测试数量，应说明这些测试验证了什么")
            if re.search(r"录像|录屏|视频|屏幕录制|recording|recorderEvents|capture_mode", text, re.I):
                issues.append(label + " 评价不得引用录像作为公开理由")
            if any(
                len(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?(?![A-Za-z])", sentence)) >= 4
                and metrics.search(sentence) and not material_number.search(sentence)
                for sentence in re.split(r"[。；\n]", text)
            ):
                issues.append(label + " 评价逐项堆叠已核对正确的原始数字，应说明核对对象和结果")
            if "未见已发生的功能缺陷" in text:
                issues.append(label + " 评价使用生硬的无缺陷套话，应改成有证据支撑的自然判断")
        return issues

    def generate_gsb(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        self.refresh_recording_stage(pair_id)
        pair = self._pair(pair_id)
        if pair["stage"] != "gsb_ready":
            raise ValueError("A/B 两侧必须先完成 Docker 验收；可启动产物还要完成合格录像")
        checks = self._require_evaluated_artifacts(pair_id)
        has_observed_failure = any(row.get("status") == "observed_failed" for row in checks)
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        recordings = self.db.all("SELECT * FROM recordings WHERE pair_id=? ORDER BY arm", (pair_id,))
        by_arm = {arm["arm"]: arm for arm in arms}
        check_by_arm = {item["arm"]: item for item in checks}
        rec_by_arm = {item["arm"]: item for item in recordings}
        required_recording_arms = [
            arm for arm in ("A", "B")
            if (check_by_arm.get(arm) or {}).get("status") == "passed"
        ]
        missing_recordings = [
            arm for arm in required_recording_arms
            if (rec_by_arm.get(arm) or {}).get("status") != "passed"
        ]
        if missing_recordings:
            raise ValueError("A/B 必须先完成对应录像；缺少：" + "、".join(missing_recordings))
        evidence = {}
        for arm in ("A", "B"):
            evidence[arm] = {
                "development": by_arm.get(arm, {}),
                "docker": check_by_arm.get(arm, {}),
                "traceEvidence": self._trace_action_evidence(by_arm.get(arm, {})),
                "discoveredBugs": self._bug_evidence(pair_id, arm),
                "specifiedBugFix": self._bug_fix_evidence(pair_id, arm),
            }
        evidence["processEvents"] = [
            event for event in self._current_process_events(pair_id)
            if not str(event.get("event_type") or "").startswith("recording.")
        ]
        review_prompt = gsb_prompt(
            task.get("prompt", ""),
            json.dumps(evidence["A"], ensure_ascii=False),
            json.dumps(evidence["B"], ensure_ascii=False),
            json.dumps(evidence["processEvents"], ensure_ascii=False),
        )
        review_prompt += "\n指定 Bug 的独立验证见 specifiedBugFix：not_verified 不代表修好，也不代表模型失败；observed_failed 才是已核对的业务缺陷。Docker 通过不能覆盖此结论。"
        result = self.codex.run(
            "gsb_review",
            review_prompt,
            GSB_SCHEMA, pair_id=pair_id, task_id=pair["task_id"], timeout=1800,
        )
        a_reason = self._clean_gsb_part(result["aReason"], 300)
        b_reason = self._clean_gsb_part(result["bReason"], 300)
        locator_issues = self._gsb_locator_issues(a_reason, b_reason)
        style_issues = self._gsb_conversational_issues(a_reason, b_reason)
        if locator_issues or style_issues:
            correction = (
                review_prompt + "\n\n上一次输出需要修正：" + "；".join(locator_issues + style_issues)
                + "\n上一次 A 理由：" + a_reason + "\n上一次 B 理由：" + b_reason
                + "\n请只依据上面的真实证据重新生成。保留能支撑结论的证据，把轨迹步骤号和机械数字改写成业务场景；每段仍要有文件/函数、命令、接口状态或报错等真实定位。"
            )
            result = self.codex.run(
                "gsb_review", correction, GSB_SCHEMA,
                pair_id=pair_id, task_id=pair["task_id"], timeout=1800,
            )
            a_reason = self._clean_gsb_part(result["aReason"], 300)
            b_reason = self._clean_gsb_part(result["bReason"], 300)
            locator_issues = self._gsb_locator_issues(a_reason, b_reason)
            style_issues = self._gsb_conversational_issues(a_reason, b_reason)
            if locator_issues or style_issues:
                raise ValueError("GSB 自动纠正后仍未通过公开理由校验：" + "；".join(locator_issues + style_issues))
        reason = self._compose_gsb_reason(a_reason, b_reason)
        review_id = "gsb-" + uuid.uuid4().hex[:16]
        stamp = now_iso()
        evidence_version = self.gsb_evidence_version(pair_id, result["verdict"], reason)
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,preference_reason,
               evidence_json,draft_verdict,draft_reason,evidence_version,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)
               ON CONFLICT(pair_id) DO UPDATE SET verdict=excluded.verdict,reason=excluded.reason,
                 a_reason=excluded.a_reason,b_reason=excluded.b_reason,preference_reason=excluded.preference_reason,
                 evidence_json=excluded.evidence_json,draft_verdict=excluded.draft_verdict,
                 draft_reason=excluded.draft_reason,final_verdict='',final_reason='',
                 evidence_version=excluded.evidence_version,
                 a_score_delivery=0,a_desc_delivery='',b_score_delivery=0,b_desc_delivery='',
                 status='draft',confirmed_by='',confirmed_at=NULL,
                 updated_at=excluded.updated_at""",
            (review_id, pair_id, result["verdict"], reason, a_reason, b_reason, "",
             json.dumps(result["evidence"], ensure_ascii=False), result["verdict"], reason,
             evidence_version, stamp, stamp),
        )
        self.db.execute("UPDATE pairs SET status='review',stage='gsb_confirmation',updated_at=? WHERE id=?", (stamp, pair_id))
        reviewer = str(self.db.setting("git_author_name", "刘昱") or "刘昱").strip() + "（按授权默认确认）"
        detail = self.confirm_gsb(pair_id, result["verdict"], a_reason, b_reason, reviewer)
        return detail.get("gsb") or {}

    def confirm_gsb(self, pair_id: str, verdict: str, a_reason: str, b_reason: str,
                    confirmed_by: str) -> Dict[str, Any]:
        if verdict not in ("A better", "Same", "B better"):
            raise ValueError("GSB 结论无效")
        checks = self._require_evaluated_artifacts(pair_id)
        has_observed_failure = any(row.get("status") == "observed_failed" for row in checks)
        clean_a = self._clean_gsb_part(a_reason, 300)
        clean_b = self._clean_gsb_part(b_reason, 300)
        if len(clean_a) < 20 or len(clean_b) < 20:
            raise ValueError("A、B 评价均至少 20 个字符，并在两段中说明支持结论的依据")
        locator_issues = self._gsb_locator_issues(clean_a, clean_b)
        if locator_issues:
            raise ValueError("；".join(locator_issues))
        recording_issues = [
            issue for issue in self._gsb_conversational_issues(clean_a, clean_b)
            if "录像" in issue
        ]
        if recording_issues:
            raise ValueError("；".join(recording_issues))
        clean = self._compose_gsb_reason(clean_a, clean_b)
        stamp = now_iso()
        evidence_version = self.gsb_evidence_version(pair_id, verdict, clean)
        self.db.execute(
            """UPDATE gsb_reviews SET draft_verdict=CASE WHEN draft_verdict='' THEN verdict ELSE draft_verdict END,
               draft_reason=CASE WHEN draft_reason='' THEN reason ELSE draft_reason END,
               verdict=?,reason=?,a_reason=?,b_reason=?,preference_reason=?,
               final_verdict=?,final_reason=?,evidence_version=?,
               status='confirmed',confirmed_by=?,confirmed_at=?,updated_at=?
               WHERE pair_id=?""",
            (verdict, clean, clean_a, clean_b, "", verdict, clean, evidence_version,
             confirmed_by.strip() or "人工确认", stamp, stamp, pair_id),
        )
        failure_arms = [row.get("arm") for row in checks if row.get("status") == "observed_failed"]
        completion_note = (
            "原始交付的产物业务验收未通过，已保留失败证据并按轨迹完成 GSB：" + "、".join(failure_arms)
            if failure_arms else ""
        )
        self.db.execute(
            """UPDATE pairs SET status='completed',stage='completed',winner=?,error=?,
               completed_at=COALESCE(completed_at,?),updated_at=? WHERE id=?""",
            (verdict, completion_note, stamp, stamp, pair_id),
        )
        pair = self._pair(pair_id)
        task = self.db.one("SELECT task_type FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        if not has_observed_failure and task.get("task_type") in ("feature", "bugfix"):
            self.db.execute("UPDATE project_chains SET followup_completed=1,status='completed',completed_at=?,updated_at=? WHERE id=?", (stamp, stamp, pair["chain_id"]))
        submission_id = "delivery-" + uuid.uuid4().hex[:16]
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,created_at,updated_at)
               VALUES(?,?,'ready_to_submit',?,?)
               ON CONFLICT(pair_id) DO UPDATE SET
                 status=CASE
                   WHEN delivery_submissions.error LIKE '待独立复核：%' THEN delivery_submissions.status
                   WHEN delivery_submissions.remote_id='' THEN 'ready_to_submit'
                   WHEN delivery_submissions.remote_status='PENDING_FIX' THEN 'needs_fix'
                   ELSE delivery_submissions.status
                 END,
                 error=CASE WHEN delivery_submissions.error LIKE '待独立复核：%'
                            THEN delivery_submissions.error ELSE '' END,
                 updated_at=excluded.updated_at""",
            (submission_id, pair_id, stamp, stamp),
        )
        self.db.audit("gsb.confirmed", "pair", pair_id, {"verdict": verdict, "confirmed_by": confirmed_by})
        return self.pair_detail(pair_id)

    def _gsb_evidence_bundle(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        arms = self.db.all(
            """SELECT arm,model,image_id,status,session_id,prompt_id,trace_path,commit_sha,result,
               warning_at,error,prompt_sent_at,finished_at FROM arm_runs WHERE pair_id=? ORDER BY arm""",
            (pair_id,),
        )
        for arm in arms:
            arm["traceEvidence"] = self._trace_action_evidence(arm)
            arm["discoveredBugs"] = self._bug_evidence(pair_id, str(arm.get("arm") or ""))
            arm["specifiedBugFix"] = self._bug_fix_evidence(pair_id, str(arm.get("arm") or ""))
        recordings = self.db.all(
            """SELECT id,arm,commit_sha,sha256,width,height,duration_seconds,status,capture_mode,commit_match,error,
               attempt_id,started_at,finished_at FROM recordings WHERE pair_id=? ORDER BY arm""",
            (pair_id,),
        )
        for recording in recordings:
            event = self.db.one(
                """SELECT detail_json FROM audit_events WHERE event_type='recording.finished'
                   AND entity_id=? ORDER BY id DESC LIMIT 1""",
                (recording.get("attempt_id") or "",),
            ) or {}
            try:
                detail = json.loads(str(event.get("detail_json") or "{}"))
            except (TypeError, ValueError):
                detail = {}
            evidence_fields = (
                "event", "ok", "status", "method", "path", "clicks", "operations",
                "featureCount", "resultControlCount", "controlsComplete", "finalResultVisible",
                "requests",
            )
            recording["recorderEvents"] = [
                {key: item[key] for key in evidence_fields if key in item}
                for item in detail.get("recorderEvents", [])
                if isinstance(item, dict) and item.get("event") == "interaction"
            ]
        return {
            "pair": {key: pair.get(key) for key in ("id", "task_id", "chain_id", "baseline_sha")},
            "task": {key: task.get(key) for key in ("title", "task_type", "difficulty", "prompt", "acceptance_json")},
            "arms": arms,
            "checks": self.db.all(
                """SELECT arm,commit_sha,status,checks_json,error,started_at,finished_at
                   FROM artifact_checks WHERE pair_id=? ORDER BY arm""",
                (pair_id,),
            ),
            "recordings": recordings,
            "processEvents": self._current_process_events(pair_id),
        }

    def gsb_evidence_version(self, pair_id: str, verdict: str = "", reason: str = "") -> str:
        payload = self._gsb_evidence_bundle(pair_id)
        # A replacement recording of the same delivered commit does not change
        # the code evidence used by GSB. Keep only its semantic readiness in the
        # version so a new file/hash/duration cannot make a finished recheck
        # stale while the user is applying it.
        payload["recordings"] = [
            {key: item.get(key) for key in ("arm", "commit_sha", "status", "commit_match", "error")}
            for item in payload.get("recordings", [])
        ]
        payload["processEvents"] = [
            item for item in payload.get("processEvents", [])
            if not str(item.get("event_type") or "").startswith("recording.")
        ]
        payload["publicVerdict"] = verdict
        payload["publicReason"] = reason
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def recheck_gsb_async(self, pair_id: str) -> str:
        operation = "gsb-recheck-" + pair_id
        self._submit(operation, self._recheck_gsb, pair_id)
        return operation

    def gsb_colloquial_source(self, pair_id: str) -> Dict[str, str]:
        """Return the current public GSB text used for a stale-safe rewrite preview."""
        self._pair(pair_id)
        review = self.db.one("SELECT verdict,a_reason,b_reason FROM gsb_reviews WHERE pair_id=?", (pair_id,))
        if not review:
            raise ValueError("尚未生成 GSB 草稿")
        return validate_source(review.get("verdict"), review.get("a_reason"), review.get("b_reason"))

    def colloquialize_gsb_async(self, pair_id: str, edits: Dict[str, Any]) -> str:
        review = self.gsb_colloquial_source(pair_id)
        source = validate_source(
            edits["verdict"] if "verdict" in edits else review["verdict"],
            edits["aReason"] if "aReason" in edits else review["aReason"],
            edits["bReason"] if "bReason" in edits else review["bReason"],
        )
        operation = "gsb-colloquial-%s-%s" % (pair_id, uuid.uuid4().hex[:10])
        self._submit(
            operation, rewrite_preview, self.codex, source, pair_id, self._gsb_locator_issues,
        )
        self.db.audit("gsb.colloquial_preview_started", "pair", pair_id, {"operation": operation})
        return operation

    def apply_gsb_colloquial_batch(self, items: Any) -> Dict[str, Any]:
        """Apply generated previews only while their source text is still current."""
        if not isinstance(items, list) or not items:
            raise ValueError("没有可应用的口语化结果")
        results = []
        applied = skipped = failed = 0
        for raw in items[:100]:
            pair_id = str(raw.get("pairId") or "") if isinstance(raw, dict) else ""
            try:
                if not re.fullmatch(r"pair-[a-zA-Z0-9]+", pair_id):
                    raise ValueError("Pair ID 格式不正确")
                source_raw = raw.get("source")
                preview_raw = raw.get("preview")
                if not isinstance(source_raw, dict) or not isinstance(preview_raw, dict):
                    raise ValueError("口语化结果缺少原文或预览")
                source = validate_source(
                    source_raw.get("verdict"), source_raw.get("aReason"), source_raw.get("bReason"),
                )
                current = self.gsb_colloquial_source(pair_id)
                if current != source:
                    skipped += 1
                    results.append({"pair_id": pair_id, "outcome": "skipped", "reason": "GSB 原文已变化，请重新口语化"})
                    continue
                preview = validate_source(
                    preview_raw.get("verdict"), preview_raw.get("aReason"), preview_raw.get("bReason"),
                )
                if preview["verdict"] != source["verdict"]:
                    raise ValueError("口语化结果改变了 GSB 结论")
                reviewer = self.db.one("SELECT confirmed_by FROM gsb_reviews WHERE pair_id=?", (pair_id,)) or {}
                confirmed_by = str(reviewer.get("confirmed_by") or "").strip()
                if not confirmed_by:
                    confirmed_by = str(self.db.setting("git_author_name", "刘昱") or "刘昱").strip()
                self.confirm_gsb(
                    pair_id, preview["verdict"], preview["aReason"], preview["bReason"], confirmed_by,
                )
                self.db.audit("gsb.colloquial_batch_applied", "pair", pair_id, {})
                applied += 1
                results.append({"pair_id": pair_id, "outcome": "applied"})
            except Exception as exc:
                failed += 1
                results.append({"pair_id": pair_id, "outcome": "failed", "error": str(exc)})
        return {"applied": applied, "skipped": skipped, "failed": failed, "results": results}

    def _recheck_gsb(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        review = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,))
        if not review:
            raise ValueError("尚未生成 GSB 草稿")
        verdict = str(review.get("verdict") or "")
        a_reason = str(review.get("a_reason") or "")
        b_reason = str(review.get("b_reason") or "")
        reason = self._compose_gsb_reason(a_reason, b_reason)
        evidence = self._gsb_evidence_bundle(pair_id)
        version = self.gsb_evidence_version(pair_id, verdict, reason)
        model = str(self.db.setting("gsb_recheck_model", "gpt-6-astra"))
        effort = str(self.db.setting("gsb_recheck_effort", "high"))
        public_evidence = {key: value for key, value in evidence.items() if key != "recordings"}
        public_evidence["processEvents"] = [
            event for event in evidence.get("processEvents", [])
            if not str(event.get("event_type") or "").startswith("recording.")
        ]
        independent_prompt = gsb_independent_recheck_prompt(
            str(evidence["task"].get("prompt") or ""),
            json.dumps(public_evidence, ensure_ascii=False),
        )
        independent_prompt += "\nspecifiedBugFix 是本次修复任务的独立验证；not_verified 不得写成通过或据此归罪模型。"
        independent = self.codex.run(
            "gsb_recheck",
            independent_prompt,
            GSB_SCHEMA,
            pair_id=pair_id,
            task_id=pair["task_id"],
            timeout=1800,
            model_override=model,
            effort_override=effort,
            cache_evidence=True,
        )
        independent_verdict = str(independent["verdict"])
        independent_a = self._clean_gsb_part(independent["aReason"], 300)
        independent_b = self._clean_gsb_part(independent["bReason"], 300)
        recheck_prompt = gsb_recheck_prompt(
            str(evidence["task"].get("prompt") or ""), verdict, a_reason, b_reason,
            independent_verdict, independent_a, independent_b,
            json.dumps(public_evidence, ensure_ascii=False),
        )
        result = self.codex.run(
            "gsb_recheck",
            recheck_prompt,
            GSB_RECHECK_SCHEMA,
            pair_id=pair_id,
            task_id=pair["task_id"],
            timeout=1800,
            model_override=model,
            effort_override=effort,
        )
        suggested_a = self._clean_gsb_part(result["suggestedAReason"], 300)
        suggested_b = self._clean_gsb_part(result["suggestedBReason"], 300)
        source_style_issues = self._gsb_conversational_issues(a_reason, b_reason)
        locator_issues = self._gsb_locator_issues(suggested_a, suggested_b)
        suggestion_style_issues = self._gsb_conversational_issues(suggested_a, suggested_b)
        if locator_issues or source_style_issues or suggestion_style_issues:
            correction = (
                recheck_prompt + "\n\n当前原评价或上一次建议需要修正："
                + "；".join(source_style_issues + locator_issues + suggestion_style_issues)
                + "\n上一次建议 A 理由：" + suggested_a + "\n上一次建议 B 理由：" + suggested_b
                + "\n请重新复检。保留所有影响结论的证据，把轨迹步骤号和无意义数字改写成实际操作或业务场景，并确保两段各自包含文件/函数、命令、接口状态或报错等可核对证据。"
            )
            result = self.codex.run(
                "gsb_recheck", correction, GSB_RECHECK_SCHEMA,
                pair_id=pair_id, task_id=pair["task_id"], timeout=1800,
                model_override=model, effort_override=effort,
            )
            suggested_a = self._clean_gsb_part(result["suggestedAReason"], 300)
            suggested_b = self._clean_gsb_part(result["suggestedBReason"], 300)
            locator_issues = self._gsb_locator_issues(suggested_a, suggested_b)
            suggestion_style_issues = self._gsb_conversational_issues(suggested_a, suggested_b)
        if any("录像" in issue for issue in suggestion_style_issues):
            raise ValueError("GSB 复检建议仍引用录像，未保存公开建议")
        result_status = str(result["status"])
        result_issues = [str(item) for item in result.get("issues", [])]
        if independent_verdict != verdict:
            result_status = "fact_conflict"
            verdict_issue = (
                "独立盲评结论为 %s，与当前结论 %s 不一致，需要按证据重新确认"
                % (independent_verdict, verdict)
            )
            if verdict_issue not in result_issues:
                result_issues.append(verdict_issue)
        result["suggestedVerdict"] = independent_verdict
        if result_status != "fact_conflict" and (source_style_issues or locator_issues or suggestion_style_issues):
            result_status = "suggested_revision"
        for issue in source_style_issues + locator_issues + suggestion_style_issues:
            if issue not in result_issues:
                result_issues.append(issue)
        suggested_reason = self._compose_gsb_reason(suggested_a, suggested_b)
        latest_job = self.db.one(
            "SELECT id FROM codex_jobs WHERE pair_id=? AND job_type='gsb_recheck' ORDER BY created_at DESC LIMIT 1",
            (pair_id,),
        ) or {}
        recheck_id = "recheck-" + uuid.uuid4().hex[:16]
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO gsb_rechecks(id,pair_id,evidence_version,input_verdict,input_reason,result_status,
               suggested_verdict,suggested_reason,suggested_a_reason,suggested_b_reason,
               suggested_preference_reason,issues_json,evidence_refs_json,model,reasoning_effort,
               codex_job_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (recheck_id, pair_id, version, verdict, reason, result_status, result["suggestedVerdict"],
             suggested_reason, suggested_a, suggested_b, "",
             json.dumps(result_issues, ensure_ascii=False),
             json.dumps(result["evidenceRefs"], ensure_ascii=False), model, effort,
             str(latest_job.get("id") or ""), stamp),
        )
        self.db.audit("gsb.rechecked", "pair", pair_id, {"recheck_id": recheck_id, "status": result_status, "model": model, "effort": effort})
        recheck = self.db.one("SELECT * FROM gsb_rechecks WHERE id=?", (recheck_id,)) or {}
        recheck.pop("suggested_preference_reason", None)
        return recheck

    def apply_gsb_recheck(self, pair_id: str, recheck_id: str) -> Dict[str, Any]:
        row = self.db.one("SELECT * FROM gsb_rechecks WHERE id=? AND pair_id=?", (recheck_id, pair_id))
        if not row:
            raise KeyError("复检记录不存在")
        review = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,)) or {}
        current_reason = self._compose_gsb_reason(
            str(review.get("a_reason") or ""), str(review.get("b_reason") or "")
        )
        current_version = self.gsb_evidence_version(
            pair_id, str(review.get("verdict") or ""), current_reason
        )
        if row["evidence_version"] != current_version:
            same_input = (
                str(row.get("input_verdict") or "") == str(review.get("verdict") or "")
                and str(row.get("input_reason") or "") == current_reason
            )
            if not same_input or self._recheck_source_changed_since(row):
                raise ValueError("公开理由或证据已经变化，请重新复检")
        verdict = row["suggested_verdict"]
        a_reason = row.get("suggested_a_reason") or ""
        b_reason = row.get("suggested_b_reason") or ""
        reviewer = str(self.db.setting("git_author_name", "刘昱") or "刘昱").strip() + "（按授权默认确认）"
        self.confirm_gsb(pair_id, verdict, a_reason, b_reason, reviewer)
        stamp = now_iso()
        applied_reason = self._compose_gsb_reason(a_reason, b_reason)
        self.db.execute(
            """UPDATE gsb_rechecks SET evidence_version=?,applied_at=?,applied_by=? WHERE id=?""",
            (self.gsb_evidence_version(pair_id, verdict, applied_reason), stamp, reviewer, recheck_id),
        )
        self.db.audit("gsb.recheck_applied", "pair", pair_id, {
            "recheck_id": recheck_id, "applied_by": reviewer,
        })
        return self.pair_detail(pair_id)

    def _recheck_source_changed_since(self, recheck: Dict[str, Any]) -> bool:
        """Support pre-fix rechecks without accepting genuinely stale evidence."""
        job = self.db.one("SELECT created_at FROM codex_jobs WHERE id=?", (recheck.get("codex_job_id") or "",)) or {}
        since = str(job.get("created_at") or recheck.get("created_at") or "")
        if not since:
            return True
        pair_id = str(recheck.get("pair_id") or "")
        source_queries = (
            ("SELECT 1 FROM tasks t JOIN pairs p ON p.task_id=t.id WHERE p.id=? AND t.updated_at>? LIMIT 1", (pair_id, since)),
            ("SELECT 1 FROM arm_runs WHERE pair_id=? AND updated_at>? LIMIT 1", (pair_id, since)),
            ("SELECT 1 FROM artifact_checks WHERE pair_id=? AND updated_at>? LIMIT 1", (pair_id, since)),
            ("""SELECT 1 FROM audit_events WHERE created_at>?
                  AND (entity_id=? OR entity_id LIKE ? OR detail_json LIKE ?)
                  AND (event_type LIKE 'claude.%' OR event_type LIKE 'artifact.%')
                  LIMIT 1""", (since, pair_id, pair_id + "-%", "%" + pair_id + "%")),
        )
        return any(self.db.one(sql, params) is not None for sql, params in source_queries)

    def apply_latest_gsb_rechecks(self, pair_ids: List[str]) -> Dict[str, Any]:
        results = []
        for pair_id in dict.fromkeys(pair_ids):
            latest = self.db.one(
                "SELECT * FROM gsb_rechecks WHERE pair_id=? ORDER BY created_at DESC LIMIT 1", (pair_id,)
            )
            if not latest:
                results.append({"pair_id": pair_id, "outcome": "skipped", "reason": "尚未完成复检"})
                continue
            if latest.get("applied_at"):
                results.append({"pair_id": pair_id, "outcome": "skipped", "reason": "最新建议已经应用"})
                continue
            if latest.get("result_status") == "passed":
                results.append({"pair_id": pair_id, "outcome": "skipped", "reason": "复检已通过，无需应用"})
                continue
            try:
                self.apply_gsb_recheck(pair_id, latest["id"])
                results.append({"pair_id": pair_id, "outcome": "applied", "recheck_id": latest["id"]})
            except Exception as exc:
                results.append({"pair_id": pair_id, "outcome": "failed", "error": redact(str(exc))})
        return {
            "results": results,
            "applied": sum(item["outcome"] == "applied" for item in results),
            "skipped": sum(item["outcome"] == "skipped" for item in results),
            "failed": sum(item["outcome"] == "failed" for item in results),
        }

    @staticmethod
    def _pair_model_issues(detail: Dict[str, Any]) -> List[str]:
        scheme = str(detail.get("model_scheme") or "")
        if not detail.get("model_a") and not detail.get("model_b"):
            # Pairs created before the new policy retain their original model.
            return []
        arms = {row["arm"]: row for row in detail.get("arms", [])}
        issues = []
        for arm in ("A", "B"):
            expected = str(detail.get("model_" + arm.lower()) or "")
            if not expected or str((arms.get(arm) or {}).get("model") or "") != expected:
                issues.append(arm + " 运行模型与创建 Pair 时锁定的模型不一致")
        if scheme == "cross_model" and detail.get("model_a") == detail.get("model_b"):
            issues.append("跨模型 Pair 的 A/B 模型必须不同")
        if scheme == "legacy" and detail.get("model_a") != detail.get("model_b"):
            issues.append("原模型 Pair 的 A/B 模型必须相同")
        a, b = arms.get("A") or {}, arms.get("B") or {}
        if a.get("image") and b.get("image") and a["image"] != b["image"]:
            issues.append("A/B 运行镜像不同；新方案只允许切换模型名")
        if a.get("image_id") and b.get("image_id") and a["image_id"] != b["image_id"]:
            issues.append("A/B 镜像版本不同；新方案只允许切换模型名")
        return issues

    @staticmethod
    def _solo_qa_submission_day(value: str) -> Optional[date]:
        """SOLO-QA submission timestamps are UTC, including its naive ISO values."""
        try:
            submitted = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=timezone.utc)
        return submitted.astimezone(ZoneInfo("Asia/Shanghai")).date()

    def _g18_double_full_issue(self, pair_id: str, a_score: int, b_score: int,
                               at: Optional[datetime] = None) -> str:
        """Guard the observed G18 daily quota without changing evidence-based scores.

        The platform first reported this quota at its tenth daily record. Use
        all known submissions and in-flight new submissions to reach that
        threshold, but exclude discarded non-full rows from the ratio's
        denominator. Keep discarded double-full rows in the numerator until
        the platform's exact denominator is known.
        """
        if (a_score, b_score) != (5, 5):
            return ""
        day = (at or datetime.now(timezone.utc)).astimezone(ZoneInfo("Asia/Shanghai")).date()
        rows = self.db.all(
            """SELECT d.pair_id,d.status,d.submitted_at,d.updated_at,
                      g.a_score_delivery,g.b_score_delivery
                 FROM delivery_submissions d JOIN gsb_reviews g ON g.pair_id=d.pair_id
                WHERE d.pair_id<>? AND ((d.remote_id<>'' AND d.submitted_at IS NOT NULL)
                      OR (d.remote_id='' AND d.status='submitting'))""",
            (pair_id,),
        )
        submitted_today = [
            row for row in rows
            if self._solo_qa_submission_day(row["submitted_at"] or row["updated_at"]) == day
        ]
        daily = [
            row for row in submitted_today
            if (row["status"] != "discarded" or (
                int(row["a_score_delivery"] or 0) == 5 and int(row["b_score_delivery"] or 0) == 5
            ))
        ]
        projected_total = len(daily) + 1
        projected_double_full = 1 + sum(
            int(row["a_score_delivery"] or 0) == 5 and int(row["b_score_delivery"] or 0) == 5
            for row in daily
        )
        if len(submitted_today) + 1 < 10 or projected_double_full * 10 <= projected_total:
            return ""
        return (
            "G18 当日双侧满分占比超限：按保守口径，本地已记录当天 %d 条提交或提交中记录，"
            "其中 %d 条 A/B 均为 5 分；"
            "本条仍为双侧 5 分，提交后预计超过 10%%。请核实平台当日配额；"
            "系统不会为绕过规则自动改分。" % (len(daily), projected_double_full - 1)
        )

    def delivery_preflight(self, pair_id: str, include_platform: bool = False) -> Dict[str, Any]:
        detail = self.pair_detail(pair_id)
        blockers: List[str] = []
        warnings: List[str] = []
        task = detail.get("task") or {}
        repository = detail.get("repository") or {}
        if repository and str(repository.get("visibility") or "").casefold() != "public":
            blockers.append("GitHub 仓库不是公开仓库，SOLO-QA 无法核验分支与提交")
        arms = {row["arm"]: row for row in detail.get("arms", [])}
        blockers.extend(self._pair_model_issues(detail))
        checks = {row["arm"]: row for row in detail.get("checks", [])}
        recs = {row["arm"]: row for row in detail.get("recordings", [])}
        for arm in ("A", "B"):
            item = arms.get(arm) or {}
            if not item.get("session_id"): blockers.append(arm + " 缺少 SessionID")
            if not item.get("prompt_id"): blockers.append(arm + " 缺少 PromptID")
            if not item.get("commit_sha"): blockers.append(arm + " 缺少最终提交")
            check_status = (checks.get(arm) or {}).get("status")
            if check_status not in ("passed", "observed_failed"):
                blockers.append(arm + " Docker 产物验收尚未形成最终结论")
            rec = recs.get(arm) or {}
            if check_status == "passed":
                if rec.get("status") != "passed": blockers.append(arm + " 录像未通过")
                if not int(rec.get("commit_match") or 0): blockers.append(arm + " 录像与最终提交不匹配")
                if rec.get("review_status") != "confirmed": blockers.append(arm + " 录像尚未审核通过")
                if task.get("task_type") == "bugfix" and self._bug_fix_evidence(pair_id, arm).get("status") == "observed_failed":
                    blockers.append(arm + " 指定 Bug 独立验收失败，与产物通过结论冲突，须复核")
        delivery = detail.get("delivery") or {}
        if str(delivery.get("error") or "").startswith("待独立复核："):
            blockers.append("交付证据待独立复核，暂不可提交")
        review = detail.get("gsb") or {}
        if review.get("status") != "confirmed": blockers.append("GSB 尚未确认")
        verdict = str(review.get("final_verdict") or review.get("verdict") or "")
        a_score = int(review.get("a_score_delivery") or 0)
        b_score = int(review.get("b_score_delivery") or 0)
        if not delivery.get("remote_id"):
            g18_issue = self._g18_double_full_issue(pair_id, a_score, b_score)
            if g18_issue:
                blockers.append(g18_issue)
        if (a_score in range(1, 6) and b_score in range(1, 6)
                and ((verdict == "A better" and a_score < b_score)
                     or (verdict == "B better" and b_score < a_score))):
            blockers.append("GSB 优劣方向与交付评分相反，须独立复核后才能提交")
        blockers.extend(
            issue for issue in self._gsb_locator_issues(
                str(review.get("a_reason") or ""), str(review.get("b_reason") or "")
            ) if issue not in blockers
        )
        verdict, reason = str(review.get("verdict") or ""), str(review.get("reason") or "")
        version = self.gsb_evidence_version(pair_id, verdict, reason) if review else ""
        latest = self.db.one("SELECT * FROM gsb_rechecks WHERE pair_id=? ORDER BY created_at DESC LIMIT 1", (pair_id,))
        if not latest or latest.get("evidence_version") != version:
            warnings.append("尚未基于当前公开理由完成模型复检")
        elif latest.get("result_status") == "fact_conflict" and not latest.get("applied_at"):
            blockers.append("模型复检发现公开理由存在事实冲突")
        elif latest.get("result_status") == "suggested_revision" and not latest.get("applied_at"):
            warnings.append("模型复检给出了措辞修改建议")
        if include_platform:
            _, _, platform_blockers = self._solo_qa_material(detail)
            blockers.extend(issue for issue in platform_blockers if issue not in blockers)
        return {"pair_id": pair_id, "eligible": not blockers, "blockers": blockers, "warnings": warnings,
                "evidence_version": version, "checked_at": now_iso()}

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _trace_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts)
        return ""

    def _trace_first_user_prompt(self, path: Optional[Path]) -> str:
        """Read the first non-empty user turn exactly as the platform does."""
        if not path or not path.is_file():
            return ""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as source:
                for line in source:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    message = event.get("message") if isinstance(event.get("message"), dict) else {}
                    if event.get("type") != "user" and message.get("role") != "user":
                        continue
                    text = self._trace_text(message.get("content"))
                    if text.strip():
                        return text
        except OSError:
            return ""
        return ""

    def _paired_trace_prompts_match(self, task_prompt: str, left: str, right: str) -> bool:
        """Require the complete first User turn to match between both Arms.

        Only newline transport normalization is allowed. The database keeps
        the authored form while Claude's TUI may remove blank paragraph rows;
        fairness depends on A and B receiving identical text, not on those two
        storage representations having identical whitespace.
        """
        def normalize(value: str) -> str:
            value = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
            return value.rstrip("\n")

        left_prompt = normalize(left)
        right_prompt = normalize(right)
        return bool(left_prompt and left_prompt == right_prompt)

    def _restore_archived_trace(self, arm: Dict[str, Any]) -> Optional[Path]:
        """Restore a completed Arm's trace when a repair archived its runtime.

        Artifact repair archives the old container before opening a new session.
        If that repair is interrupted and the preserved commit is later resumed,
        the database can still point at the old runtime directory even though the
        exact trace now lives under ``claude-attempts``. Recovering that immutable
        trace avoids misclassifying a storage move as a prompt mismatch.
        """
        if str(arm.get("status") or "") != "completed":
            return None
        arm_id = str(arm.get("id") or "").strip()
        session_id = str(arm.get("session_id") or "").strip()
        if not arm_id or not session_id:
            return None
        archive_root = self.config.data_dir / "claude-attempts"
        candidates: List[Path] = []
        for attempt in archive_root.glob(arm_id + "-attempt-*"):
            candidates.extend((attempt / "traces").rglob(session_id + ".jsonl"))
        if not candidates:
            return None
        # A session id is immutable. Duplicate archive copies are harmless; use
        # the newest complete copy and restore its whole trace tree.
        source_file = max(candidates, key=lambda item: item.stat().st_mtime)
        source_root = source_file
        while source_root.name != "traces" and source_root != source_root.parent:
            source_root = source_root.parent
        if source_root.name != "traces":
            return None
        target_root = self.config.data_dir / "claude-runs" / arm_id / "traces"
        target_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_root, target_root, dirs_exist_ok=True)
        self.db.execute(
            "UPDATE arm_runs SET trace_path=?,updated_at=? WHERE id=? AND status='completed'",
            (str(target_root), now_iso(), arm_id),
        )
        self.db.audit("claude.archived_trace_restored", "arm_run", arm_id, {
            "session_id": session_id, "archive": str(source_root),
            "restored_to": str(target_root),
        })
        return target_root

    def _inspect_trace(self, arm: Dict[str, Any], prompt: str) -> tuple:
        issues: List[str] = []
        expected_prompts = [prompt]
        prompt_file = self.claude.runtime_dir / str(arm.get("id") or "") / "prompt.txt"
        try:
            sent_prompt = prompt_file.read_text(encoding="utf-8")
            marker = "[PAIRWISE_ARTIFACT_REPAIR]"
            before, separator, _ = sent_prompt.partition(marker)
            if separator and before.strip() == prompt.strip():
                expected_prompts.append(sent_prompt)
        except (OSError, UnicodeError):
            pass
        session_id = str(arm.get("session_id") or "").strip()
        root = Path(str(arm.get("trace_path") or "")).expanduser().resolve()
        allowed_root = (self.config.data_dir / "claude-runs").resolve()
        if session_id and (allowed_root not in root.parents or not root.is_dir()):
            restored = self._restore_archived_trace(arm)
            if restored:
                root = restored.resolve()
        if not session_id or allowed_root not in root.parents or not root.is_dir():
            return None, "", [str(arm.get("arm") or "?") + " 轨迹目录无效"]
        matches = list(root.rglob(session_id + ".jsonl"))
        if len(matches) != 1:
            return None, "", [str(arm.get("arm") or "?") + " 未找到唯一的 SessionID 轨迹文件"]
        path = matches[0].resolve()
        if path.stat().st_size > 27 * 1024 * 1024:
            issues.append(str(arm.get("arm") or "?") + " 轨迹文件超过本期 27 MB 上限")
        versions, sessions = set(), set()
        exact_prompt = False
        try:
            with path.open("r", encoding="utf-8", errors="replace") as source:
                for line in source:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("version"):
                        versions.add(str(event["version"]))
                    if event.get("sessionId"):
                        sessions.add(str(event["sessionId"]))
                    message = event.get("message") if isinstance(event.get("message"), dict) else {}
                    if ((event.get("type") == "user" or message.get("role") == "user")
                            and any(
                                self.claude._prompt_matches(
                                    expected, self._trace_text(message.get("content")),
                                )
                                for expected in expected_prompts
                            )):
                        exact_prompt = True
        except OSError as exc:
            issues.append(str(arm.get("arm") or "?") + " 轨迹读取失败：" + str(exc))
        if sessions and session_id not in sessions:
            issues.append(str(arm.get("arm") or "?") + " SessionID 与轨迹内容不一致")
        if not exact_prompt:
            issues.append(str(arm.get("arm") or "?") + " 轨迹中没有与题面逐字一致的首轮 User Prompt")
        version = next(iter(versions)) if len(versions) == 1 else ""
        if not version:
            issues.append(str(arm.get("arm") or "?") + " 轨迹无法确定唯一 Harness 版本")
        return path, version, issues

    def _schedule_missing_delivery_assessment(self) -> None:
        """Fill the new SOLO-QA fields without delaying GSB confirmation."""
        with self._future_lock:
            if any(
                key.startswith("delivery-assessment-") and not future.done()
                for key, future in self._futures.items()
            ):
                return
        running = self.db.one(
            "SELECT id FROM codex_jobs WHERE job_type='delivery_assessment' AND status='running' LIMIT 1"
        )
        if running:
            return
        rows = self.db.all(
            """SELECT g.pair_id FROM gsb_reviews g
                 JOIN pairs p ON p.id=g.pair_id
                 JOIN delivery_submissions d ON d.pair_id=p.id
                WHERE p.status='completed' AND g.status='confirmed'
                  AND d.status='ready_to_submit' AND d.remote_id=''
                  AND (g.a_score_delivery=0 OR g.b_score_delivery=0
                       OR g.a_desc_delivery='' OR g.b_desc_delivery='')
                ORDER BY p.completed_at DESC LIMIT 20"""
        )
        for row in rows:
            if self._submit_auto(
                "delivery-assessment-" + row["pair_id"],
                self.generate_delivery_assessment, row["pair_id"],
            ):
                return

    @staticmethod
    def _delivery_assessment_required_issues(review: Dict[str, Any]) -> List[str]:
        """Check fields required by the remote form, without prose heuristics."""
        issues: List[str] = []
        for arm in ("a", "b"):
            label = arm.upper()
            score = review.get(arm + "_score_delivery")
            if type(score) is not int or score not in range(1, 6):
                issues.append(label + " 交付完整性评分须为 1–5 的整数")
            if not str(review.get(arm + "_desc_delivery") or "").strip():
                issues.append(label + " 交付完整性描述不能为空")
        return issues

    @staticmethod
    def _strip_delivery_assessment_lead(description: str, arm: str) -> str:
        """Drop a standalone scoring preface when the actual arm evidence follows it."""
        lead, separator, remainder = description.partition("。")
        if (separator and re.match(r"^(?:以|按|判准|(?:交付)?完整性)", lead)
                and re.search(r"(?:判准|为准|判断|评定|完整性)", lead)
                and re.match(rf"^{arm}(?:\s|的|在|通过|实现|交付)", remainder.strip())):
            return remainder.strip()
        return description

    @classmethod
    def _delivery_assessment_issues(cls, review: Dict[str, Any]) -> List[str]:
        issues: List[str] = []
        reasons = [str(review.get("a_reason") or ""), str(review.get("b_reason") or ""),
                   str(review.get("reason") or "")]
        descriptions = []
        for arm in ("a", "b"):
            label = arm.upper()
            score = review.get(arm + "_score_delivery")
            description = re.sub(r"\s+", " ", str(review.get(arm + "_desc_delivery") or "")).strip()
            descriptions.append(description)
            if type(score) is not int or score not in range(1, 6):
                issues.append(label + " 交付完整性评分须为 1–5 的整数")
            if len(description) < 60:
                issues.append(label + " 交付完整性描述缺少充分的交付事实")
                continue
            if cls._strip_delivery_assessment_lead(description, label) != description:
                issues.append(label + " 交付完整性描述不应另起模板化判准开场白")
            if re.search(
                r"未见[^。；]{0,40}(?:声称|宣称|虚假完成|虚假成功|未落地|未交付|缺口|不符)",
                description,
            ):
                issues.append(label + " 交付完整性描述不能以笼统的无缺口保证代替验收事实")
            if not cls._gsb_has_locator(description):
                issues.append(label + " 交付完整性描述缺少文件、命令、接口或报错等可核对证据")
            if re.search(r"录像|录屏|视频|屏幕录制|recording|recorderEvents|capture_mode", description, re.I):
                issues.append(label + " 交付完整性描述不得引用录像作为依据")
            normalized = _comparison_text(description)
            if any(
                normalized and len(normalized) >= 60
                and SequenceMatcher(None, normalized, _comparison_text(reason)).ratio() >= 0.95
                for reason in reasons
            ):
                issues.append(label + " 交付完整性描述不能照抄 GSB 理由")
            if re.search(r"(?:A\s*比\s*B|B\s*比\s*A|A\s*更好|B\s*更好|\bSame\b)", description, re.I):
                issues.append(label + " 交付完整性描述不能写 A/B 胜负结论")
        if all(descriptions) and SequenceMatcher(
            None, _comparison_text(descriptions[0]), _comparison_text(descriptions[1]),
        ).ratio() >= 0.90:
            issues.append("A/B 交付完整性描述不能高度重复")
        return issues

    def update_delivery_assessment(
        self, pair_id: str, a_score: Any, a_description: Any,
        b_score: Any, b_description: Any, expected_updated_at: str,
        expected_assessment: Any,
    ) -> Dict[str, Any]:
        """Save the four local delivery fields without changing the GSB decision."""
        if not isinstance(expected_assessment, dict) or not expected_updated_at:
            raise ValueError("页面资料已变化，请刷新后再编辑完整性评估")
        descriptions = (a_description, b_description)
        if any(not isinstance(value, str) or len(value.strip()) > 800 for value in descriptions):
            raise ValueError("完整性描述须为不超过 800 字的文本")
        checks = {row["arm"]: row for row in self._require_evaluated_artifacts(pair_id)}
        scores = {"A": a_score, "B": b_score}
        for arm, score in scores.items():
            status = str(checks[arm].get("status") or "")
            if type(score) is not int or score not in range(1, 6):
                raise ValueError(arm + " 交付完整性评分须为 1–5 的整数")
            if status == "observed_failed" and score > 3:
                raise ValueError(arm + " 产物验收失败，交付完整性不能高于 3 分")
            if score == 5 and status != "passed":
                raise ValueError(arm + " 没有通过产物验收，交付完整性不能给 5 分")
            if score == 5 and self._bug_fix_evidence(pair_id, arm).get("status") == "not_verified":
                raise ValueError(arm + " 指定 Bug 未独立验证，交付完整性不能给 5 分")
        fields = ("a_score_delivery", "a_desc_delivery", "b_score_delivery", "b_desc_delivery")
        with self.db.transaction() as conn:
            row = conn.execute(
                """SELECT g.*,d.status submission_status,d.remote_id FROM gsb_reviews g
                   LEFT JOIN delivery_submissions d ON d.pair_id=g.pair_id WHERE g.pair_id=?""",
                (pair_id,),
            ).fetchone()
            if not row or row["status"] != "confirmed":
                raise ValueError("GSB 尚未确认，不能编辑交付完整性")
            if not ((row["submission_status"] == "ready_to_submit" and not row["remote_id"])
                    or row["submission_status"] == "needs_fix"):
                raise ValueError("只有本地待提交或待返修记录可编辑交付完整性")
            if row["updated_at"] != expected_updated_at or any(
                expected_assessment.get(field) != row[field] for field in fields
            ):
                raise ValueError("完整性评估已被其他操作修改，请刷新后重试")
            candidate = dict(row)
            candidate.update({
                "a_score_delivery": a_score, "a_desc_delivery": a_description.strip(),
                "b_score_delivery": b_score, "b_desc_delivery": b_description.strip(),
            })
            issues = self._delivery_assessment_issues(candidate)
            if issues:
                raise ValueError("；".join(issues))
            previous = {field: row[field] for field in fields}
            stamp = now_iso()
            conn.execute(
                """UPDATE gsb_reviews SET a_score_delivery=?,a_desc_delivery=?,
                   b_score_delivery=?,b_desc_delivery=?,updated_at=? WHERE pair_id=?""",
                (a_score, candidate["a_desc_delivery"], b_score,
                 candidate["b_desc_delivery"], stamp, pair_id),
            )
            conn.execute(
                """INSERT INTO audit_events(event_type,entity_type,entity_id,detail_json,created_at)
                   VALUES('delivery.assessment_edited','pair',?,?,?)""",
                (pair_id, json.dumps({"previous": previous,
                                      "current": {field: candidate[field] for field in fields}},
                                     ensure_ascii=False), stamp),
            )
        return self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,)) or {}

    def generate_delivery_assessment(self, pair_id: str) -> Dict[str, Any]:
        """Assess final deliverables independently of the comparative GSB prose."""
        detail = self.pair_detail(pair_id)
        review = detail.get("gsb") or {}
        delivery = detail.get("delivery") or {}
        if delivery.get("status") != "ready_to_submit" or delivery.get("remote_id"):
            raise ValueError("仅为本地待提交、未绑定远端记录的 Pair 生成新增字段")
        if review.get("status") != "confirmed":
            raise ValueError("GSB 尚未确认，不能生成交付完整性评估")
        if not self._delivery_assessment_issues(review):
            return review
        pair = self._pair(pair_id)
        task = detail.get("task") or {}
        arms = {row["arm"]: row for row in detail.get("arms", [])}
        checks = {row["arm"]: row for row in self._require_evaluated_artifacts(pair_id)}
        evidence = {}
        commits = {}
        for name in ("A", "B"):
            arm, check = arms.get(name) or {}, checks.get(name) or {}
            commits[name] = str(arm.get("commit_sha") or "")
            source = self._difficulty_arm_evidence(pair, arm, check)
            trace = source.get("traceEvidence") or {}
            trace["events"] = [
                {**event, "detail": str(event.get("detail") or "")[:350]}
                for event in (trace.get("events") or [])[-35:]
            ]
            source["traceEvidence"] = trace
            source["specifiedBugFix"] = self._bug_fix_evidence(pair_id, name)
            evidence[name] = source
        prompt = delivery_assessment_prompt(
            str(task.get("prompt") or ""), str(task.get("acceptance_json") or "[]"),
            json.dumps(evidence["A"], ensure_ascii=False),
            json.dumps(evidence["B"], ensure_ascii=False),
        )
        candidate: Dict[str, Any] = {}
        for attempt in range(2):
            result = self.codex.run(
                "delivery_assessment", prompt, DELIVERY_ASSESSMENT_SCHEMA,
                pair_id=pair_id, task_id=pair["task_id"], timeout=1800,
            )
            candidate = {**review}
            issues: List[str] = []
            for arm in ("a", "b"):
                prefix = arm.upper()
                candidate[arm + "_score_delivery"] = result.get(arm + "ScoreDelivery")
                candidate[arm + "_desc_delivery"] = self._strip_delivery_assessment_lead(
                    self._clean_gsb_part(result.get(arm + "DescDelivery"), 800), prefix,
                )
                check = checks.get(prefix) or {}
                score = candidate[arm + "_score_delivery"]
                if (check.get("status") == "observed_failed"
                        and type(score) is int and score > 3):
                    issues.append(prefix + " 产物验收失败，交付完整性不能高于 3 分")
                if score == 5 and check.get("status") != "passed":
                    issues.append(prefix + " 没有通过产物验收，交付完整性不能给 5 分")
                bug = evidence[prefix]["specifiedBugFix"]
                if bug.get("status") == "not_verified" and score == 5:
                    issues.append(prefix + " 指定 Bug 未独立验证，交付完整性不能给 5 分")
            issues.extend(self._delivery_assessment_issues(candidate))
            if not issues:
                break
            if attempt:
                raise ValueError("；".join(issues))
            prompt += (
                "\n上次输出未通过本地校验：" + "；".join(issues)
                + "\n请重新核对同一份证据，只输出新的完整评分和描述，不抬分、不补造验证。"
            )
        current = self.db.all("SELECT arm,commit_sha FROM arm_runs WHERE pair_id=?", (pair_id,))
        if {row["arm"]: row["commit_sha"] for row in current} != commits:
            raise ValueError("A/B 产物提交在评分期间变化，已丢弃旧评估")
        with self.db.transaction() as conn:
            changed = conn.execute(
                """UPDATE gsb_reviews SET a_score_delivery=?,a_desc_delivery=?,
                       b_score_delivery=?,b_desc_delivery=?,updated_at=?
                     WHERE pair_id=? AND status='confirmed'
                       AND EXISTS(SELECT 1 FROM delivery_submissions d
                                   WHERE d.pair_id=gsb_reviews.pair_id
                                     AND d.status='ready_to_submit' AND d.remote_id='')""",
                (candidate["a_score_delivery"], candidate["a_desc_delivery"],
                 candidate["b_score_delivery"], candidate["b_desc_delivery"],
                 now_iso(), pair_id),
            ).rowcount
        if not changed:
            raise ValueError("Pair 已不再是本地待提交状态，已丢弃旧评估")
        self.db.audit("delivery.assessment_completed", "pair", pair_id, {
            "aScore": candidate["a_score_delivery"],
            "bScore": candidate["b_score_delivery"], "commits": commits,
        })
        return self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,)) or {}

    def _solo_qa_material(self, detail: Dict[str, Any]) -> tuple:
        issues: List[str] = []
        issues.extend(self._pair_model_issues(detail))
        task = detail.get("task") or {}
        repo = detail.get("repository") or {}
        review = detail.get("gsb") or {}
        arms = {row["arm"]: row for row in detail.get("arms", [])}
        checks = {row["arm"]: row for row in detail.get("checks", [])}
        recs = {row["arm"]: row for row in detail.get("recordings", [])}
        task_types = {"zero_to_one": "0-1代码生成", "feature": "feature迭代", "bugfix": "Bug修复"}
        verdicts = {"A better": "A 更好", "Same": "Same", "B better": "B 更好"}
        task_type = task_types.get(str(task.get("task_type") or ""), "")
        if not task_type:
            issues.append("任务类型无法映射到本期 GSB 表单")
        difficulty = str(task.get("difficulty") or "")
        if not task_difficulty_allowed(str(task.get("task_type") or ""), difficulty):
            issues.append("新提交题目的实际难度只允许困难或地狱")
        prompt = str(task.get("prompt") or "")
        if not prompt:
            issues.append("缺少完整 User Prompt")
        remote = str(repo.get("remote_url") or "").removesuffix(".git")
        main_sha = str(repo.get("main_sha") or detail.get("baseline_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", main_sha):
            issues.append("初始环境快照不是 40 位完整 SHA")
        if not re.fullmatch(r"https://github\.com/[^/]+/[^/]+", remote):
            issues.append("缺少有效的 GitHub 仓库地址")
        files: Dict[str, Dict[str, Any]] = {}
        versions: Dict[str, str] = {}
        trace_prompts: Dict[str, str] = {}
        for arm_name in ("A", "B"):
            arm = arms.get(arm_name) or {"arm": arm_name}
            trace, version, trace_issues = self._inspect_trace(arm, prompt)
            issues.extend(trace_issues)
            versions[arm_name] = version
            if trace:
                trace_prompts[arm_name] = self._trace_first_user_prompt(trace)
                files[arm_name.lower() + "_trace_file"] = {
                    "name": trace.name, "path": str(trace), "size": trace.stat().st_size,
                    "sha256": self._sha256_file(trace), "content_type": "application/x-ndjson",
                }
            commit_sha = str(arm.get("commit_sha") or "")
            if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
                issues.append(arm_name + " 产物快照不是 40 位完整 SHA")
            workspace = Path(str(arm.get("workspace_path") or "")).expanduser().resolve()
            if commit_sha and workspace.is_dir():
                parent = run_command(
                    ["git", "rev-parse", commit_sha + "^"],
                    cwd=workspace, check=False, timeout=15,
                )
                if parent.returncode != 0 or parent.stdout.strip() != main_sha:
                    issues.append(arm_name + " 产物快照的父提交不是初始环境快照")
                if (re.fullmatch(r"[0-9a-f]{40}", main_sha)
                        and re.fullmatch(r"[0-9a-f]{40}", commit_sha)):
                    try:
                        summary = self.git.delivery_diff_summary(workspace, main_sha, commit_sha)
                    except Exception as exc:
                        issues.append(arm_name + " 无法核对产物有效源码改动：" + redact(str(exc)))
                    else:
                        if summary["generated_files"]:
                            roots = "、".join(summary["generated_roots"][:3])
                            issues.append(
                                arm_name + " 产物快照包含生成依赖目录（%s），禁止提交" % roots
                            )
                        if summary["source_line_changes"] < 10:
                            issues.append(
                                "%s 相对初始环境的有效源码改动仅 %d 行，不足平台要求的 10 行"
                                % (arm_name, summary["source_line_changes"])
                            )
            check_status = str((checks.get(arm_name) or {}).get("status") or "")
            if not check_status:
                issues.append(arm_name + " 缺少 Docker 产物验收结果")
            rec = recs.get(arm_name) or {}
            video = Path(str(rec.get("path") or "")).expanduser().resolve()
            recording_root = (self.config.data_dir / "recordings").resolve()
            if check_status != "passed" and not video.is_file():
                continue
            if recording_root not in video.parents or not video.is_file():
                issues.append(arm_name + " 录像文件不存在")
            elif video.suffix.lower() not in (".mp4", ".mov", ".webm", ".m4v"):
                issues.append(arm_name + " 录像格式不受本期平台支持")
            elif video.stat().st_size > 500 * 1024 * 1024:
                issues.append(arm_name + " 录像超过本期 500 MB 上限")
            else:
                files[arm_name.lower() + "_video"] = {
                    "name": video.name, "path": str(video), "size": video.stat().st_size,
                    "sha256": str(rec.get("sha256") or self._sha256_file(video)),
                    "content_type": mimetypes.guess_type(str(video))[0] or "video/mp4",
                }
        if versions.get("A") and versions.get("B") and versions["A"] != versions["B"]:
            issues.append("A/B Harness 版本不一致")
        if (trace_prompts.get("A") and trace_prompts.get("B")
                and not self._paired_trace_prompts_match(
                    prompt, trace_prompts["A"], trace_prompts["B"],
                )):
            issues.append("A/B 轨迹里的完整首轮 User Prompt 不一致")
        a_session = str((arms.get("A") or {}).get("session_id") or "")
        b_session = str((arms.get("B") or {}).get("session_id") or "")
        a_model_name = str((arms.get("A") or {}).get("model") or "")
        b_model_name = str((arms.get("B") or {}).get("model") or "")
        if not a_model_name:
            issues.append("A 缺少实际运行模型名称")
        if not b_model_name:
            issues.append("B 缺少实际运行模型名称")
        if a_session and a_session == b_session:
            issues.append("A/B 必须使用不同 SessionID")
        verdict = verdicts.get(str(review.get("verdict") or ""), "")
        if not verdict:
            issues.append("GSB 结论无法映射到本期表单")
        reason = str(review.get("reason") or "").replace("`", "").strip()
        if len(reason) < 60:
            issues.append("GSB 理由不足 60 字")
        if "A：" not in reason or "B：" not in reason:
            issues.append("GSB 理由必须分别包含 A、B 评价")
        delivery = detail.get("delivery") or {}
        required_assessment_issues = self._delivery_assessment_required_issues(review)
        if delivery.get("status") == "ready_to_submit" and not delivery.get("remote_id"):
            issues.extend(self._delivery_assessment_issues(review))
        else:
            issues.extend(required_assessment_issues)
        issues.extend(
            issue for issue in self._gsb_locator_issues(
                str(review.get("a_reason") or ""), str(review.get("b_reason") or "")
            ) if issue not in issues
        )
        languages = normalize_stack(task.get("stack"))
        if not languages:
            issues.append("语言/框架缺少主要编程语言或应用框架")
        failed_artifacts = [
            arm for arm in ("A", "B")
            if str((checks.get(arm) or {}).get("status") or "") not in ("", "passed")
        ]
        cutoff = str(self.db.setting("dockerless_task_policy_started_at", "") or "")
        future_self_contained = bool(cutoff and str(task.get("created_at") or "") >= cutoff)
        if future_self_contained:
            for arm_name in ("A", "B"):
                try:
                    evidence = json.loads(str((checks.get(arm_name) or {}).get("checks_json") or "[]"))
                except (TypeError, ValueError):
                    evidence = []
                if not any(isinstance(item, dict) and item.get("name") == "self_contained_source"
                           and item.get("passed") for item in evidence if isinstance(evidence, list)):
                    issues.append(arm_name + " 尚未证明代码本身无外部服务依赖，不能选无外部依赖验收")
        values = {
            "user_prompt": prompt,
            "question_type": task_type,
            "difficulty": difficulty,
            "languages": languages,
            "harness": "Claude Code",
            "harness_version": versions.get("A") or versions.get("B") or "",
            "os_platform": "MacOS/Linux",
            # This is a fixed-choice platform field.  Per-Arm product
            # failures belong in remark and GSB evidence; appending them to
            # the choice text makes an otherwise valid payload impossible to
            # submit.
            "repro_level": "无外部依赖" if future_self_contained else "已容器化，可一键起环境",
            "env_snapshot": remote + "/commit/" + main_sha if remote and main_sha else "",
            "a_session_id": a_session,
            "a_model_name": a_model_name,
            "a_prompt_id": str((arms.get("A") or {}).get("prompt_id") or ""),
            "a_artifact_snapshot": remote + "/commit/" + str((arms.get("A") or {}).get("commit_sha") or "") if remote else "",
            "b_session_id": b_session,
            "b_model_name": b_model_name,
            "b_prompt_id": str((arms.get("B") or {}).get("prompt_id") or ""),
            "b_artifact_snapshot": remote + "/commit/" + str((arms.get("B") or {}).get("commit_sha") or "") if remote else "",
            "gsb_verdict": verdict,
            "gsb_reason": reason,
            "validity": "有效",
            "remark": (
                "Docker 验收失败侧未启动 Claude 返修，GSB 按最终提交和失败证据如实评价。"
                if failed_artifacts else ""
            ),
        }
        if not required_assessment_issues:
            values.update({
                "a_score_delivery": int(review["a_score_delivery"]),
                "a_desc_delivery": str(review["a_desc_delivery"]),
                "b_score_delivery": int(review["b_score_delivery"]),
                "b_desc_delivery": str(review["b_desc_delivery"]),
            })
        return values, files, issues

    def solo_qa_payload(self, pair_id: str) -> Dict[str, Any]:
        detail = self.pair_detail(pair_id)
        check = self.delivery_preflight(pair_id, include_platform=True)
        values, files, platform_issues = self._solo_qa_material(detail)
        for key, meta in files.items():
            meta["url"] = "/api/solo-qa/pairs/%s/files/%s" % (pair_id, key)
        payload_identity = {
            "pair_id": pair_id,
            "values": values,
            "files": {key: {k: v for k, v in meta.items() if k != "path"} for key, meta in files.items()},
        }
        payload_sha256 = hashlib.sha256(json.dumps(payload_identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return {
            **payload_identity,
            "ready": bool(check["eligible"]),
            "issues": list(dict.fromkeys(check["blockers"] + platform_issues)),
            "warnings": check["warnings"],
            "payload_sha256": payload_sha256,
            "solo_qa": detail.get("delivery") or {},
        }

    def solo_qa_file(self, pair_id: str, field_key: str) -> Dict[str, Any]:
        detail = self.pair_detail(pair_id)
        _, files, _ = self._solo_qa_material(detail)
        item = files.get(field_key)
        if not item:
            raise KeyError("提交文件不存在")
        return item

    def update_solo_qa_state(self, values: Dict[str, Any]) -> Dict[str, Any]:
        pair_id = str(values.get("pair_id") or "")
        self._pair(pair_id)
        allowed = {"ready_to_submit", "submitting", "qc_pending", "qc_passed", "needs_fix", "discarded", "failed"}
        status = str(values.get("status") or "")
        if status not in allowed:
            raise ValueError("提交状态无效")
        stamp = now_iso()
        submission_id = "delivery-" + uuid.uuid4().hex[:16]
        cleaned = lambda key, limit: str(values.get(key) or "")[:limit]
        current = self.db.one("SELECT * FROM delivery_submissions WHERE pair_id=?", (pair_id,)) or {}
        incoming_remote_id = cleaned("remote_id", 128)
        current_remote_id = str(current.get("remote_id") or "")
        if status == "submitting" and not (incoming_remote_id or current_remote_id):
            review = self.db.one(
                "SELECT a_score_delivery,b_score_delivery FROM gsb_reviews WHERE pair_id=?",
                (pair_id,),
            ) or {}
            quota_issue = self._g18_double_full_issue(
                pair_id, int(review.get("a_score_delivery") or 0),
                int(review.get("b_score_delivery") or 0),
            )
            if quota_issue:
                raise ValueError(quota_issue)
        if current_remote_id and incoming_remote_id and incoming_remote_id != current_remote_id:
            raise ValueError(
                "该 Pair 已绑定 SOLO-QA #%s，禁止改绑为 #%s；请同步原记录或走返修"
                % (current_remote_id, incoming_remote_id)
            )
        if status == "submitting" and current.get("status") == "submitting":
            try:
                updated = datetime.fromisoformat(str(current.get("updated_at") or ""))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                active_seconds = (datetime.now(timezone.utc) - updated).total_seconds()
            except ValueError:
                active_seconds = 0
            if active_seconds < 15 * 60:
                raise ValueError("该 Pair 已有提交正在进行，已拦截重复上传")
        remote_id = incoming_remote_id or current_remote_id
        remote_url = cleaned("remote_url", 1000) or str(current.get("remote_url") or "")
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,remote_id,remote_url,payload_sha256,
                 remote_status,qc_summary,remote_updated_at,error,submitted_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(pair_id) DO UPDATE SET
                 status=excluded.status,remote_id=excluded.remote_id,remote_url=excluded.remote_url,
                 payload_sha256=excluded.payload_sha256,remote_status=excluded.remote_status,
                 qc_summary=excluded.qc_summary,remote_updated_at=excluded.remote_updated_at,
                 error=excluded.error,submitted_at=CASE WHEN excluded.submitted_at IS NOT NULL
                   THEN excluded.submitted_at ELSE delivery_submissions.submitted_at END,
                 updated_at=excluded.updated_at""",
            (submission_id, pair_id, status, remote_id, remote_url,
             cleaned("payload_sha256", 64), cleaned("remote_status", 64), cleaned("qc_summary", 2000),
             cleaned("remote_updated_at", 128), cleaned("error", 2000),
             cleaned("submitted_at", 128) or None, stamp, stamp),
        )
        self.db.audit("solo_qa.state", "pair", pair_id, {"status": status, "remote_id": remote_id})
        return self.db.one("SELECT * FROM delivery_submissions WHERE pair_id=?", (pair_id,)) or {}

    def set_delivery_hidden(self, pair_id: str, hidden: bool) -> Dict[str, Any]:
        self._pair(pair_id)
        stamp = now_iso()
        submission_id = "delivery-" + uuid.uuid4().hex[:16]
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,hidden_at,created_at,updated_at)
               VALUES(?,?,'not_submitted',?,?,?) ON CONFLICT(pair_id) DO UPDATE SET
               hidden_at=excluded.hidden_at,updated_at=excluded.updated_at""",
            (submission_id, pair_id, stamp if hidden else None, stamp, stamp),
        )
        self.db.audit("delivery.hidden" if hidden else "delivery.restored", "pair", pair_id, {})
        return self.db.one("SELECT * FROM delivery_submissions WHERE pair_id=?", (pair_id,)) or {}

    def submit_delivery(self, pair_id: str) -> Dict[str, Any]:
        raise ValueError("正式提交必须通过 Chrome 提交小助手上传到 SOLO-QA，不能只在本地登记")

    def pair_detail(self, pair_id: str) -> Dict[str, Any]:
        self.refresh_recording_stage(pair_id)
        pair = self._pair(pair_id)
        pair["task"] = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],))
        pair["repository"] = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,))
        pair["arms"] = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        pair["checks"] = self._current_artifact_checks(pair_id)
        pair["difficulty_review"] = self.db.one(
            "SELECT * FROM difficulty_reviews WHERE pair_id=?", (pair_id,)
        )
        pair["recordings"] = self.db.all("SELECT * FROM recordings WHERE pair_id=? ORDER BY arm", (pair_id,))
        pair["recording_attempts"] = self.db.all(
            "SELECT * FROM recording_attempts WHERE pair_id=? ORDER BY created_at DESC", (pair_id,)
        )
        pair["gsb"] = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,))
        if pair["gsb"]:
            pair["gsb"].pop("preference_reason", None)
        pair["gsb_rechecks"] = self.db.all("SELECT * FROM gsb_rechecks WHERE pair_id=? ORDER BY created_at DESC", (pair_id,))
        for recheck in pair["gsb_rechecks"]:
            recheck.pop("suggested_preference_reason", None)
        pair["delivery"] = self.db.one("SELECT * FROM delivery_submissions WHERE pair_id=?", (pair_id,))
        return pair

    @staticmethod
    def _is_claude_api_error(error: str) -> bool:
        text = str(error or "").casefold()
        return "api error" in text or "litellm" in text

    @staticmethod
    def _is_claude_gateway_timeout_error(error: str) -> bool:
        """Return true for an explicit upstream 504/gateway timeout."""
        text = str(error or "").casefold()
        return bool(
            re.search(r"(?:^|\D)504(?:\D|$)", text)
            or "gateway time-out" in text
            or "gateway timeout" in text
        )

    @classmethod
    def _claude_api_error_signature(cls, error: str) -> str:
        """Group repainting API errors without losing distinct failures.

        The terminal-only 504 detector includes the current trace idle age in
        its message.  Comparing that full message makes every five-second
        monitor pass look like a new failure and floods the audit log.  Keep
        the first detailed message, but deduplicate subsequent observations by
        the stable provider failure class.
        """
        text = str(error or "")
        lowered = text.casefold()
        if cls._is_claude_rate_limit_error(text):
            return "rate_limit"
        if cls._is_claude_gateway_timeout_error(text):
            return "gateway_timeout"
        if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
            return "certificate"
        if "connection" in lowered or "connecterror" in lowered:
            return "connection"
        return re.sub(r"\d+", "#", lowered).strip()[-1000:]

    def _record_pair_development_failure(self, pair_id: str, arm_id: str,
                                         error: str, failure_kind: str) -> Tuple[int, int, bool]:
        """Count every failed development run against one Pair-wide budget."""
        maximum = max(1, int(self.db.setting("development_max_attempts", 2)))
        stamp = now_iso()
        with self.db.transaction() as conn:
            pair = conn.execute(
                "SELECT status,stage,development_failure_count FROM pairs WHERE id=?",
                (pair_id,),
            ).fetchone()
            if not pair:
                raise KeyError(pair_id)
            current = int(pair["development_failure_count"] or 0)
            if pair["status"] in ("failed", "cancelled") or pair["stage"] in (
                "task_replacement", "replaced", "replacement_failed",
            ):
                return current, maximum, current >= maximum
            already_exhausted = current >= maximum
            # A peer that was already running is allowed to finish naturally
            # after the shared budget is exhausted.  Its eventual failure is
            # still terminal evidence, but must not make the Pair counter
            # exceed the configured maximum.
            current = min(maximum, current + 1)
            conn.execute(
                "UPDATE pairs SET development_failure_count=?,updated_at=? WHERE id=?",
                (current, stamp, pair_id),
            )
            conn.execute(
                """INSERT INTO audit_events(event_type,entity_type,entity_id,detail_json,created_at)
                   VALUES('claude.attempt_failed','arm_run',?,?,?)""",
                (arm_id, json.dumps({
                    "pair_id": pair_id,
                    "pair_failure_count": current,
                    "maximum": maximum,
                    "failure_kind": failure_kind,
                    "error": redact(error)[-1000:],
                    "counts_toward_pair_failure_limit": True,
                    "pair_failure_budget_already_exhausted": already_exhausted,
                }, ensure_ascii=False), stamp),
            )
        return current, maximum, current >= maximum

    def _pair_failure_lock(self, pair_id: str) -> threading.RLock:
        """Serialize A/B failure decisions so two simultaneous errors count once each."""
        with self._failure_locks_lock:
            return self._failure_locks.setdefault(pair_id, threading.RLock())

    @staticmethod
    def _is_claude_rate_limit_error(error: str) -> bool:
        """Return true for Claude/provider rate-limit failures."""
        text = str(error or "").casefold()
        return bool(
            re.search(r"(?:^|\D)429(?:\D|$)", text)
            or "ratelimiterror" in text
            or "rate limit" in text
            or "max_parallel_requests" in text
        )

    @staticmethod
    def _is_claude_user_rpm_error(error: str) -> bool:
        """Match the user's per-minute quota error, not every recoverable 429."""
        text = str(error or "").casefold()
        return "user_rpm_exceeded" in text or (
            "rate limit exceeded for current user" in text and "rpm limit" in text
        )

    def _user_rpm_fast_fail_active(self, error_at: str) -> bool:
        """Apply the new rule only to native errors emitted after activation."""
        enabled_at = str(self.db.setting("claude_user_rpm_fast_fail_after", "") or "")
        if not enabled_at or not error_at:
            return False
        try:
            return datetime.fromisoformat(error_at.replace("Z", "+00:00")) >= datetime.fromisoformat(
                enabled_at.replace("Z", "+00:00")
            )
        except ValueError:
            return False

    def _rate_limit_restart_cooldown(self, arm_id: str) -> Tuple[int, int]:
        """Back off repeated non-counting 429 retries without consuming attempts."""
        recent = int((self.db.one(
            """SELECT COUNT(*) count FROM audit_events
               WHERE event_type='claude.rate_limit_turn_ended' AND entity_id=?
                 AND created_at>=datetime('now','-1 hour')""",
            (arm_id,),
        ) or {"count": 0})["count"])
        ordinal = recent + 1
        base = max(5, int(self.db.setting("rate_limit_retry_base_seconds", 60)))
        maximum = max(base, int(self.db.setting("rate_limit_retry_max_seconds", 900)))
        return ordinal, min(maximum, base * (2 ** min(ordinal - 1, 10)))

    @staticmethod
    def _api_retry_delay_seconds(error: str, previous_retries: int,
                                 current: Optional[datetime] = None) -> int:
        """Return a bounded cooldown without consuming a development attempt."""
        now = current or datetime.now(timezone.utc)
        lowered = str(error or "").casefold()
        exponent = min(max(0, int(previous_retries)), 4)
        if "429" in lowered or "rate limit" in lowered or "rate_limit" in lowered:
            delay = 60 * (2 ** exponent)
            match = re.search(
                r"resets at:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*utc",
                str(error or ""), re.IGNORECASE,
            )
            if match:
                reset_at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                # The provider supplies the authoritative recovery time.
                delay = max(1, int((reset_at - now).total_seconds()))
        elif "504" in lowered or "gateway" in lowered:
            delay = 120 * (2 ** exponent)
        else:
            delay = 180 * (2 ** exponent)
        return max(1, min(900, delay))

    def _queue_api_retry(self, pair_id: str, arm: Dict[str, Any],
                         error: str) -> Dict[str, Any]:
        with self._pair_failure_lock(pair_id):
            return self._queue_api_retry_locked(pair_id, arm, error)

    def _queue_api_retry_locked(self, pair_id: str, arm: Dict[str, Any],
                                error: str) -> Dict[str, Any]:
        """Archive an API error after its current no-code window has expired."""
        arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or arm
        if arm.get("status") == "waiting_api_retry":
            return arm
        failure_count, maximum, exhausted = self._record_pair_development_failure(
            pair_id, arm["id"], error, "api_error",
        )
        if exhausted:
            archived = self.claude.archive_failed_attempt(
                arm, error, prepare_retry=False,
                count_development_failure=False, count_error_retry=False,
            )
            label = "项目累计 %d 次失败（包含 API 错误）" % maximum
            self.db.audit("claude.api_retry_exhausted", "arm_run", arm["id"], {
                "pair_id": pair_id, "pair_failure_count": failure_count,
                "maximum": maximum, "error": redact(error)[-1000:],
            })
            self._retire_after_peer_finishes(
                pair_id, arm["id"], error, label,
            )
            return archived
        previous_retries = int(arm.get("api_retry_count") or 0)
        delay = self._api_retry_delay_seconds(error, previous_retries)
        retry_after = datetime.now(timezone.utc) + timedelta(seconds=delay)
        self._register_global_rate_limit(error, retry_after)
        prepared = self.claude.archive_failed_attempt(
            arm, error, prepare_retry=True,
            count_development_failure=False, count_error_retry=False,
        )
        stamp = now_iso()
        self.db.execute(
            """UPDATE arm_runs SET status='waiting_api_retry',api_retry_count=?,
               api_retry_after=?,last_api_error=?,error=?,updated_at=? WHERE id=?""",
            (previous_retries + 1, retry_after.isoformat(timespec="seconds"),
             redact(error)[-3000:], redact(error)[-2000:], stamp, arm["id"]),
        )
        active_other = int((self.db.one(
            """SELECT COUNT(*) count FROM arm_runs WHERE pair_id=? AND id<>?
                 AND status IN ('queued','running','developing','waiting_retry','checkpointing','exported')""",
            (pair_id, arm["id"]),
        ) or {"count": 0})["count"])
        if not active_other:
            self.db.execute(
                """UPDATE pairs SET status='waiting_api_retry',stage='development',error=?,updated_at=?
                     WHERE id=? AND stage='development'""",
                ("Claude API 暂时不可用，已保留现场并等待自动重试", stamp, pair_id),
            )
        self.db.audit("claude.api_retry_queued", "arm_run", arm["id"], {
            "pair_id": pair_id, "retry_number": previous_retries + 1,
            "pair_failure_count": failure_count, "maximum": maximum,
            "retry_after": retry_after.isoformat(timespec="seconds"),
            "cooldown_seconds": delay, "error": redact(error)[-1000:],
            "prompt_mode": "fresh_session_same_original_prompt_once",
            "counts_toward_development_attempts": False,
            "counts_toward_error_retries": False,
            "counts_toward_pair_failure_limit": True,
        })
        return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or prepared

    def _recover_api_retry(self, pair_id: str, arm_id: str, prompt: str) -> Dict[str, Any]:
        """Start a clean first-turn session after a transient API cooldown."""
        arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or {}
        if not arm or arm.get("status") != "waiting_api_retry":
            return arm
        if self.db.setting("pipeline_drain", False):
            return arm
        if self._pair_development_budget_exhausted(pair_id):
            self._finish_exhausted_pair_after_peer(pair_id)
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
        blocked = self._abort_development_restart(
            pair_id, arm_id, "Pair 已进入换题或失败终态，取消 API 自动重试",
        )
        if blocked:
            return blocked
        retry_at = str(arm.get("api_retry_after") or "")
        if retry_at and retry_at > now_iso():
            return arm
        if self._available_development_arm_slots() <= 0:
            return arm
        pair = self._pair(pair_id)
        retry_number = int(arm.get("api_retry_count") or 1)
        try:
            canonical = self.git.reset_arm_to_baseline(pair_id, str(arm["arm"]))
            self.db.execute(
                "UPDATE arm_runs SET status='waiting_retry',updated_at=? WHERE id=?",
                (now_iso(), arm_id),
            )
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            if self._pair_development_budget_exhausted(pair_id):
                self._finish_exhausted_pair_after_peer(pair_id)
                return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            reservation = self._launch_arm_if_capacity(arm)
            if reservation is None:
                return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            if not reservation:
                deferred = self._mark_arm_waiting_for_capacity(
                    arm_id, str(arm.get("error") or ""),
                )
                self.db.execute(
                    "UPDATE arm_runs SET status='waiting_api_retry',api_retry_after=?,updated_at=? WHERE id=?",
                    (now_iso(), now_iso(), arm_id),
                )
                return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or deferred
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            self.claude.wait_until_ready(arm)
            self.claude.materialize_repository(arm, canonical, pair["baseline_sha"])
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            self._send_prompt_with_pair_stagger(pair_id, arm, prompt)
            stamp = now_iso()
            self.db.execute(
                """UPDATE arm_runs SET api_retry_after=NULL,last_api_error='',error='',updated_at=?
                     WHERE id=?""",
                (stamp, arm_id),
            )
            self.db.execute(
                """UPDATE pairs SET status='running',stage='development',error='',updated_at=?
                     WHERE id=?""",
                (stamp, pair_id),
            )
            self.db.audit("claude.api_retry_started", "arm_run", arm_id, {
                "pair_id": pair_id, "retry_number": retry_number,
                "attempt": int(arm.get("attempt_no") or 1),
                "prompt_mode": "fresh_session_same_original_prompt_once",
                "counts_toward_development_attempts": False,
                "counts_toward_error_retries": False,
            })
            # Use the canonical monitor operation id. Calling _monitor_arm
            # directly here allowed the scheduler to attach a second monitor
            # to the same session, so both workers archived one 429 twice.
            self._submit_monitor(
                "monitor-" + arm_id, self._monitor_arm,
                pair_id, arm_id, prompt,
            )
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
        except Exception as exc:
            current_arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            # The previous API failure was already counted when its no-code
            # window ended. A failed *restart* (container, Screen, prompt, or
            # repository setup) is infrastructure, not another development
            # failure. Reuse the bounded infrastructure restart/pause path.
            return self._handle_attempt_failure(
                pair_id, current_arm, prompt,
                "全新 Session 基础设施重试失败：%s" % redact(str(exc)),
            )

    def _pair_blocks_development_restart(self, pair: Dict[str, Any]) -> bool:
        if str(pair.get("status") or "") in ("failed", "cancelled") or str(
            pair.get("stage") or ""
        ) in ("task_replacement", "replaced", "replacement_failed"):
            return True
        pair_id = str(pair.get("id") or "")
        if not pair_id:
            return False
        return bool(self.db.one(
            """SELECT 1 FROM audit_events
                 WHERE entity_type='pair' AND entity_id=?
                   AND event_type='pair.prompt_mismatch_retry_cancelled_by_user'
                 LIMIT 1""",
            (pair_id,),
        ))

    def _abort_development_restart(self, pair_id: str, arm_id: str,
                                   reason: str) -> Optional[Dict[str, Any]]:
        pair = self._pair(pair_id)
        if not self._pair_blocks_development_restart(pair):
            return None
        arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or {}
        if arm.get("status") in ("queued", "running", "developing", "waiting_retry", "waiting_api_retry", "checkpointing"):
            stamp = now_iso()
            self.db.execute(
                "UPDATE arm_runs SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?",
                (reason[-2000:], stamp, stamp, arm_id),
            )
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
        arm["restart_skipped_terminal_pair"] = True
        self.db.audit("claude.restart_skipped_terminal_pair", "arm_run", arm_id, {
            "pair_id": pair_id, "pair_status": pair.get("status"),
            "pair_stage": pair.get("stage"), "reason": reason[-1000:],
        })
        return arm

    @staticmethod
    def _is_prompt_delivery_error(error: str) -> bool:
        text = str(error or "")
        return any(token in text for token in (
            "轨迹首轮 User Prompt",
            "发送完整题面",
            "粘贴题面失败",
            "题面已粘贴但提交失败",
            "题面提交后未形成可验证轨迹",
        ))

    @classmethod
    def _is_session_infrastructure_error(cls, error: str) -> bool:
        """Identify failures before a replacement session reaches development.

        Repository materialization and fresh-session launch failures happen
        before Claude receives the task.  They may require a new container,
        but they must not consume the Pair-wide development failure budget.
        """
        text = str(error or "")
        return cls._is_prompt_delivery_error(text) or any(token in text for token in (
            "恢复全新 Session 失败",
            "启动全新 Session 仍失败",
            "全新 Session 基础设施重试失败",
        ))

    def _consecutive_prompt_infrastructure_failures(self, arm_id: str) -> int:
        """Count the latest uninterrupted prompt/trace infrastructure errors."""
        limit = max(2, int(self.db.setting("prompt_infrastructure_retry_limit", 2)))
        rows = self.db.all(
            """SELECT detail_json FROM audit_events
               WHERE event_type='claude.attempt_failed' AND entity_id=?
               ORDER BY id DESC LIMIT ?""",
            (arm_id, limit),
        )
        count = 0
        for row in rows:
            try:
                previous_error = str(json.loads(row.get("detail_json") or "{}").get("error") or "")
            except (TypeError, ValueError):
                break
            if not self._is_session_infrastructure_error(previous_error):
                break
            count += 1
        return count

    def _restart_arm_from_baseline(self, pair_id: str, arm: Dict[str, Any], prompt: str,
                                   error: str, count_development_failure: bool = True,
                                   count_error_retry: bool = True) -> Dict[str, Any]:
        if self._pair_development_budget_exhausted(pair_id):
            self._finish_exhausted_pair_after_peer(pair_id)
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or arm
        blocked = self._abort_development_restart(
            pair_id, arm["id"], "Pair 已进入换题或失败终态，取消启动新的 Claude Session",
        )
        if blocked:
            return blocked
        pair = self._pair(pair_id)
        if pair.get("stage") != "development":
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or arm
        self._invalidate_recordings(pair_id, [arm.get("arm")], error)
        restarted = self.claude.archive_failed_attempt(
            arm, error, prepare_retry=True,
            count_development_failure=count_development_failure,
            count_error_retry=count_error_retry,
        )
        if self.db.setting("pipeline_drain", False):
            self.db.execute(
                "UPDATE arm_runs SET status='waiting_retry',error=?,updated_at=? WHERE id=?",
                (redact(error)[-2000:], now_iso(), arm["id"]),
            )
            self.db.audit("claude.restart_paused_for_pipeline_drain", "arm_run", arm["id"], {
                "pair_id": pair_id, "reason": redact(error)[-1000:],
                "failure_count_preserved": True,
            })
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        canonical = self.git.reset_arm_to_baseline(pair_id, str(arm["arm"]))
        lowered = error.casefold()
        delay = 20 if any(token in lowered for token in ("429", "504", "rate limit", "rate_limit")) else 8
        self.db.execute(
            "UPDATE arm_runs SET status='waiting_retry',error=?,updated_at=? WHERE id=?",
            (redact(error)[-2000:], now_iso(), arm["id"]),
        )
        time.sleep(delay)
        blocked = self._abort_development_restart(
            pair_id, arm["id"], "准备重跑期间 Pair 已进入换题或失败终态，取消启动新的 Claude Session",
        )
        if blocked:
            return blocked
        restarted = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        if self._pair_development_budget_exhausted(pair_id):
            self._finish_exhausted_pair_after_peer(pair_id)
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        current_pair = self.db.one("SELECT stage FROM pairs WHERE id=?", (pair_id,)) or {}
        if current_pair.get("stage") != "development":
            return restarted
        reservation = self._launch_arm_if_capacity(restarted)
        if reservation is None:
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        if not reservation:
            return self._mark_arm_waiting_for_capacity(arm["id"], error)
        restarted = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        self.claude.wait_until_ready(restarted)
        self.claude.materialize_repository(restarted, canonical, pair["baseline_sha"])
        restarted = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        current_pair = self.db.one("SELECT stage FROM pairs WHERE id=?", (pair_id,)) or {}
        if current_pair.get("stage") != "development":
            self.claude.reset_unsent_arm(restarted)
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        self._send_prompt_with_pair_stagger(pair_id, restarted, prompt)
        self.db.execute(
            "UPDATE pairs SET status='running',error='',updated_at=? WHERE id=? AND stage='development'",
            (now_iso(), pair_id),
        )
        self.db.audit("claude.arm_restarted_after_error", "arm_run", arm["id"], {
            "attempt": int(restarted.get("attempt_no") or 1), "baseline_sha": pair["baseline_sha"],
            "reason": redact(error)[-1000:], "prompt_mode": "same_original_prompt_once",
            "counts_toward_development_attempts": count_development_failure,
            "counts_toward_error_retries": count_error_retry,
        })
        return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted

    def _restart_arm_from_delivered_commit(self, pair_id: str, arm: Dict[str, Any],
                                            prompt: str, error: str,
                                            source_workspace: Optional[Path] = None,
                                            source_sha_override: str = "",
                                            count_development_failure: bool = True,
                                            count_error_retry: bool = True) -> Dict[str, Any]:
        """Repair a real artifact defect without discarding delivered code."""
        arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or arm
        attempt = max(1, int(arm.get("attempt_no") or 1))
        maximum = max(1, int(self.db.setting("development_max_attempts", 2)))
        if attempt >= maximum:
            archived = self.claude.archive_failed_attempt(arm, error, prepare_retry=False)
            self._retire_pair_and_schedule_replacement(pair_id, arm["id"], error)
            return archived
        source_sha = str(source_sha_override or arm.get("commit_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise RuntimeError("缺少可复用的 %s 已交付提交" % arm.get("arm", "Arm"))
        repair_prompt = self._artifact_repair_prompt(prompt, error, source_sha)
        self._invalidate_recordings(pair_id, [arm.get("arm")], error)
        restarted = self.claude.archive_failed_attempt(
            arm, error, prepare_retry=True,
            count_development_failure=count_development_failure,
            count_error_retry=count_error_retry,
        )
        persisted_error = redact(error)[-2000:]
        if "docker 产物验收" not in persisted_error.casefold():
            persisted_error = ("Docker 产物验收失败：返工会话错误：" + persisted_error)[-2000:]
        self.db.execute(
            "UPDATE arm_runs SET status='waiting_retry',commit_sha=?,error=?,updated_at=? WHERE id=?",
            (source_sha, persisted_error, now_iso(), arm["id"]),
        )
        canonical = self.git.prepare_arm_commit(pair_id, str(arm["arm"]), source_sha)
        time.sleep(8)
        restarted = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        self.claude.launch(restarted)
        restarted = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        self.claude.wait_until_ready(restarted)
        self.claude.materialize_repository(restarted, canonical, source_sha)
        restarted = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        self._send_prompt_with_pair_stagger(pair_id, restarted, repair_prompt)
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',error='',updated_at=? WHERE id=?",
            (now_iso(), pair_id),
        )
        self.db.audit("artifact.repair_started_from_commit", "arm_run", arm["id"], {
            "arm": arm["arm"], "source_commit": source_sha,
            "attempt": int(restarted.get("attempt_no") or attempt + 1),
            "prompt_mode": "original_plus_artifact_failure",
        })
        current = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        # This transient key is intentionally not persisted in the database. It
        # lets the caller monitor the exact prompt sent to this fresh session.
        current["_monitor_prompt"] = repair_prompt
        return current

    @staticmethod
    def _artifact_repair_prompt(prompt: str, error: str, source_sha: str) -> str:
        marker = "[PAIRWISE_ARTIFACT_REPAIR]"
        if marker in prompt:
            return prompt
        return (
            prompt.rstrip()
            + "\n\n"
            + marker
            + "\n当前工作区已从已交付提交 " + source_sha + " 恢复。"
            + "不要从共同基线重做，也不要只重复提交现有代码。"
            + "请复现并修复以下 Docker 产物验收问题，完成后运行清洁 Docker Compose 验收并提交修复：\n"
            + redact(error)[-1600:]
        )

    def _artifact_repair_source(self, pair_id: str, arm: Dict[str, Any],
                                require_pending_error: bool = True):
        """Find a clean archived checkout for a pending artifact repair.

        The canonical A/B clone intentionally remains at the common baseline,
        so it must never be used to recover this kind of retry.
        """
        if require_pending_error and "Docker 产物验收失败" not in str(arm.get("error") or ""):
            return None
        check = self.db.one(
            """SELECT commit_sha FROM artifact_checks
               WHERE pair_id=? AND arm=? AND status='failed' AND commit_sha<>''
               ORDER BY created_at DESC LIMIT 1""",
            (pair_id, arm.get("arm")),
        ) or {}
        source_sha = str(check.get("commit_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            return None
        events = self.db.all(
            """SELECT detail_json FROM audit_events
               WHERE event_type='claude.failed_attempt_archived' AND entity_id=?
               ORDER BY id DESC LIMIT 12""",
            (arm.get("id"),),
        )
        for event in events:
            try:
                archive = Path(str(json.loads(event.get("detail_json") or "{}").get("archive") or ""))
            except (TypeError, ValueError):
                continue
            candidate = archive / "workspace"
            if not (candidate / ".git").is_dir():
                continue
            head = run_command(["git", "rev-parse", "HEAD"], cwd=candidate, check=False, timeout=30)
            branch = run_command(["git", "branch", "--show-current"], cwd=candidate, check=False, timeout=30)
            dirty = run_command(["git", "status", "--porcelain"], cwd=candidate, check=False, timeout=30)
            if (
                head.returncode == 0
                and head.stdout.strip() == source_sha
                and branch.stdout.strip() == str(arm.get("arm") or "")
                and not dirty.stdout.strip()
            ):
                return candidate, source_sha
        return None

    def _handle_attempt_failure(self, pair_id: str, arm: Dict[str, Any], prompt: str,
                                error: str, early_replace: bool = False) -> Dict[str, Any]:
        with self._pair_failure_lock(pair_id):
            return self._handle_attempt_failure_locked(
                pair_id, arm, prompt, error, early_replace,
            )

    def _handle_attempt_failure_locked(self, pair_id: str, arm: Dict[str, Any], prompt: str,
                                       error: str, early_replace: bool = False) -> Dict[str, Any]:
        """Retry every failed development attempt in a new session.

        A/B share one failure budget. The second failure stops that Arm; an
        active peer keeps its own development window before the Pair is replaced.
        API failures use the same budget.
        """
        arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or arm
        attempt = max(1, int(arm.get("attempt_no") or 1))
        prompt_delivery_error = self._is_prompt_delivery_error(error)
        session_infrastructure_error = self._is_session_infrastructure_error(error)
        if not session_infrastructure_error and self._is_claude_api_error(error):
            return self._queue_api_retry(pair_id, arm, error)
        previous_prompt_failures = (
            self._consecutive_prompt_infrastructure_failures(str(arm["id"]))
            if session_infrastructure_error else 0
        )
        prompt_failure_limit = max(2, int(self.db.setting("prompt_infrastructure_retry_limit", 2)))
        pause_prompt_infrastructure = (
            session_infrastructure_error and previous_prompt_failures + 1 >= prompt_failure_limit
        )
        if session_infrastructure_error:
            maximum = max(1, int(self.db.setting("development_max_attempts", 2)))
            count_development_failure = False
            count_error_retry = False
            self.db.audit("claude.attempt_failed", "arm_run", arm["id"], {
                "attempt": attempt, "maximum": maximum, "error": redact(error)[-1000:],
                "action": (
                    "pause_prompt_infrastructure" if pause_prompt_infrastructure
                    else "fresh_session_from_baseline_without_failure_count"
                ),
                "early_replace": early_replace,
                "counts_toward_development_attempts": False,
                "counts_toward_error_retries": False,
                "counts_toward_pair_failure_limit": False,
            })
        else:
            _failure_count, maximum, exhausted = self._record_pair_development_failure(
                pair_id, arm["id"], error, "development_error",
            )
            count_development_failure = True
            count_error_retry = True

        if session_infrastructure_error and pause_prompt_infrastructure:
            paused = self.claude.archive_failed_attempt(
                arm, error, prepare_retry=False,
                count_development_failure=False, count_error_retry=False,
            )
            stamp = now_iso()
            paused_error = (
                "题面/轨迹基础设施连续 %d 次失败，已暂停自动重启：%s"
                % (previous_prompt_failures + 1, redact(error))
            )[-3000:]
            self.db.execute(
                """UPDATE arm_runs SET status='infrastructure_paused',error=?,updated_at=?
                   WHERE id=?""",
                (paused_error, stamp, arm["id"]),
            )
            self.db.execute(
                """UPDATE pairs SET error=?,updated_at=? WHERE id=? AND stage='development'""",
                (("%s 题面/轨迹基础设施连续失败，已暂停该侧；需要人工检查后恢复"
                  % str(arm.get("arm") or "Arm"))[-3000:], stamp, pair_id),
            )
            self.db.audit("claude.prompt_infrastructure_paused", "arm_run", arm["id"], {
                "consecutiveFailures": previous_prompt_failures + 1,
                "automaticRestartStopped": True,
            })
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or {
                **paused, "status": "infrastructure_paused", "error": paused_error,
            }
        if early_replace or (not session_infrastructure_error and exhausted):
            archived = self.claude.archive_failed_attempt(arm, error, prepare_retry=False)
            label = (
                "同一侧连续 2 次出现相同无代码轨迹，已提前换题"
                if early_replace else "项目累计 %d 次开发失败" % maximum
            )
            self._retire_after_peer_finishes(pair_id, arm["id"], error, label)
            return archived
        try:
            return self._restart_arm_from_baseline(
                pair_id, arm, prompt, error,
                count_development_failure=count_development_failure,
                count_error_retry=count_error_retry,
            )
        except Exception as exc:
            current = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or arm
            prefix = (
                "轨迹首轮 User Prompt 重跑启动失败"
                if prompt_delivery_error else
                "全新 Session 基础设施重试失败" if session_infrastructure_error else
                "第 %d 次失败后启动全新 Session 仍失败" % attempt
            )
            return self._handle_attempt_failure(
                pair_id, current, prompt,
                "%s：%s" % (prefix, redact(str(exc))),
            )

    def _retire_after_peer_finishes(self, pair_id: str, failed_arm_id: str,
                                    error: str, retire_label: str) -> bool:
        """Stop only the exhausted Arm and let its peer finish its own window."""
        peer = self.db.one(
            "SELECT id,arm,status FROM arm_runs WHERE pair_id=? AND id<>? ORDER BY arm LIMIT 1",
            (pair_id, failed_arm_id),
        ) or {}
        # Once the shared failure budget is exhausted, only a peer that is
        # already live or preserving completed work may continue.  Queued and
        # retry-waiting peers must not consume another terminal slot.
        active_statuses = {"running", "developing", "checkpointing", "exported"}
        if peer.get("status") in active_statuses:
            stamp = now_iso()
            message = (
                "%s；失败侧已停止，%s 侧继续运行，并按自身题面发送时间执行 60 分钟无业务代码规则"
                % (retire_label, peer.get("arm") or "另一")
            )
            self.db.execute(
                """UPDATE pairs SET status='running',stage='development',error=?,updated_at=?
                     WHERE id=?""",
                ((message + "：" + redact(error))[-3000:], stamp, pair_id),
            )
            self.db.audit("pair.failure_limit_waiting_for_peer", "pair", pair_id, {
                "failed_arm_id": failed_arm_id,
                "continuing_arm_id": peer.get("id") or "",
                "continuing_arm": peer.get("arm") or "",
                "continuing_status": peer.get("status") or "",
                "no_code_timeout_minutes": int(self.db.setting("first_prompt_stop_minutes", 60)),
                "retire_label": retire_label,
                "reason": redact(error)[-1000:],
            })
            return False
        self._retire_pair_and_schedule_replacement(
            pair_id, failed_arm_id, error, retire_label,
        )
        return True

    def _finish_exhausted_pair_after_peer(self, pair_id: str) -> bool:
        """Replace an exhausted Pair only after the other Arm is no longer active."""
        pair = self.db.one(
            "SELECT status,stage,development_failure_count,error FROM pairs WHERE id=?",
            (pair_id,),
        ) or {}
        maximum = max(1, int(self.db.setting("development_max_attempts", 2)))
        if (pair.get("stage") != "development"
                or int(pair.get("development_failure_count") or 0) < maximum):
            return False
        arms = self.db.all(
            "SELECT id,arm,status,error,commit_sha FROM arm_runs WHERE pair_id=?",
            (pair_id,),
        )
        failed = next((arm for arm in arms if arm.get("status") == "failed"), None)
        if not failed:
            return False
        active_statuses = {"running", "developing", "checkpointing", "exported"}
        if any(
            arm.get("id") != failed.get("id") and arm.get("status") in active_statuses
            for arm in arms
        ):
            return False
        peer = next((arm for arm in arms if arm.get("id") != failed.get("id")), {})
        if peer.get("status") == "completed" and peer.get("commit_sha"):
            terminal_check = self.db.one(
                """SELECT id FROM artifact_checks
                     WHERE pair_id=? AND arm=? AND commit_sha=?
                       AND status IN ('passed','observed_failed')
                     ORDER BY created_at DESC LIMIT 1""",
                (pair_id, peer.get("arm"), peer.get("commit_sha")),
            )
            if not terminal_check:
                self.db.execute(
                    """UPDATE pairs SET status='running',stage='development',error=?,updated_at=?
                         WHERE id=?""",
                    (
                        "失败侧已停止；%s 侧已完成，先执行 Docker 产物验收再决定保留返修或换题"
                        % (peer.get("arm") or "另一"),
                        now_iso(), pair_id,
                    ),
                )
                self.db.audit("pair.failure_limit_validating_completed_peer", "pair", pair_id, {
                    "failed_arm_id": failed.get("id") or "",
                    "completed_arm_id": peer.get("id") or "",
                    "completed_arm": peer.get("arm") or "",
                    "commit_sha": peer.get("commit_sha") or "",
                })
                self._schedule_completed_arm_validations(pair_id)
                return False
        error = str(failed.get("error") or pair.get("error") or "项目已达到开发失败上限")
        self._retire_pair_and_schedule_replacement(
            pair_id, str(failed["id"]), error,
            "项目累计 %d 次失败，另一侧已结束" % maximum,
        )
        return True

    def _retire_pair_and_schedule_replacement(self, pair_id: str, failed_arm_id: str,
                                              error: str,
                                              retire_label: str = "项目累计 2 次开发失败") -> None:
        stamp = now_iso()
        repair = None
        with self.db.transaction() as conn:
            pair = conn.execute(
                "SELECT status,stage,development_failure_count FROM pairs WHERE id=?",
                (pair_id,),
            ).fetchone()
            if not pair or pair["stage"] in ("task_replacement", "replaced", "replacement_failed"):
                return
            # A completed side that has already passed artifact validation is
            # expensive, valid work.  The failed side may be queued only while
            # the Pair still has shared failure budget; reaching the configured
            # limit must never create a fresh window.
            peer = conn.execute(
                """SELECT a.id,a.arm,a.commit_sha FROM arm_runs a
                    WHERE a.pair_id=? AND a.id<>? AND a.status='completed'
                      AND a.commit_sha<>''
                      AND EXISTS(SELECT 1 FROM artifact_checks c
                        WHERE c.pair_id=a.pair_id AND c.arm=a.arm
                          AND c.commit_sha=a.commit_sha AND c.status='passed')
                    LIMIT 1""",
                (pair_id, failed_arm_id),
            ).fetchone()
            repair_already_used = conn.execute(
                """SELECT 1 FROM audit_events
                    WHERE event_type='claude.single_arm_repair_resumed'
                      AND entity_id=? LIMIT 1""",
                (failed_arm_id,),
            ).fetchone()
            maximum = max(1, int(self.db.setting("development_max_attempts", 2)))
            budget_remaining = int(pair["development_failure_count"] or 0) < maximum
            if peer and not repair_already_used and budget_remaining:
                failed = conn.execute(
                    "SELECT arm FROM arm_runs WHERE id=? AND pair_id=?",
                    (failed_arm_id, pair_id),
                ).fetchone()
                repair = {
                    "failed_arm": str(failed["arm"] if failed else "Arm"),
                    "preserved_arm": str(peer["arm"]),
                    "preserved_commit": str(peer["commit_sha"]),
                }
                message = (
                    "%s；%s 已通过 Docker 产物验收，保留该侧并等待空位仅返修 %s：%s"
                    % (retire_label, repair["preserved_arm"], repair["failed_arm"], redact(error))
                )[-3000:]
                conn.execute(
                    """UPDATE pairs SET status='repair_pending',stage='single_arm_repair_pending',
                         error=?,updated_at=? WHERE id=?""",
                    (message, stamp, pair_id),
                )
                conn.execute(
                    """UPDATE delivery_submissions SET status='needs_review',error=?,updated_at=?
                       WHERE pair_id=?""",
                    (message, stamp, pair_id),
                )
            else:
                conn.execute(
                    "UPDATE pairs SET status='failed',stage='task_replacement',error=?,updated_at=? WHERE id=?",
                    ((retire_label + "，正在自动换题：" + redact(error))[-3000:], stamp, pair_id),
                )
                conn.execute(
                    """UPDATE delivery_submissions SET status='discarded',error=?,updated_at=?
                       WHERE pair_id=?""",
                    ("原 Pair %s，已停止交付并正在自动换题" % retire_label, stamp, pair_id),
                )
        if repair:
            self._invalidate_recordings(
                pair_id, [repair["failed_arm"]], reason="失败侧等待单独返修：" + error,
            )
            self.db.audit("claude.single_arm_repair_queued", "arm_run", failed_arm_id, {
                "pair_id": pair_id,
                "arm": repair["failed_arm"],
                "preservedArm": repair["preserved_arm"],
                "preservedCommit": repair["preserved_commit"],
                "waitForCapacity": 1,
                "pairFailureBudgetPreserved": True,
            })
            return
        self._invalidate_recordings(pair_id, reason="当前 Pair 已换题：" + error)
        for other in self.db.all("SELECT * FROM arm_runs WHERE pair_id=? AND id<>?", (pair_id, failed_arm_id)):
            if other["status"] == "waiting_api_retry":
                self.db.execute(
                    "UPDATE arm_runs SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?",
                    ("同一 Pair 已达到失败上限并换题", stamp, stamp, other["id"]),
                )
            elif other["status"] in ("queued", "running", "developing", "waiting_retry", "checkpointing"):
                try:
                    self.claude.archive_failed_attempt(
                        other, "同一 Pair 已累计 2 次失败，当前 Pair 已换题", prepare_retry=False,
                    )
                except Exception as exc:
                    self.db.audit("claude.peer_retire_failed", "arm_run", other["id"], {
                        "error": redact(str(exc))[-1000:],
                    })
        self.db.audit("pair.task_replacement_scheduled", "pair", pair_id, {
            "failed_arm_id": failed_arm_id, "reason": redact(error)[-1000:],
            "retire_label": retire_label,
        })
        self._submit("replace-task-" + pair_id, self._start_replacement_pair, pair_id)

    def _start_replacement_pair(self, retired_pair_id: str) -> Dict[str, Any]:
        try:
            candidate = self._next_ready_task()
            if not candidate:
                self._schedule_refill_once()
                self.db.execute(
                    "UPDATE pairs SET stage='replaced',error=?,updated_at=? WHERE id=?",
                    ("原 Pair 已废弃；题库暂无合格题，正在从所有可用来源准备补位题目",
                     now_iso(), retired_pair_id),
                )
                self.db.audit("pair.task_replacement_waiting_for_task", "pair", retired_pair_id, {
                    "rule": "first_available",
                })
                return {"retiredPairId": retired_pair_id, "replacementPairId": "",
                        "replacementTaskId": "", "outcome": "awaiting_task_refill"}
            try:
                replacement = self.create_pair(candidate["id"])
            except ValueError as exc:
                if "已达到 Pair 并发上限" not in str(exc):
                    raise
                stamp = now_iso()
                self.db.execute(
                    "UPDATE pairs SET stage='replaced',error=?,updated_at=? WHERE id=?",
                    ("原 Pair 已废弃；并发空位已由自动补位使用，无需重复创建替换 Pair", stamp, retired_pair_id),
                )
                self.db.execute(
                    """UPDATE delivery_submissions SET status='discarded',error=?,updated_at=?
                       WHERE pair_id=?""",
                    ("原 Pair 已废弃；并发空位已由自动补位使用", stamp, retired_pair_id),
                )
                self.db.audit("pair.task_replacement_skipped_capacity", "pair", retired_pair_id, {
                    "reason": "capacity_filled_by_scheduler",
                })
                return {"retiredPairId": retired_pair_id, "replacementPairId": "",
                        "replacementTaskId": "", "outcome": "capacity_filled"}
            replacement_id = replacement["id"]
            self.prepare_pair_repository(replacement_id)
            self.start_pair(replacement_id)
            self.db.execute(
                "UPDATE pairs SET stage='replaced',error=?,updated_at=? WHERE id=?",
                ("原 Pair 已废弃，已用当前首个合格题自动换题为 %s" % replacement_id, now_iso(), retired_pair_id),
            )
            self.db.execute(
                """UPDATE delivery_submissions SET status='discarded',error=?,updated_at=?
                   WHERE pair_id=?""",
                ("原 Pair 已废弃，已自动换题为 %s" % replacement_id,
                 now_iso(), retired_pair_id),
            )
            self.db.audit("pair.task_replaced", "pair", retired_pair_id, {
                "replacement_pair_id": replacement_id, "replacement_task_id": candidate["id"],
            })
            return {"retiredPairId": retired_pair_id, "replacementPairId": replacement_id,
                    "replacementTaskId": candidate["id"]}
        except Exception as exc:
            self.db.execute(
                "UPDATE pairs SET stage='replacement_failed',error=?,updated_at=? WHERE id=?",
                (("自动换题失败：" + redact(str(exc)))[-3000:], now_iso(), retired_pair_id),
            )
            self.db.execute(
                """UPDATE delivery_submissions SET status='discarded',error=?,updated_at=?
                   WHERE pair_id=?""",
                (("原 Pair 已废弃；自动换题失败：" + redact(str(exc)))[-2000:],
                 now_iso(), retired_pair_id),
            )
            self.db.audit("pair.task_replacement_failed", "pair", retired_pair_id, {
                "error": redact(str(exc))[-1000:],
            })
            raise

    def _arm_comparison_sha(self, pair_id: str, arm: str) -> str:
        pair = self.db.one("SELECT baseline_sha FROM pairs WHERE id=?", (pair_id,)) or {}
        repo = self.db.one("SELECT a_sha,b_sha FROM git_repositories WHERE pair_id=?", (pair_id,)) or {}
        column = "a_sha" if arm == "A" else "b_sha"
        source_sha = str(repo.get(column) or pair.get("baseline_sha") or "")
        return source_sha if re.fullmatch(r"[0-9a-f]{40}", source_sha) else ""

    def _monitor_arm(self, pair_id: str, arm_id: str, prompt: str) -> Dict[str, Any]:
        """Ensure only one long-lived monitor can own an Arm at a time."""
        with self._arm_monitor_locks_guard:
            monitor_lock = self._arm_monitor_locks.setdefault(arm_id, threading.Lock())
        if not monitor_lock.acquire(blocking=False):
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or {}
        try:
            return self._monitor_arm_exclusive(pair_id, arm_id, prompt)
        finally:
            monitor_lock.release()

    def _monitor_arm_exclusive(self, pair_id: str, arm_id: str,
                               prompt: str) -> Dict[str, Any]:
        # Claude's native TUI removes blank paragraph rows when it records the
        # first user event. Use and persist that representation before any
        # exact-match check, including monitors resumed after a service update.
        prompt = self._canonicalize_pair_prompt(pair_id, prompt)
        started = time.monotonic()
        initial = self.db.one("SELECT prompt_sent_at FROM arm_runs WHERE id=?", (arm_id,)) or {}
        try:
            sent_at = datetime.fromisoformat(str(initial.get("prompt_sent_at") or ""))
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            started -= max(0.0, (datetime.now(timezone.utc) - sent_at).total_seconds())
        except ValueError:
            pass
        warned = False
        progress_token = ""
        progress_at = time.monotonic()
        code_stall_warned = False
        deferred_api_error = ""
        deferred_api_error_signature = ""
        monitor_error_signature = ""
        while True:
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,))
            if not arm or arm["status"] not in ("developing", "running", "waiting_retry"):
                return arm or {}
            pair_state = self.db.one("SELECT stage FROM pairs WHERE id=?", (pair_id,)) or {}
            if pair_state.get("stage") != "development":
                return arm
            try:
                state = self.claude.trace_state(arm, prompt)
            except Exception as exc:
                state = {"complete": False, "api_error": "", "monitor_error": "轨迹监控失败：%s" % redact(str(exc))}
            if state.get("session_id") or state.get("prompt_id"):
                self.db.execute(
                    "UPDATE arm_runs SET session_id=?,prompt_id=?,updated_at=? WHERE id=?",
                    (state.get("session_id", ""), state.get("prompt_id", ""), now_iso(), arm_id),
                )
            progress_token, progress_at, native_progressed = self._advance_native_progress(
                state, progress_token, progress_at, time.monotonic(),
            )
            if native_progressed:
                # A warning describes one continuous idle window.  Once the
                # native JSONL grows again, a later real stall gets its own
                # warning instead of inheriting the previous window.
                code_stall_warned = False
            # A terminal API error is preserved with the native trace, then
            # handed to the independent cooldown queue when it is a 429. A
            # trace that already contains a later normal completion remains
            # deliverable; 504 and other failures keep their normal counters.
            monitor_error = str(state.get("monitor_error") or "")
            monitor_infrastructure_error = monitor_error.startswith("轨迹监控失败：")
            error = "" if monitor_infrastructure_error else monitor_error
            if monitor_infrastructure_error:
                signature = hashlib.sha256(monitor_error.encode("utf-8")).hexdigest()
                if signature != monitor_error_signature:
                    monitor_error_signature = signature
                    self.db.audit("claude.trace_monitor_warning", "arm_run", arm_id, {
                        "pair_id": pair_id,
                        "error": redact(monitor_error)[-1000:],
                        "counts_toward_development_attempts": False,
                        "action": "keep_session_and_retry_monitoring",
                    })
            elif not monitor_error:
                monitor_error_signature = ""
            if not error and state.get("prompt_matches") is False:
                issue = "%s 轨迹中没有与题面逐字一致的首轮 User Prompt" % arm.get("arm", "Arm")
                self.db.audit("claude.live_prompt_mismatch", "arm_run", arm_id, {
                    "expected_length": len(prompt),
                    "observed_length": len(str(state.get("observed_prompt") or "")),
                    "action": "fresh_session_with_exact_database_prompt",
                    "counts_toward_development_attempts": False,
                })
                return self._restart_trace_invalid_arms(pair_id, [arm], prompt, [issue])
            if state.get("followup_detected"):
                error = "检测到首轮后的追加消息，当前 Session 作废并从共同基线重跑：%s" % state.get("followup_text", "")
            terminal_rate_limit = bool(
                not state.get("complete")
                and state.get("api_error_turn_ended")
                and self._is_claude_rate_limit_error(str(state.get("api_error") or ""))
            )
            terminal_gateway_timeout = bool(
                not state.get("complete")
                and state.get("api_error_turn_ended")
                and self._is_claude_gateway_timeout_error(
                    str(state.get("api_error") or "")
                )
            )
            if not error and (terminal_rate_limit or terminal_gateway_timeout):
                error = str(
                    state.get("api_error")
                    or (
                        "API Error: 504 Gateway Timeout"
                        if terminal_gateway_timeout
                        else "API Error: 429 Rate Limit"
                    )
                )
            if not error and state.get("turn_ended_without_final"):
                error = "Claude 原生会话已结束，但没有输出最终答复"
            if not error and not state.get("complete"):
                error = self.claude.stalled_gateway_timeout(
                    arm, str(state.get("path") or ""),
                    int(self.db.setting("terminal_idle_seconds", 120)),
                )
            if not error and not state.get("complete") and not self.claude.runtime_alive(arm):
                error = "Claude 容器或终端意外结束，当前 Session 没有形成完整结果"
            observed_api_error = str(state.get("api_error") or "")
            deferred_error = ""
            if self._is_claude_api_error(observed_api_error):
                deferred_error = observed_api_error
            elif self._is_claude_api_error(error):
                deferred_error = error
            if (
                deferred_error
                and not state.get("complete")
                and self._is_claude_user_rpm_error(deferred_error)
                and self._user_rpm_fast_fail_active(str(state.get("api_error_at") or ""))
            ):
                # A user-wide RPM cap has no model fallback in this provider
                # route. End this Arm now; the normal shared failure budget and
                # global cooldown decide whether a clean retry is allowed.
                self.db.audit("claude.user_rpm_limit_immediate_end", "arm_run", arm_id, {
                    "pair_id": pair_id,
                    "error": redact(deferred_error)[-1000:],
                    "error_at": state.get("api_error_at"),
                    "action": "archive_and_release_terminal",
                    "waited_for_no_code_deadline": False,
                })
                return self._handle_attempt_failure(pair_id, arm, prompt, deferred_error)
            if deferred_error and terminal_gateway_timeout:
                # The native turn-duration marker means Claude is already
                # back at its prompt after giving up on the 504. Keeping this
                # container open cannot resume the turn, so archive this side
                # immediately and let the normal Pair-wide retry budget decide
                # whether it receives one clean retry. A later final answer in
                # the same trace sets ``complete`` and never enters this path.
                self.db.audit(
                    "claude.gateway_timeout_turn_ended",
                    "arm_run",
                    arm_id,
                    {
                        "pair_id": pair_id,
                        "error": redact(deferred_error)[-1000:],
                        "session_interrupted_immediately": True,
                        "action": "archive_and_release_terminal",
                    },
                )
                return self._handle_attempt_failure(
                    pair_id, arm, prompt, deferred_error,
                )
            if deferred_error:
                error_signature = self._claude_api_error_signature(deferred_error)
                if error_signature != deferred_api_error_signature:
                    deferred_api_error = deferred_error
                    deferred_api_error_signature = error_signature
                    retry_after = datetime.now(timezone.utc) + timedelta(
                        seconds=self._api_retry_delay_seconds(
                            deferred_error, int(arm.get("api_retry_count") or 0),
                        ),
                    )
                    self._register_global_rate_limit(deferred_error, retry_after)
                    self.db.execute(
                        "UPDATE arm_runs SET last_api_error=?,error=?,updated_at=? WHERE id=?",
                        (redact(deferred_error)[-3000:],
                         ("API 错误已记录；有代码按 70 分钟总时长和 40 分钟有效轨迹停滞处理"
                          if self.claude.has_business_code(
                              Path(arm["workspace_path"]),
                              self._arm_comparison_sha(pair_id, str(arm["arm"])),
                          ) else "API 错误已记录，继续等待本轮 60 分钟无代码时限"),
                         now_iso(), arm_id),
                    )
                    self.db.audit("claude.api_error_deferred_to_no_code_deadline", "arm_run", arm_id, {
                        "pair_id": pair_id,
                        "error": redact(deferred_error)[-1000:],
                        "no_code_timeout_minutes": int(self.db.setting("first_prompt_stop_minutes", 60)),
                        "counts_when_timeout_expires": True,
                        "session_interrupted_immediately": False,
                    })
                error = ""
            if error:
                self.db.audit("claude.session_invalidated", "arm_run", arm_id, {
                    "error": redact(error)[-1000:], "action": "fresh_session_from_baseline",
                })
                retried = self._handle_attempt_failure(pair_id, arm, prompt, error)
                if retried.get("status") in (
                    "failed", "infrastructure_paused", "waiting_retry", "waiting_api_retry",
                ):
                    return retried
                started = time.monotonic()
                warned = False
                continue
            if state.get("complete"):
                result = str(state.get("result") or "")
                if state.get("api_error"):
                    self.db.audit("claude.api_error_recovered", "arm_run", arm_id, {
                        "error": redact(str(state.get("api_error")))[-1000:],
                        "action": "accepted_native_turn_end_in_same_session",
                        "completion_mode": state.get("completion_mode", ""),
                    })
                if int(state.get("automatic_companion_count") or 0):
                    self.db.audit("claude.automatic_companion_ignored", "arm_run", arm_id, {
                        "count": int(state.get("automatic_companion_count") or 0),
                        "messages": list(state.get("automatic_companion_messages") or [])[:10],
                        "classification": "system_generated_not_manual_followup",
                    })
                try:
                    self.db.execute("UPDATE arm_runs SET status='checkpointing',result=?,updated_at=? WHERE id=?", (result, now_iso(), arm_id))
                    trace_dir = self.claude.export_and_stop(arm)
                    self.db.execute(
                        """UPDATE arm_runs SET status='checkpointing',trace_path=?,result=?,
                           error='',updated_at=? WHERE id=?""",
                        (str(trace_dir), result, now_iso(), arm_id),
                    )
                except Exception as exc:
                    failure = "完成后导出轨迹失败：%s" % redact(str(exc))
                    current = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
                    retried = self._handle_attempt_failure(pair_id, current, prompt, failure)
                    if retried.get("status") in (
                        "failed", "infrastructure_paused", "waiting_retry", "waiting_api_retry",
                    ):
                        return retried
                    started = time.monotonic()
                    warned = False
                    continue
                try:
                    return self._finish_checkpointed_arm(pair_id, arm_id)
                except Exception:
                    # The completed code and native trace stay in place. The
                    # scheduler retries only the Git push instead of asking
                    # Claude to redo an already finished implementation.
                    return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or {}
            elapsed = time.monotonic() - started
            workspace = Path(arm["workspace_path"])
            business_progress = self.claude.business_progress(
                workspace, self._arm_comparison_sha(pair_id, str(arm["arm"])),
            )
            has_code = bool(business_progress.get("has_code"))
            if elapsed >= int(self.db.setting("first_prompt_warning_minutes", 15)) * 60 and not has_code and not warned:
                warned = True
                self.db.execute("UPDATE arm_runs SET warning_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), arm_id))
                self.db.audit("claude.no_code_warning", "arm_run", arm_id, {"elapsedSeconds": int(elapsed)})
            stop_minutes = int(self.db.setting("first_prompt_stop_minutes", 60))
            repeated_trace_minutes = int(self.db.setting("repeated_no_code_trace_minutes", 40))
            attempt = max(1, int(arm.get("attempt_no") or 1))
            hard_timeout_due = elapsed >= stop_minutes * 60
            repeated_trace_check_due = (
                attempt >= 2 and elapsed >= max(0, repeated_trace_minutes) * 60
            )
            if not has_code and (hard_timeout_due or repeated_trace_check_due):
                signature = str(state.get("activity_signature") or "")
                previous = self.db.one(
                    """SELECT detail_json FROM audit_events
                       WHERE event_type='claude.no_code_timeout_signature' AND entity_id=?
                       ORDER BY id DESC LIMIT 1""",
                    (arm_id,),
                ) or {}
                try:
                    previous_detail = json.loads(previous.get("detail_json") or "{}")
                except ValueError:
                    previous_detail = {}
                repeated = bool(
                    signature and attempt >= 2
                    and int(previous_detail.get("attempt") or 0) == attempt - 1
                    and previous_detail.get("signature") == signature
                )
                # At the 40-minute retry checkpoint, stop only a byte-for-byte
                # equivalent no-code activity signature. A different trace is
                # genuine new behavior and keeps the normal 60-minute window.
                if not hard_timeout_due and not repeated:
                    time.sleep(5)
                    continue
                self.db.audit("claude.no_code_timeout_signature", "arm_run", arm_id, {
                    "attempt": attempt, "signature": signature,
                    "summary": list(state.get("activity_summary") or [])[:12],
                    "matches_previous_attempt": repeated,
                    "rule_trigger": "repeated_trace_early" if not hard_timeout_due else "hard_timeout",
                    "elapsedSeconds": int(elapsed),
                })
                if repeated and not hard_timeout_due:
                    reason = "同一侧连续 2 次出现完全相同的无代码轨迹；第二次尝试已达到 40 分钟"
                elif deferred_api_error:
                    reason = "%d 分钟内无代码产出，期间出现 %s" % (
                        stop_minutes, deferred_api_error,
                    )
                else:
                    reason = (
                        "同一侧连续 2 次出现完全相同的无代码轨迹特征"
                        if repeated else "首轮超时且无代码产出"
                    )
                retried = self._handle_attempt_failure(
                    pair_id, arm, prompt, reason, early_replace=repeated,
                )
                if retried.get("status") in (
                    "failed", "infrastructure_paused", "waiting_retry", "waiting_api_retry",
                ):
                    return retried
                started = time.monotonic()
                warned = False
                continue
            progress_idle = time.monotonic() - progress_at
            warning_after = int(self.db.setting("development_total_warning_minutes", 60)) * 60
            warning_idle = min(
                int(self.db.setting("development_trace_stall_minutes", 40)) * 60,
                10 * 60,
            )
            stall_reason = self._development_stall_reason(elapsed, progress_idle, has_code)
            if (has_code and elapsed >= warning_after and progress_idle >= warning_idle
                    and not code_stall_warned):
                code_stall_warned = True
                self.db.audit("claude.code_stall_warning", "arm_run", arm_id, {
                    "elapsedSeconds": int(elapsed),
                    "traceIdleSeconds": int(progress_idle),
                    "progressSource": "native_jsonl",
                })
            if stall_reason:
                retried = self._handle_attempt_failure(pair_id, arm, prompt, stall_reason)
                if retried.get("status") in (
                    "failed", "infrastructure_paused", "waiting_retry", "waiting_api_retry",
                ):
                    return retried
                started = time.monotonic()
                warned = False
                progress_token = ""
                progress_at = time.monotonic()
                code_stall_warned = False
                continue
            # A normal text event still advances the native-trace watchdog,
            # but it is not proof that implementation work continues. Catch
            # sessions that only keep talking after their last file/commit or
            # tool action without waiting for the separate 70/40 trace rule.
            if has_code:
                last_business_progress = float(
                    business_progress.get("last_modified") or 0.0
                )
                tool_activity = str(state.get("last_tool_activity_at") or "")
                if tool_activity:
                    try:
                        parsed_activity = datetime.fromisoformat(
                            tool_activity.replace("Z", "+00:00")
                        )
                        if parsed_activity.tzinfo is None:
                            parsed_activity = parsed_activity.replace(tzinfo=timezone.utc)
                        last_business_progress = max(
                            last_business_progress, parsed_activity.timestamp(),
                        )
                    except ValueError:
                        pass
                business_idle_minutes = max(
                    1, int(self.db.setting("business_progress_idle_minutes", 60)),
                )
                business_idle_seconds = (
                    max(0.0, time.time() - last_business_progress)
                    if last_business_progress else 0.0
                )
                if (last_business_progress
                        and business_idle_seconds >= business_idle_minutes * 60):
                    reason = (
                        "已有业务代码，但连续 %d 分钟没有业务文件、提交或工具执行进展"
                        % business_idle_minutes
                    )
                    self.db.audit(
                        "claude.business_progress_timeout", "arm_run", arm_id, {
                            "pair_id": pair_id,
                            "arm": arm.get("arm") or "",
                            "elapsedSeconds": int(elapsed),
                            "idleSeconds": int(business_idle_seconds),
                            "idleMinutes": business_idle_minutes,
                            "lastProgressAt": datetime.fromtimestamp(
                                last_business_progress, timezone.utc,
                            ).isoformat(),
                            "businessPaths": list(
                                business_progress.get("paths") or []
                            )[:12],
                            "rule": "business_code_idle_timeout",
                        },
                    )
                    retried = self._handle_attempt_failure(
                        pair_id, arm, prompt, reason,
                    )
                    if retried.get("status") in (
                        "failed", "infrastructure_paused", "waiting_retry", "waiting_api_retry",
                    ):
                        return retried
                    started = time.monotonic()
                    warned = False
                    progress_token = ""
                    progress_at = time.monotonic()
                    code_stall_warned = False
                    continue
            time.sleep(5)

    @staticmethod
    def _advance_native_progress(state: Dict[str, Any], previous_token: str,
                                 previous_at: float, observed_at: float):
        """Advance the watchdog only for a new effective native event."""
        current = str(state.get("progress_token") or "")
        if current and current != previous_token:
            try:
                age = max(0.0, float(state.get("effective_progress_age_seconds") or 0.0))
            except (TypeError, ValueError):
                age = 0.0
            # Preserve the event's real age after service restarts.  Without
            # this, merely restarting the console grants every stale Arm a
            # fresh 40-minute idle window.
            return current, observed_at - age, True
        return previous_token, previous_at, False

    def _development_stall_reason(self, elapsed: float, trace_idle: float,
                                  has_code: bool) -> str:
        """Stop code-producing sessions only when their native trace is stale.

        ``terminal.log`` is deliberately excluded: Claude's spinner repaints it
        continuously even when the model has made no observable progress.
        """
        stop_after = int(self.db.setting("development_total_stop_minutes", 70)) * 60
        idle_after = int(self.db.setting("development_trace_stall_minutes", 40)) * 60
        if has_code and elapsed >= stop_after and trace_idle >= idle_after:
            return (
                "开发总时长已达到 %d 分钟，且原生轨迹连续 %d 分钟没有有效事件；"
                "终端动画刷新不计为进展" % (stop_after // 60, idle_after // 60)
            )
        return ""

    def _restart_trace_invalid_arms(self, pair_id: str, arms: List[Dict[str, Any]],
                                    prompt: str, issues: List[str]) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if self._pair_blocks_development_restart(pair):
            # The reusable-pair scheduler can revisit the same cancelled or
            # terminal Pair every few seconds.  Record the durable decision
            # once instead of growing audit_events indefinitely.
            if not self.db.one(
                """SELECT 1 FROM audit_events
                     WHERE event_type='claude.trace_repair_skipped_terminal_pair'
                       AND entity_type='pair' AND entity_id=? LIMIT 1""",
                (pair_id,),
            ):
                self.db.audit("claude.trace_repair_skipped_terminal_pair", "pair", pair_id, {
                    "pair_status": pair.get("status"), "pair_stage": pair.get("stage"),
                    "issues": list(dict.fromkeys(issues)),
                })
            return {"pairId": pair_id, "restarted": [], "issues": list(dict.fromkeys(issues)),
                    "skipped": "terminal_pair"}
        terminal_limit = self._development_arm_limit()
        active_arms = self._active_development_arm_count()
        replacing_active = sum(
            1 for arm in arms
            if str(arm.get("status") or "") in ("running", "developing", "checkpointing")
            or (
                str(arm.get("status") or "") == "waiting_retry"
                and bool(arm.get("image_id"))
            )
        )
        if active_arms - replacing_active + len(arms) > terminal_limit:
            return {
                "pairId": pair_id,
                "restarted": [],
                "issues": list(dict.fromkeys(issues)),
                "deferred": "terminal_capacity",
                "terminalLimit": terminal_limit,
            }
        stamp = now_iso()
        reason = "；".join(dict.fromkeys(str(issue) for issue in issues))[-2500:]
        prompt_mismatch = any("首轮 User Prompt" in str(issue) for issue in issues)
        problem = "轨迹题面不一致" if prompt_mismatch else "轨迹文件校验未通过"
        retry_reason = (
            "轨迹首轮题面不一致，按数据库原题面重新运行"
            if prompt_mismatch else
            "轨迹文件不可用，按数据库原题面重新运行"
        )
        with self.db.transaction() as conn:
            current_pair = conn.execute(
                "SELECT id,status,stage FROM pairs WHERE id=?", (pair_id,),
            ).fetchone()
            if not current_pair or self._pair_blocks_development_restart(dict(current_pair)):
                return {"pairId": pair_id, "restarted": [],
                        "issues": list(dict.fromkeys(issues)), "skipped": "terminal_pair"}
            conn.execute(
                """UPDATE pairs SET status='running',stage='development',winner='',completed_at=NULL,
                   error=?,updated_at=? WHERE id=?""",
                ((problem + "，正在按原题面用新 Session 重跑：" + reason)[-3000:], stamp, pair_id),
            )
            for arm in arms:
                conn.execute(
                    "UPDATE arm_runs SET status='waiting_retry',error=?,updated_at=? WHERE id=?",
                    (reason, stamp, arm["id"]),
                )
            conn.execute("DELETE FROM gsb_rechecks WHERE pair_id=?", (pair_id,))
            conn.execute(
                """UPDATE gsb_reviews SET status='draft',confirmed_by='',confirmed_at=NULL,
                   final_verdict='',final_reason='',updated_at=? WHERE pair_id=?""",
                (stamp, pair_id),
            )
            conn.execute(
                """UPDATE delivery_submissions SET status='needs_review',error=?,updated_at=?
                   WHERE pair_id=?""",
                (problem + "，等待受影响侧重跑和重新验收", stamp, pair_id),
            )
            if pair.get("chain_id"):
                conn.execute(
                    """UPDATE project_chains SET status='active',followup_completed=0,
                       completed_at=NULL,updated_at=? WHERE id=?""",
                    (stamp, pair["chain_id"]),
                )
        restarted = []
        for arm in arms:
            current_pair = self.db.one("SELECT stage FROM pairs WHERE id=?", (pair_id,)) or {}
            if current_pair.get("stage") != "development":
                break
            current = self._restart_arm_from_baseline(
                pair_id, arm, prompt,
                retry_reason,
                count_development_failure=False,
                count_error_retry=False,
            )
            if current.get("restart_skipped_terminal_pair"):
                continue
            restarted.append(str(arm["arm"]))
            self._submit_monitor(
                "monitor-" + current["id"], self._monitor_arm,
                pair_id, current["id"], prompt,
            )
        self.db.execute(
            "UPDATE pairs SET error=?,updated_at=? WHERE id=? AND stage='development'",
            ((problem + "，正在按原题面用新 Session 重跑：" + reason)[-3000:], now_iso(), pair_id),
        )
        self.db.audit("claude.trace_prompt_repair_started", "pair", pair_id, {
            "arms": restarted, "issues": list(dict.fromkeys(issues)),
            "prompt_mode": "exact_database_prompt_new_session",
            "counts_toward_development_attempts": False,
        })
        return {"pairId": pair_id, "restarted": restarted, "issues": list(dict.fromkeys(issues))}

    def _refresh_pair_after_arm(self, pair_id: str) -> None:
        with self._pair_completion_lock:
            arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
            if len(arms) != 2:
                return
            statuses = {arm["status"] for arm in arms}
            if "failed" in statuses:
                if self._finish_exhausted_pair_after_peer(pair_id):
                    return
                pair_state = self.db.one(
                    "SELECT development_failure_count FROM pairs WHERE id=?", (pair_id,),
                ) or {}
                maximum = max(1, int(self.db.setting("development_max_attempts", 2)))
                completed_peer = next(
                    (arm for arm in arms if arm["status"] == "completed" and arm.get("commit_sha")),
                    None,
                )
                if (int(pair_state.get("development_failure_count") or 0) < maximum
                        and completed_peer and not self.db.one(
                            """SELECT id FROM artifact_checks WHERE pair_id=? AND arm=? AND commit_sha=?
                               AND status IN ('passed','observed_failed') LIMIT 1""",
                            (pair_id, completed_peer["arm"], completed_peer["commit_sha"]),
                        )):
                    self.db.execute(
                        """UPDATE pairs SET status='running',stage='development',error=?,updated_at=?
                           WHERE id=?""",
                        ("失败侧已停止；先独立验收已完成侧的 Docker 产物", now_iso(), pair_id),
                    )
                    self._schedule_completed_arm_validations(pair_id)
                    return
                if int(pair_state.get("development_failure_count") or 0) >= maximum:
                    self.db.execute(
                        "UPDATE pairs SET status='running',stage='development',updated_at=? WHERE id=?",
                        (now_iso(), pair_id),
                    )
                    return
                self.db.execute(
                    """UPDATE pairs SET status='failed',stage='development_failed',
                       error='A/B 至少一侧开发失败',updated_at=? WHERE id=?""",
                    (now_iso(), pair_id),
                )
                return
            if "waiting_api_retry" in statuses and statuses <= {"completed", "waiting_api_retry"}:
                self.db.execute(
                    """UPDATE pairs SET status='waiting_api_retry',stage='development',
                       error='Claude API 暂时不可用，已保留现场并等待自动重试',updated_at=?
                       WHERE id=?""",
                    (now_iso(), pair_id),
                )
                return
            pair = self._pair(pair_id)
            task = self.db.one("SELECT prompt FROM tasks WHERE id=?", (pair["task_id"],)) or {}
            prompt = str(task.get("prompt") or "")
            invalid = []
            issues = []
            trace_prompts: Dict[str, str] = {}
            completed = [arm for arm in arms if arm["status"] == "completed"]
            for arm in completed:
                trace, _, arm_issues = self._inspect_trace(arm, prompt)
                if arm_issues:
                    invalid.append(arm)
                    issues.extend(arm_issues)
                if trace:
                    trace_prompts[str(arm["arm"])] = self._trace_first_user_prompt(trace)
            if (len(completed) == 2 and trace_prompts.get("A") and trace_prompts.get("B")
                    and not self._paired_trace_prompts_match(
                        prompt, trace_prompts["A"], trace_prompts["B"],
                    )):
                invalid = completed
                issues.append("A/B 轨迹里的完整首轮 User Prompt 不一致")
            if invalid:
                self._restart_trace_invalid_arms(pair_id, invalid, prompt, issues)
                return
            stage = "artifact_validation" if statuses == {"completed"} else "development"
            self.db.execute(
                "UPDATE pairs SET status='running',stage=?,updated_at=? WHERE id=?",
                (stage, now_iso(), pair_id),
            )
            self._schedule_completed_arm_validations(pair_id)

    @staticmethod
    def _artifact_retry_key(pair_id: str, arm: str) -> str:
        return pair_id + ":" + arm

    def _schedule_completed_arm_validations(self, pair_id: str) -> None:
        """Validate each delivered Arm once; only host failures are retried."""
        for arm in self.db.all(
            "SELECT * FROM arm_runs WHERE pair_id=? AND status='completed' ORDER BY arm",
            (pair_id,),
        ):
            commit_sha = str(arm.get("commit_sha") or "")
            if not commit_sha:
                continue
            terminal = self.db.one(
                """SELECT id FROM artifact_checks
                   WHERE pair_id=? AND arm=? AND commit_sha=?
                     AND status IN ('passed','observed_failed')""",
                (pair_id, arm["arm"], commit_sha),
            )
            if terminal:
                continue
            retry_key = self._artifact_retry_key(pair_id, str(arm["arm"]))
            if time.monotonic() < self._artifact_retry_after.get(retry_key, 0.0):
                continue
            operation = "artifact-%s-%s-%s" % (pair_id, arm["arm"], commit_sha[:12])
            self._submit_auto(
                operation, self._validate_completed_arm, pair_id, str(arm["arm"]),
            )

    def _validate_completed_arm(self, pair_id: str, arm_name: str) -> Dict[str, Any]:
        """Check the exact first prompt and artifact for one completed Arm."""
        arm = self.db.one(
            "SELECT * FROM arm_runs WHERE pair_id=? AND arm=?",
            (pair_id, arm_name),
        )
        if not arm or arm["status"] != "completed":
            return {"pairId": pair_id, "arm": arm_name, "skipped": True}
        pair = self._pair(pair_id)
        task = self.db.one("SELECT prompt FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        prompt = str(task.get("prompt") or "")
        _, _, issues = self._inspect_trace(arm, prompt)
        if issues:
            return self._restart_trace_invalid_arms(pair_id, [arm], prompt, issues)
        return self._validate_pair_artifacts(pair_id, [arm_name])

    def _validate_pair_artifacts(self, pair_id: str,
                                 arm_names: Optional[List[str]] = None) -> Dict[str, Any]:
        arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        selected = [arm for arm in arms if arm["status"] == "completed"
                    and (arm_names is None or arm["arm"] in arm_names)]
        results = []
        reused_by_arm: Dict[str, bool] = {}
        for arm in selected:
            current = self.db.one(
                """SELECT * FROM artifact_checks
                   WHERE pair_id=? AND arm=? AND commit_sha=?
                     AND status IN ('passed','observed_failed')""",
                (pair_id, arm["arm"], arm["commit_sha"]),
            )
            retry_key = self._artifact_retry_key(pair_id, str(arm["arm"]))
            reusable = bool(current) and (
                current.get("status") == "passed"
                or not self._artifact_environment_failure(current)
                or time.monotonic() < self._artifact_retry_after.get(retry_key, 0.0)
            )
            reused_by_arm[str(arm["arm"])] = reusable
            results.append(current if reusable else self.artifacts.validate(
                pair_id, arm["arm"], Path(arm["workspace_path"]), arm["commit_sha"],
            ))
            if results[-1].get("status") == "passed":
                if self._verify_fixed_bug(pair_id, arm) == "observed_failed":
                    results[-1]["status"] = "observed_failed"
        failed = [
            item for item in results
            if item.get("status") not in ("passed", "observed_failed")
        ]
        if failed:
            environment_failures = [item for item in failed if self._artifact_environment_failure(item)]
            product_failures = [item for item in failed if item not in environment_failures]
            for item in environment_failures:
                retry_key = self._artifact_retry_key(pair_id, str(item.get("arm") or ""))
                self._artifact_retry_after[retry_key] = time.monotonic() + 30
            environment_names = [str(item.get("arm") or "") for item in environment_failures]
            product_names = [str(item.get("arm") or "") for item in product_failures]
            if product_failures:
                self.db.audit("artifact.failures_recorded_for_gsb", "pair", pair_id, {
                    "arms": product_names,
                    "commitsPreserved": True,
                    "claudeRepairStarted": False,
                })
            if environment_failures:
                all_completed = len(arms) == 2 and all(arm["status"] == "completed" for arm in arms)
                self.db.execute(
                    """UPDATE pairs SET status='running',stage=?,error=?,updated_at=?
                       WHERE id=?""",
                    ("artifact_validation" if all_completed else "development",
                     "Docker 验收环境冲突，已保留已完成提交并将在 30 秒后重验：" + "、".join(environment_names),
                     now_iso(), pair_id),
                )
                self.db.audit("artifact.environment_retry_scheduled", "pair", pair_id, {
                    "arms": environment_names, "preserved_commits": True, "retry_after_seconds": 30,
                })
                return {"pairId": pair_id, "checks": results,
                        "reused": environment_names, "retryScheduled": True}
            names = [str(item.get("arm") or "") for item in product_failures]
            for item in product_failures:
                self.db.execute(
                    "UPDATE artifact_checks SET status='observed_failed',updated_at=? WHERE id=?",
                    (now_iso(), item["id"]),
                )
                item["status"] = "observed_failed"
            self.db.audit("artifact.final_failure_preserved", "pair", pair_id, {
                "arms": names,
                "rule": "preserve_original_delivery_and_describe_failure_in_gsb",
                "claude_repair_started": False,
            })
            if environment_failures:
                environment_names = [str(item.get("arm") or "") for item in environment_failures]
                self.db.execute(
                    "UPDATE pairs SET status='running',stage='artifact_validation',error=?,updated_at=? WHERE id=?",
                    ("Docker 验收环境冲突将在 30 秒后重验：" + "、".join(environment_names),
                     now_iso(), pair_id),
                )
                return {"pairId": pair_id, "checks": results, "preserved": names,
                        "reused": environment_names, "retryScheduled": True}
        result_by_arm = {str(item.get("arm") or ""): item for item in results}
        for arm in selected:
            self._artifact_retry_after.pop(
                self._artifact_retry_key(pair_id, str(arm["arm"])), None,
            )
            if (result_by_arm.get(str(arm["arm"])) or {}).get("status") == "passed":
                self.db.audit("artifact.arm_passed", "arm_run", arm["id"], {
                    "pair_id": pair_id, "arm": arm["arm"], "commit_sha": arm["commit_sha"],
                    "reused": reused_by_arm.get(str(arm["arm"]), False),
                })
        current_arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        passed_count = 0
        observed_failed = []
        for arm in current_arms:
            if arm["status"] != "completed":
                continue
            if self.db.one(
                """SELECT id FROM artifact_checks WHERE pair_id=? AND arm=?
                   AND commit_sha=? AND status='passed'""",
                (pair_id, arm["arm"], arm["commit_sha"]),
            ):
                passed_count += 1
            elif self.db.one(
                """SELECT id FROM artifact_checks WHERE pair_id=? AND arm=?
                   AND commit_sha=? AND status='observed_failed'""",
                (pair_id, arm["arm"], arm["commit_sha"]),
            ):
                observed_failed.append(str(arm["arm"]))
        all_completed = len(current_arms) == 2 and all(
            arm["status"] == "completed" for arm in current_arms
        )
        if all_completed and passed_count + len(observed_failed) == 2 and observed_failed:
            requires_recording = passed_count > 0
            next_stage = "difficulty_review"
            if requires_recording:
                message = (
                    "Claude 原始交付的产物业务验收未通过，失败侧跳过录像；"
                    "通过侧完成录像后生成 GSB：" + "、".join(observed_failed)
                )
            else:
                message = (
                    "Claude 原始交付的产物业务验收未通过，已保留为 GSB 证据："
                    + "、".join(observed_failed)
                )
            self.db.execute(
                "UPDATE pairs SET status='running',stage=?,error=?,updated_at=? WHERE id=?",
                (next_stage, message, now_iso(), pair_id),
            )
            self.db.audit("artifact.pair_failure_ready_for_gsb", "pair", pair_id, {
                "failed_arms": observed_failed, "recording_required": requires_recording,
                "recording_arms": [
                    str(arm["arm"]) for arm in current_arms
                    if requires_recording and self.db.one(
                        """SELECT id FROM artifact_checks WHERE pair_id=? AND arm=?
                           AND commit_sha=? AND status='passed'""",
                        (pair_id, arm["arm"], arm["commit_sha"]),
                    )
                ],
                "difficulty_review_required": True,
            })
            self._submit_auto("difficulty-" + pair_id, self.reassess_actual_difficulty, pair_id)
            return {"pairId": pair_id, "checks": results, "preserved": observed_failed}
        if all_completed and passed_count == 2:
            self.db.execute(
                "UPDATE pairs SET status='running',stage='difficulty_review',error='',updated_at=? WHERE id=?",
                (now_iso(), pair_id),
            )
            self.db.audit("artifact.pair_passed", "pair", pair_id, {
                "rule": "both_current_commits_passed_then_actual_difficulty_review",
            })
            self._submit_auto(
                "difficulty-" + pair_id,
                self.reassess_actual_difficulty,
                pair_id,
            )
        else:
            self.db.execute(
                "UPDATE pairs SET status='running',stage=?,updated_at=? WHERE id=?",
                ("artifact_validation" if all_completed else "development", now_iso(), pair_id),
            )
            if all_completed:
                self._schedule_completed_arm_validations(pair_id)
        return {"pairId": pair_id, "checks": results}

    def _artifact_gate_complete(self, pair_id: str) -> bool:
        """Advance only after both delivered commits have terminal artifact evidence."""
        checks = self._current_artifact_checks(pair_id)
        if len(checks) != 2:
            return False
        if any(self._artifact_environment_failure(check) for check in checks if check.get("status") != "passed"):
            self.db.execute(
                "UPDATE pairs SET status='running',stage='artifact_validation',updated_at=? WHERE id=?",
                (now_iso(), pair_id),
            )
            self._schedule_completed_arm_validations(pair_id)
            return False
        statuses = {str(check.get("arm") or ""): str(check.get("status") or "") for check in checks}
        all_passed = all(statuses.get(arm) == "passed" for arm in ("A", "B"))
        self.db.execute(
            "UPDATE pairs SET status='running',stage='difficulty_review',error=?,updated_at=? WHERE id=?",
            ("" if all_passed else
             "Docker 验收失败已作为最终 GSB 证据保留，不启动 Claude 返修",
             now_iso(), pair_id),
        )
        self.db.audit(
            "artifact.pair_passed" if all_passed else "artifact.pair_evaluated_with_failures",
            "pair", pair_id,
            {"statuses": statuses, "claudeRepairStarted": False},
        )
        self._submit_auto("difficulty-" + pair_id, self.reassess_actual_difficulty, pair_id)
        return True

    @staticmethod
    def _artifact_environment_failure(check: Dict[str, Any]) -> bool:
        """Identify host/runtime collisions that do not invalidate a commit."""
        text = str(check.get("error") or "")
        try:
            items = json.loads(check.get("checks_json") or "[]")
        except ValueError:
            items = []
        text += "\n" + "\n".join(str(item.get("detail") or "") for item in items if not item.get("passed"))
        lowered = text.casefold()
        return (any(marker in lowered for marker in (
            "port is already allocated", "address already in use",
            "failed programming external connectivity", "network is still in use",
        )) or ("container name" in lowered
               and "is already in use by container" in lowered))

    def operation(self, operation_id: str) -> Dict[str, Any]:
        with self._future_lock:
            future = self._futures.get(operation_id)
        if not future:
            return {"id": operation_id, "status": "unknown"}
        if not future.done():
            return {"id": operation_id, "status": "running"}
        try:
            return {"id": operation_id, "status": "completed", "result": future.result()}
        except Exception as exc:
            return {"id": operation_id, "status": "failed", "error": str(exc)}

    def _submit(self, operation: str, fn, *args) -> None:
        with self._future_lock:
            existing = self._futures.get(operation)
            if existing and not existing.done():
                return
            self._futures[operation] = self.executor.submit(fn, *args)

    def _submit_monitor(self, operation: str, fn, *args) -> bool:
        """Run long-lived Claude monitoring without blocking user actions."""
        with self._future_lock:
            existing = self._futures.get(operation)
            if existing and not existing.done():
                return False
            self._futures[operation] = self.monitor_executor.submit(fn, *args)
            return True

    def _checkpoint_lock(self, arm_id: str):
        with self._checkpoint_locks_guard:
            lock = self._checkpoint_locks.get(arm_id)
            if lock is None:
                lock = threading.Lock()
                self._checkpoint_locks[arm_id] = lock
            return lock

    def _pair(self, pair_id: str) -> Dict[str, Any]:
        pair = self.db.one("SELECT * FROM pairs WHERE id=?", (pair_id,))
        if not pair:
            raise KeyError("Pair 不存在")
        return pair
