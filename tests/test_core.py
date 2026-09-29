import json
import hashlib
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from pairwise_console.commands import CommandResult, run_command
from pairwise_console.classification import normalize_language_framework, normalize_stack
from pairwise_console.config import OLD_APP_DIR, load_config
from pairwise_console.db import Database, now_iso
from pairwise_console.gitops import GitOps
from pairwise_console.analytics import dashboard
from pairwise_console.artifact import isolated_compose_environment
from pairwise_console.api import Handler
from pairwise_console.exports import build_xlsx
from pairwise_console.importer import import_historical_tasks
from pairwise_console.prompts import (
    delivery_assessment_prompt, feature_generation_prompt, generated_task_prompt_issues,
    repair_generated_task_punctuation,
    gsb_independent_recheck_prompt, gsb_prompt, gsb_recheck_prompt,
    task_generation_prompt, task_validation_prompt,
)
from pairwise_console.recording import (
    RecordingManager, minimum_recording_duration_seconds, recording_interaction_issue,
)
from pairwise_console.service import PairwiseService, contains_browser_verification


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = load_config(Path(__file__).resolve().parents[1])
        from dataclasses import replace
        self.config = replace(
            self.config,
            data_dir=self.root / "data",
            db_path=self.root / "data" / "test.db",
            projects_dir=self.root / "projects",
            old_db_path=self.root / "old.db",
            git_author_email="test@example.com",
        )
        self.db = Database(self.config.db_path)
        self.db.initialize()
        self.service = PairwiseService(self.config, self.db)

    def tearDown(self):
        self.service.executor.shutdown(wait=False, cancel_futures=True)
        self.service.monitor_executor.shutdown(wait=False, cancel_futures=True)
        self.temp.cleanup()

    def insert_ready_task(self):
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("task-1", "test", "zero_to_one", "hard-project", "Build a hard project with Docker Compose",
             "困难", '["跨模块状态","异常恢复"]', "fingerprint-1", "ready", stamp, stamp),
        )

    def insert_ready_bugfix_task(self, difficulty="中等"):
        self.insert_ready_task()
        source_pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=?,updated_at=? WHERE id=?",
            (stamp, stamp, source_pair["id"]),
        )
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               parent_pair_id,fingerprint,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("task-bugfix", "test", "bugfix", "medium bug repair", "Fix the reproduced bug",
             difficulty, '["跨模块调用","边界条件"]', source_pair["id"],
             "fingerprint-bugfix-" + difficulty, "ready", stamp, stamp),
        )
        return source_pair

    def mark_completed_feature_source(self, pair_id, completed_at=None):
        stamp = completed_at or now_iso()
        commit = (pair_id.replace("pair-", "") + "a" * 40)[:40]
        self.db.execute(
            """UPDATE pairs SET status='completed',stage='completed',winner='A better',
               completed_at=?,updated_at=? WHERE id=?""",
            (stamp, stamp, pair_id),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            (pair_id + "-source-a", pair_id, "A", "A", str(self.root), pair_id + "-container-a",
             pair_id + "-screen-a", "auto_model/urm", "image", commit, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,created_at,updated_at)
               VALUES(?,?, 'A',?,'passed','[]',?,?)""",
            (pair_id + "-source-check-a", pair_id, commit, stamp, stamp),
        )

    def test_feature_source_without_verified_verify_is_excluded_before_generation(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.mark_completed_feature_source(pair["id"])
        with patch.object(self.service.codex, "run") as generate:
            with self.assertRaisesRegex(ValueError, "缺少已验收的 verify 服务"):
                self.service.generate_followup_feature(pair["id"])
        generate.assert_not_called()
        self.assertFalse(any(
            source["id"] == pair["id"]
            for source in self.service._eligible_feature_sources()
        ))

    def test_feature_generation_stops_between_attempts_during_pipeline_drain(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.mark_completed_feature_source(pair["id"])
        self.db.execute(
            "UPDATE artifact_checks SET checks_json=? WHERE pair_id=? AND arm='A'",
            ('[{"name":"verify_service_present","passed":true}]', pair["id"]),
        )
        self.db.set_setting("pipeline_drain", True)
        with patch.object(self.service.codex, "run") as generate:
            with self.assertRaisesRegex(RuntimeError, "流水线已暂停"):
                self.service.generate_followup_feature(pair["id"])
        generate.assert_not_called()

    def test_artifact_recheck_preserves_previous_evidence_in_audit(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            ("check-original", pair["id"], "A", "a" * 40, "observed_failed",
             '[{"name":"verify_image_prepared","passed":false,"exit_code":1}]',
             "verify 镜像准备失败", stamp, stamp),
        )
        probe = {"compose_file": "", "status": "passed", "checks": [], "error": ""}
        with patch.object(self.service.artifacts, "_probe", return_value=probe):
            result = self.service.artifacts.validate(pair["id"], "A", self.root, "a" * 40)
        self.assertEqual(result["status"], "passed")
        row = self.db.one(
            """SELECT detail_json FROM audit_events
                 WHERE event_type='artifact.recheck_previous_evidence' AND entity_id=?""",
            (pair["id"],),
        )
        self.assertIsNotNone(row)
        previous = json.loads(row["detail_json"])
        self.assertEqual(previous["id"], "check-original")
        self.assertEqual(previous["status"], "observed_failed")
        self.assertIn("verify_image_prepared", previous["checks_json"])

    def test_defaults_use_codex_for_review_and_claude_for_development(self):
        self.assertEqual(self.db.setting("codex_model"), "gpt-5.6-sol")
        self.assertEqual(self.db.setting("codex_default_effort"), "medium")
        self.assertEqual(self.db.setting("codex_bug_effort"), "high")
        self.assertEqual(self.db.setting("claude_model"), "auto_model/urm")
        self.assertEqual(self.db.setting("github_visibility"), "public")
        self.assertEqual(self.db.setting("first_prompt_stop_minutes"), 60)
        self.assertEqual(self.db.setting("repeated_no_code_trace_minutes"), 40)
        self.assertEqual(self.db.setting("development_trace_stall_minutes"), 40)
        self.assertEqual(self.db.setting("business_progress_idle_minutes"), 60)
        self.assertEqual(self.db.setting("development_max_attempts"), 2)
        self.assertTrue(self.db.setting("task_generation_zero_to_one_only"))
        self.assertFalse(self.db.setting("manual_bug_only_mode"))
        self.assertFalse(self.db.setting("manual_bug_auto_refill_enabled"))
        self.assertEqual(self.db.setting("manual_bug_ready_target"), 6)
        self.assertEqual(self.db.setting("manual_bug_min_estimated_modules"), 2)
        self.assertEqual(self.db.setting("manual_bug_max_estimated_modules"), 4)
        self.assertEqual(self.db.setting("manual_bug_min_estimated_source_lines"), 80)
        self.assertEqual(self.db.setting("manual_bug_max_estimated_source_lines"), 250)
        self.assertEqual(self.db.setting("manual_bug_min_estimated_minutes"), 45)
        self.assertEqual(self.db.setting("manual_bug_target_estimated_minutes"), 90)
        self.assertEqual(self.db.setting("manual_bug_max_estimated_minutes"), 180)
        self.assertEqual(self.db.setting("ab_prompt_stagger_seconds"), 30)
        self.assertEqual(self.db.setting("max_claude_terminals"), 3)

    def test_new_submission_model_mix_is_pinned_and_rebalances_after_failure(self):
        self.insert_ready_task()
        existing = self.service.create_pair("task-1")
        self.service.claude.prepare_arm(existing, "B", self.root / "existing-b")
        self.db.execute("UPDATE pairs SET created_at=? WHERE id=?",
                        ("2020-01-01T00:00:00+00:00", existing["id"]))
        self.db.set_setting("ab_submission_model_policy", {
            "enabled": True, "startedAt": now_iso(), "legacyModel": "auto_model/urm",
            "aModel": "auto_model/urm", "bModel": "ark/urm-03",
        })
        self.db.set_setting("max_pairs_parallel", 5)
        created = []
        for index in range(2, 6):
            stamp = now_iso()
            task_id = "task-%d" % index
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
                   difficulty_evidence_json,fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, "test", "zero_to_one", "hard-%d" % index,
                 "Build a hard project with Docker Compose", "困难",
                 '["跨模块状态","异常恢复"]', "fingerprint-%d" % index,
                 "ready", stamp, stamp),
            )
            pair = self.service.create_pair(task_id)
            created.append(pair)
            for arm in ("A", "B"):
                self.service.claude.prepare_arm(pair, arm, self.root / (pair["id"] + arm))
        self.assertEqual([pair["model_scheme"] for pair in created[:3]],
                         ["cross_model", "legacy", "legacy"])
        self.assertEqual(self.db.one(
            "SELECT model FROM arm_runs WHERE pair_id=? AND arm='B'", (created[0]["id"],)
        )["model"], "ark/urm-03")
        self.assertEqual(self.db.one(
            "SELECT model FROM arm_runs WHERE pair_id=? AND arm='B'", (created[1]["id"],)
        )["model"], "auto_model/urm")
        self.assertEqual(created[3]["model_scheme"], "cross_model")
        self.db.execute("UPDATE pairs SET status='failed' WHERE id=?", (created[0]["id"],))
        self.assertEqual(self.service._new_pair_model_assignment()[0], "cross_model")
        # An old, unsent Arm is not changed by the policy or a global setting.
        self.db.set_setting("claude_model", "another-model")
        self.service.claude.prepare_arm(existing, "B", self.root / "existing-b")
        self.assertEqual(self.db.one(
            "SELECT model FROM arm_runs WHERE pair_id=? AND arm='B'", (existing["id"],)
        )["model"], "auto_model/urm")

    def test_cross_model_delivery_rejects_model_or_image_drift(self):
        detail = {"model_scheme": "cross_model", "model_a": "auto_model/urm",
                  "model_b": "ark/urm-03", "arms": [
                      {"arm": "A", "model": "auto_model/urm", "image": "same"},
                      {"arm": "B", "model": "auto_model/urm", "image": "different"},
                  ]}
        issues = self.service._pair_model_issues(detail)
        self.assertTrue(any("B 运行模型" in issue for issue in issues))
        self.assertTrue(any("镜像不同" in issue for issue in issues))

    def test_service_restart_migrates_old_trace_stall_window_to_40_minutes(self):
        self.db.set_setting("development_trace_stall_minutes", 15)
        PairwiseService(self.config, self.db)
        self.assertEqual(self.db.setting("development_trace_stall_minutes"), 40)

    def test_service_restart_migrates_manual_bug_scale_to_current_envelope(self):
        old_limits = {
            "manual_bug_min_estimated_modules": 2,
            "manual_bug_max_estimated_modules": 3,
            "manual_bug_min_estimated_source_lines": 60,
            "manual_bug_max_estimated_source_lines": 180,
            "manual_bug_min_estimated_minutes": 40,
            "manual_bug_target_estimated_minutes": 80,
            "manual_bug_max_estimated_minutes": 90,
        }
        for key, value in old_limits.items():
            self.db.set_setting(key, value)

        restarted = PairwiseService(self.config, self.db)
        try:
            self.assertEqual(self.db.setting("manual_bug_min_estimated_modules"), 2)
            self.assertEqual(self.db.setting("manual_bug_max_estimated_modules"), 4)
            self.assertEqual(self.db.setting("manual_bug_min_estimated_source_lines"), 80)
            self.assertEqual(self.db.setting("manual_bug_max_estimated_source_lines"), 250)
            self.assertEqual(self.db.setting("manual_bug_min_estimated_minutes"), 45)
            self.assertEqual(self.db.setting("manual_bug_target_estimated_minutes"), 90)
            self.assertEqual(self.db.setting("manual_bug_max_estimated_minutes"), 180)
        finally:
            restarted.executor.shutdown(wait=False, cancel_futures=True)
            restarted.monitor_executor.shutdown(wait=False, cancel_futures=True)

    def test_service_restart_migrates_generated_and_mix_caps_to_180_minutes(self):
        self.db.set_setting("generated_task_max_estimated_minutes", 120)
        self.db.set_setting("task_mix_policy", {
            "enabled": True, "phase": "balanced", "maxEstimatedMinutes": 120,
        })
        restarted = PairwiseService(self.config, self.db)
        try:
            self.assertEqual(self.db.setting("generated_task_max_estimated_minutes"), 180)
            self.assertEqual(self.db.setting("task_mix_policy")["maxEstimatedMinutes"], 180)
        finally:
            restarted.executor.shutdown(wait=False, cancel_futures=True)
            restarted.monitor_executor.shutdown(wait=False, cancel_futures=True)

    def test_old_scale_only_bug_rejection_is_restored_under_current_envelope(self):
        self.insert_ready_task()
        source_pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,preconditions,
               reproduction_steps_json,reproduction_commands_json,actual_result,expected_result,reproduce_count,
               difficulty,difficulty_evidence_json,estimated_module_count,estimated_source_lines_min,
               estimated_source_lines_max,estimated_minutes_min,estimated_minutes_max,complexity_axes_json,
               status,error,created_at,updated_at)
               VALUES('bug-old-scale',?,'A',?,'scale bug','ready','[]','[]','bad','good',2,
                      '困难','[]',3,160,240,75,90,'["state"]','difficulty_rejected',?, ?,?)""",
            (source_pair["id"], "a" * 40, "预计有效源码改动不在 60–180 行范围", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO tasks(id,source,source_id,task_type,title,prompt,difficulty,
               difficulty_evidence_json,fingerprint,estimated_module_count,estimated_source_lines_min,
               estimated_source_lines_max,estimated_minutes_min,estimated_minutes_max,status,rejection_reason,
               created_at,updated_at)
               VALUES('task-old-scale','bug_discovery','bug-old-scale','bugfix','scale bug','valid prompt',
                      '困难','[]','old-scale',3,160,240,75,90,'rejected',?, ?,?)""",
            ("旧版 Bug 任务已停用，需按当前准入规则重新生成："
             "预计有效源码改动不在 60–180 行范围", stamp, stamp),
        )

        self.assertEqual(self.service._restore_old_scale_rejected_bug_tasks(), 1)
        task = self.db.one("SELECT status,rejection_reason FROM tasks WHERE id='task-old-scale'")
        candidate = self.db.one("SELECT status,error FROM bug_candidates WHERE id='bug-old-scale'")
        self.assertEqual(task, {"status": "ready", "rejection_reason": ""})
        self.assertEqual(candidate, {"status": "converted", "error": ""})
        self.assertIn("task-old-scale", self.service._manual_bug_reserved_ids())

    def test_unrelated_bug_rejection_is_not_restored_by_scale_migration(self):
        self.insert_ready_task()
        source_pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,preconditions,
               reproduction_steps_json,reproduction_commands_json,actual_result,expected_result,reproduce_count,
               difficulty,difficulty_evidence_json,estimated_module_count,estimated_source_lines_min,
               estimated_source_lines_max,estimated_minutes_min,estimated_minutes_max,complexity_axes_json,
               status,error,created_at,updated_at)
               VALUES('bug-browser-rejected',?,'A',?,'browser bug','ready','[]','[]','bad','good',2,
                      '困难','[]',3,160,240,75,90,'["state"]','difficulty_rejected',?, ?,?)""",
            (source_pair["id"], "a" * 40, "复现依赖浏览器自动化；预计有效源码改动不在 60–180 行范围", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO tasks(id,source,source_id,task_type,title,prompt,difficulty,
               difficulty_evidence_json,fingerprint,estimated_module_count,estimated_source_lines_min,
               estimated_source_lines_max,estimated_minutes_min,estimated_minutes_max,status,rejection_reason,
               created_at,updated_at)
               VALUES('task-browser-rejected','bug_discovery','bug-browser-rejected','bugfix','browser bug',
                      'valid prompt','困难','[]','browser-rejected',3,160,240,75,90,'rejected',?, ?,?)""",
            ("复现依赖浏览器自动化", stamp, stamp),
        )

        self.assertEqual(self.service._restore_old_scale_rejected_bug_tasks(), 0)
        self.assertEqual(
            self.db.one("SELECT status FROM tasks WHERE id='task-browser-rejected'")["status"],
            "rejected",
        )

    def test_service_restart_does_not_retire_existing_ready_generated_tasks(self):
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("task-existing-generated", "generated", "zero_to_one", "已有待用题", "旧版短题面",
             "困难", '["已有验收依据"]', "existing-generated-fingerprint", "ready", stamp, stamp),
        )
        restarted = PairwiseService(self.config, self.db)
        try:
            task = self.db.one("SELECT status,rejection_reason FROM tasks WHERE id=?", ("task-existing-generated",))
            self.assertEqual(task["status"], "ready")
            self.assertFalse(task.get("rejection_reason"))
        finally:
            restarted.executor.shutdown(wait=False, cancel_futures=True)
            restarted.monitor_executor.shutdown(wait=False, cancel_futures=True)

    def test_legacy_task_mix_settings_are_removed(self):
        for key, value in (
            ("task_mix_zero_to_one", 7),
            ("task_mix_feature", 7),
            ("task_mix_bugfix", 10),
            ("task_mix_started_at", "2026-09-18T00:00:00+00:00"),
        ):
            self.db.set_setting(key, value)
        self.service._seed_settings()
        for key in (
            "task_mix_zero_to_one", "task_mix_feature",
            "task_mix_bugfix", "task_mix_started_at",
        ):
            self.assertIsNone(self.db.setting(key))

    def test_solo_qa_uses_fixed_repro_choice_and_rejects_medium_bugfix(self):
        values, _, issues = self.service._solo_qa_material({
            "task": {
                "task_type": "bugfix", "difficulty": "中等",
                "prompt": "Fix a reproduced race", "stack": "Python, FastAPI",
                "project_category": "纯后端",
            },
            "repository": {},
            "arms": [],
            "checks": [{"arm": "A", "status": "failed"}, {"arm": "B", "status": "passed"}],
            "recordings": [],
            "gsb": {},
            "delivery": {"status": "ready_to_submit", "remote_id": ""},
        })

        self.assertEqual(values["repro_level"], "已容器化，可一键起环境")
        self.assertIn("Docker 验收失败侧", values["remark"])
        self.assertIn("新提交题目的实际难度只允许困难或地狱", issues)
        self.assertIn("B 录像文件不存在", issues)
        self.assertNotIn("A 录像文件不存在", issues)
        self.assertNotIn("a_score_delivery", values)
        self.assertNotIn("b_desc_delivery", values)
        self.assertIn("A 交付完整性评分须为 1–5 的整数", issues)

    def test_solo_qa_uses_each_arms_recorded_model_name(self):
        values, _, issues = self.service._solo_qa_material({
            "task": {"task_type": "feature", "difficulty": "困难",
                     "prompt": "实现独立业务流程", "stack": "Python"},
            "repository": {},
            "model_scheme": "cross_model",
            "model_a": "auto_model/urm", "model_b": "ark/urm-03",
            "arms": [{"arm": "A", "model": "auto_model/urm"},
                     {"arm": "B", "model": "ark/urm-03"}],
            "checks": [], "recordings": [], "gsb": {}, "delivery": {},
        })
        self.assertEqual(values["a_model_name"], "auto_model/urm")
        self.assertEqual(values["b_model_name"], "ark/urm-03")
        self.assertFalse(any("运行模型" in issue or "缺少实际运行模型" in issue
                             for issue in issues))

    def test_restored_docker_contract_does_not_require_self_contained_evidence(self):
        detail = {
            "task": {
                "task_type": "zero_to_one", "difficulty": "困难",
                "prompt": "A self-contained application", "stack": "Python, FastAPI",
                "created_at": "2999-01-01T00:00:00+00:00",
            },
            "repository": {}, "arms": [], "recordings": [], "gsb": {},
            "delivery": {"status": "ready_to_submit", "remote_id": ""},
            "checks": [
                {"arm": arm, "status": "passed", "checks_json": '[{"name":"self_contained_source","passed":true}]'}
                for arm in ("A", "B")
            ],
        }
        values, _, issues = self.service._solo_qa_material(detail)
        self.assertEqual(values["repro_level"], "已容器化，可一键起环境")
        self.assertFalse(any("尚未证明代码本身无外部服务依赖" in issue for issue in issues))
        detail["checks"][1]["checks_json"] = "[]"
        _, _, issues = self.service._solo_qa_material(detail)
        self.assertFalse(any("尚未证明代码本身无外部服务依赖" in issue for issue in issues))

    def test_delivery_assessment_requires_independent_fact_based_descriptions(self):
        columns = {row["name"] for row in self.db.all("PRAGMA table_info(gsb_reviews)")}
        self.assertTrue({"a_score_delivery", "a_desc_delivery", "b_score_delivery", "b_desc_delivery"} <= columns)
        gsb = (
            "A 在 app/main.py 完成了主要流程，Docker 验收通过，正常接口返回结果符合题面；"
            "但边界条件仍未验证，不能据此断言全部功能完整，仍需要补齐边界输入的核对。"
        )
        review = {
            "a_reason": gsb, "b_reason": "B 在 API 处理异常路径时返回 500。",
            "reason": "A：" + gsb + " B：B 在 API 处理异常路径时返回 500。",
            "a_score_delivery": 4, "a_desc_delivery": gsb,
            "b_score_delivery": 3,
            "b_desc_delivery": "B 的 API 在边界请求返回 500，最终代码没有交付题面要求的异常裁决；正常请求虽可运行，但边界用户无法取得结果，所需输出也没有形成。",
        }
        issues = self.service._delivery_assessment_issues(review)
        self.assertIn("A 交付完整性描述不能照抄 GSB 理由", issues)
        review["a_desc_delivery"] = (
            "A 的 app/main.py 已实现正常提交和结果查询，清洁 Docker 启动及接口冒烟均通过；"
            "但题面要求的边界输入没有独立验收记录，尚不能按全部需求已核对处理。"
        )
        self.assertEqual(self.service._delivery_assessment_issues(review), [])
        review["b_desc_delivery"] = review["a_desc_delivery"]
        self.assertIn("A/B 交付完整性描述不能高度重复",
                      self.service._delivery_assessment_issues(review))

    def test_public_reviews_do_not_use_recordings_as_evidence(self):
        a_reason = "A 的 app/main.py 已实现查询，接口冒烟返回预期结果，录像也证明页面正常。"
        b_reason = "B 的 app/main.py 业务验收通过，异常输入返回符合题面的错误。"
        self.assertTrue(any("录像" in issue for issue in
                            self.service._gsb_conversational_issues(a_reason, b_reason)))
        review = {
            "a_reason": a_reason, "b_reason": b_reason,
            "a_score_delivery": 4, "b_score_delivery": 4,
            "a_desc_delivery": (
                "A 的 app/main.py 已实现正常查询，Docker 启动及接口冒烟通过；"
                "录像显示页面有结果，但边界输入尚未独立核对，不能视作完整交付。"
            ),
            "b_desc_delivery": (
                "B 的 app/main.py 已实现正常查询，接口冒烟返回符合预期的结果；"
                "异常输入也有独立验证，代码中的对应处理可以复查。"
            ),
        }
        self.assertIn("A 交付完整性描述不得引用录像作为依据",
                      self.service._delivery_assessment_issues(review))
        self.assertNotIn('"recording"', gsb_prompt("任务", '{}', '{}'))
        self.assertIn("不作为完整性评分", delivery_assessment_prompt("任务", "[]", "{}", "{}"))

    def test_solo_qa_repair_requires_fields_without_dropping_valid_prose(self):
        detail = {
            "task": {"task_type": "bugfix", "difficulty": "困难", "prompt": "修复业务问题", "stack": "Python"},
            "repository": {}, "arms": [], "checks": [], "recordings": [],
            "delivery": {"status": "needs_fix", "remote_id": "9910"},
            "gsb": {},
        }
        values, _, issues = self.service._solo_qa_material(detail)
        self.assertIn("A 交付完整性评分须为 1–5 的整数", issues)
        self.assertIn("B 交付完整性描述不能为空", issues)
        self.assertNotIn("a_score_delivery", values)

        detail["gsb"] = {
            "a_score_delivery": 2, "b_score_delivery": 3,
            "a_desc_delivery": "这侧交付了主要业务流程，但边界行为仍需复核。",
            "b_desc_delivery": "另一侧交付了可运行的流程，关键操作能够取得结果。",
        }
        values, _, issues = self.service._solo_qa_material(detail)
        self.assertEqual(values["a_score_delivery"], 2)
        self.assertEqual(values["b_score_delivery"], 3)
        self.assertEqual(values["a_desc_delivery"], detail["gsb"]["a_desc_delivery"])
        self.assertFalse(any("交付完整性" in issue for issue in issues))

    def test_delivery_assessment_prompt_asks_for_scenes_not_numeric_checklist(self):
        prompt = delivery_assessment_prompt("题面", "验收", "A证据", "B证据")
        self.assertIn("一两个最能说明交付情况的具体现场", prompt)
        self.assertIn("直接从 A 或 B 的实际交付事实写起", prompt)
        self.assertIn("不要另起“以……为判准”", prompt)
        self.assertIn("描述里不要报精确数字", prompt)
        self.assertIn("每侧须自然点出至少一处证据中确有的可复查定位", prompt)
        self.assertIn("不要再补一句笼统的“未见……”式保证", prompt)

    def test_delivery_assessment_rejects_generic_no_gap_ending(self):
        review = {
            "a_score_delivery": 4,
            "a_desc_delivery": (
                "A 的 /api/result 已经能返回当前草稿的结果，业务冒烟也覆盖了编辑后旧结果失效；"
                "使用者重新提交草稿会得到最新判断，未见声称完成而未落地的缺口。"
            ),
            "b_score_delivery": 3,
            "b_desc_delivery": (
                "B 的 /api/result 能返回当前结果，但编辑后仍保留旧结论；"
                "业务冒烟复现了这一问题，使用者可能误把过期判断当作最新结果。"
            ),
        }
        self.assertIn(
            "A 交付完整性描述不能以笼统的无缺口保证代替验收事实",
            self.service._delivery_assessment_issues(review),
        )

    def test_delivery_assessment_removes_standalone_scoring_preface(self):
        description = (
            "以结果随草稿更新为判准。"
            "A 的 /api/review 在已测场景能返回当前结果，但改稿后旧结果仍可见，"
            "使用者可能误用过期结论，交付只能算部分可用。"
        )
        cleaned = self.service._strip_delivery_assessment_lead(description, "A")
        self.assertTrue(cleaned.startswith("A 的 /api/review"))
        self.assertNotIn("为判准", cleaned)
        self.assertEqual(
            self.service._strip_delivery_assessment_lead(cleaned, "A"), cleaned,
        )
        review = {
            "a_score_delivery": 3, "a_desc_delivery": description,
            "b_score_delivery": 3,
            "b_desc_delivery": (
                "B 的 /api/review 能按当前草稿返回结果，容器中的业务冒烟已跑通；"
                "但页面修改输入后的旧结果清理尚未核对，使用者仍可能读到过期结论。"
            ),
        }
        self.assertIn(
            "A 交付完整性描述不应另起模板化判准开场白",
            self.service._delivery_assessment_issues(review),
        )

    def test_delivery_assessment_only_populates_unsubmitted_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("assessment-" + arm, pair["id"], arm, arm, str(self.root),
                 "container-" + arm, "screen-" + arm, "model", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,
                   created_at,updated_at) VALUES(?,?,?,?, 'passed','[]',?,?)""",
                ("assessment-check-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,
               created_at,updated_at) VALUES(?,?,?,?,?,?,'confirmed',?,?)""",
            ("assessment-review", pair["id"], "Same", "A：有证据。 B：有证据。",
             "A 在 API 完成正常路径。", "B 在 API 完成正常路径。", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,created_at,updated_at)
               VALUES(?,?,'ready_to_submit',?,?)""",
            ("assessment-delivery", pair["id"], stamp, stamp),
        )
        result = {
            "aScoreDelivery": 4,
            "aDescDelivery": "A 的 app/main.py 已覆盖正常提交与结果读取，Docker 清洁启动及 API 冒烟通过；但题面要求的边界输入没有独立核对记录，完整性只能按主要功能达成评估。",
            "bScoreDelivery": 3,
            "bDescDelivery": "B 的 API 正常请求可以返回结果，但给出边界输入时出现错误，最终产物未交付对应的异常裁决，用户无法完成该路径；Docker 启动通过不改变这一缺口。",
        }
        with patch.object(self.service, "_difficulty_arm_evidence", return_value={"traceEvidence": {"events": []}}), \
             patch.object(self.service.codex, "run", return_value=result) as run:
            updated = self.service.generate_delivery_assessment(pair["id"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(updated["a_score_delivery"], 4)
        self.assertEqual(updated["b_score_delivery"], 3)
        self.db.execute(
            "UPDATE delivery_submissions SET remote_id='123',status='qc_passed' WHERE pair_id=?",
            (pair["id"],),
        )
        with self.assertRaisesRegex(ValueError, "仅为本地待提交"):
            self.service.generate_delivery_assessment(pair["id"])

    def test_manual_delivery_assessment_edit_preserves_gsb_and_blocks_stale_or_submitted(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("manual-assessment-" + arm, pair["id"], arm, arm, str(self.root),
                 "container-" + arm, "screen-" + arm, "model", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,
                   created_at,updated_at) VALUES(?,?,?,?, 'passed','[]',?,?)""",
                ("manual-check-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,
               created_at,updated_at) VALUES(?,?,?,?,?,?,'confirmed',?,?)""",
            ("manual-assessment-review", pair["id"], "Same", "A：原评价 B：原评价",
             "A 在 app/main.py 实现了正常路径。", "B 的 API 对边界请求会报错。", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,created_at,updated_at)
               VALUES(?,?,'ready_to_submit',?,?)""",
            ("manual-assessment-delivery", pair["id"], stamp, stamp),
        )
        expected = {"a_score_delivery": 0, "a_desc_delivery": "",
                    "b_score_delivery": 0, "b_desc_delivery": ""}
        a_desc = ("A 的 app/main.py 已能完成正常提交并取回结果，清洁 Docker 验收也通过；"
                  "但边界输入没有独立核对，不能把这一侧视为所有要求都已验证。")
        b_desc = ("B 的 API 对正常请求可以返回结果，边界请求却会报错；即使服务能够启动，"
                  "使用者仍无法在这一场景取得题面要求的裁决。该路径的交付仍有缺口，不能仅凭健康检查视为完整。")
        with self.assertRaisesRegex(ValueError, "1–5"):
            self.service.update_delivery_assessment(
                pair["id"], 6, a_desc, 3, b_desc, stamp, expected,
            )
        saved = self.service.update_delivery_assessment(
            pair["id"], 4, a_desc, 3, b_desc, stamp, expected,
        )
        self.assertEqual((saved["a_score_delivery"], saved["b_score_delivery"]), (4, 3))
        self.assertEqual(saved["verdict"], "Same")
        self.assertEqual(saved["a_reason"], "A 在 app/main.py 实现了正常路径。")
        handler = object.__new__(Handler)
        handler.server = MagicMock(db=self.db)
        review_row = handler._reviews_page({"q": [pair["id"]]})["items"][0]
        self.assertEqual(review_row["a_desc_delivery"], a_desc)
        self.assertEqual(review_row["b_score_delivery"], 3)
        with self.assertRaisesRegex(ValueError, "已被其他操作修改"):
            self.service.update_delivery_assessment(
                pair["id"], 4, a_desc, 3, b_desc, stamp, expected,
            )
        self.db.execute(
            "UPDATE delivery_submissions SET status='qc_passed',remote_id='remote-1' WHERE pair_id=?",
            (pair["id"],),
        )
        with self.assertRaisesRegex(ValueError, "只有本地待提交或待返修"):
            self.service.update_delivery_assessment(
                pair["id"], 4, a_desc, 3, b_desc, saved["updated_at"],
                {"a_score_delivery": 4, "a_desc_delivery": a_desc,
                 "b_score_delivery": 3, "b_desc_delivery": b_desc},
            )

    def test_delivery_assessment_scheduler_skips_backed_off_pair(self):
        with patch.object(self.db, "one", return_value=None), \
             patch.object(self.db, "all", return_value=[{"pair_id": "first"}, {"pair_id": "second"}]), \
             patch.object(self.service, "_submit_auto", side_effect=[False, True]) as submit:
            self.service._schedule_missing_delivery_assessment()
        self.assertEqual(
            [call.args[0] for call in submit.call_args_list],
            ["delivery-assessment-first", "delivery-assessment-second"],
        )

    def test_service_restart_releases_interrupted_generation_batches(self):
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO generation_batches(id,status,requested_count,created_at,updated_at)
               VALUES('batch-interrupted','running',1,?,?)""",
            (stamp, stamp),
        )

        restarted = PairwiseService(self.config, self.db)
        try:
            batch = self.db.one(
                "SELECT status,error,finished_at FROM generation_batches WHERE id='batch-interrupted'"
            )
            self.assertEqual(batch["status"], "failed")
            self.assertIn("服务重启", batch["error"])
            self.assertTrue(batch["finished_at"])
            event = self.db.one(
                """SELECT detail_json FROM audit_events
                   WHERE event_type='automation.generation_batches_recovered'
                   ORDER BY id DESC LIMIT 1"""
            )
            self.assertEqual(json.loads(event["detail_json"])["count"], 1)
        finally:
            restarted.executor.shutdown(wait=False, cancel_futures=True)
            restarted.monitor_executor.shutdown(wait=False, cancel_futures=True)
        self.assertIsNone(self.db.setting("task_mix_zero_to_one"))
        self.assertIsNone(self.db.setting("task_mix_feature"))
        self.assertIsNone(self.db.setting("task_mix_bugfix"))

    def test_service_restart_requeues_interrupted_bug_reproduction(self):
        self.insert_ready_task()
        source_pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,preconditions,
               reproduction_steps_json,reproduction_commands_json,actual_result,expected_result,difficulty,
               difficulty_evidence_json,status,created_at,updated_at)
               VALUES('bug-interrupted-reproduction',?,'A',?,'interrupted','ready','[]','[]',
                      'bad','good','困难','[]','reproducing',?,?)""",
            (source_pair["id"], "a" * 40, stamp, stamp),
        )

        restarted = PairwiseService(self.config, self.db)
        try:
            candidate = self.db.one(
                "SELECT status,error FROM bug_candidates WHERE id='bug-interrupted-reproduction'"
            )
            self.assertEqual(candidate["status"], "awaiting_reproduction")
            self.assertIn("可重试", candidate["error"])
            event = self.db.one(
                """SELECT detail_json FROM audit_events
                   WHERE event_type='bug.reproductions_recovered'
                   ORDER BY id DESC LIMIT 1"""
            )
            self.assertEqual(json.loads(event["detail_json"])["count"], 1)
        finally:
            restarted.executor.shutdown(wait=False, cancel_futures=True)
            restarted.monitor_executor.shutdown(wait=False, cancel_futures=True)

    def test_service_restart_closes_stale_generation_batches(self):
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO generation_batches(id,status,requested_count,created_at,updated_at)
               VALUES('batch-stale','running',1,?,?)""",
            (stamp, stamp),
        )
        self.service._recover_interrupted_background_jobs()
        batch = self.db.one(
            "SELECT status,error,finished_at FROM generation_batches WHERE id='batch-stale'",
        )
        self.assertEqual(batch["status"], "failed")
        self.assertIn("结束陈旧状态", batch["error"])
        self.assertTrue(batch["finished_at"])

    def test_task_prompts_prejudge_minimum_necessary_complexity(self):
        validation = task_validation_prompt("task", "known", "baseline", "rejected examples")
        generated = task_generation_prompt("known", "zero_to_one")
        feature = feature_generation_prompt("original", "artifact", "known", "全栈")
        self.assertIn("最小实现", validation)
        self.assertIn("准确基线", validation)
        self.assertIn("近期开发完成后的真实难度案例", validation)
        self.assertIn("Bug 修复也不得因真实复现而例外", validation)
        self.assertIn("一个可独立验收的工程核心", generated)
        self.assertIn("四至六个完整中文句子", generated)
        self.assertIn("只增加一个工程核心", feature)
        self.assertIn("不增加独立运行组件", feature)
        self.assertIn("runtimeComponents 仅指本轮新增且需独立部署", feature)
        self.assertIn("该字段必须是空数组 []", feature)
        self.assertIn("Dockerfile、Docker Compose、健康检查", generated)
        self.assertIn("Docker Compose/verify 验收链路", feature)
        self.assertIn("页面功能由系统在仓库外另行操作验收和录像", validation)
        self.assertIn("总上限为 180 分钟", generated)
        self.assertIn("总上限为 180 分钟", feature)
        self.assertIn("Docker/verify 交付验证的完整时间", generated)
        self.assertIn("Docker/verify 交付验证的完整时间", feature)
        self.assertIn("不得只把 estimatedMinutesMin/Max 写小", generated)
        self.assertIn("下一候选必须减去真实开发范围", feature)
        self.assertIn("超出时如实记录并拒绝", validation)
        self.assertNotIn("至少两个相互制约", generated)
        self.assertNotIn("至少两个当前不存在", feature)

    def test_task_generation_prompt_can_require_frontend_or_fullstack(self):
        frontend = task_generation_prompt("known", "zero_to_one", "纯前端")
        fullstack = task_generation_prompt("known", "zero_to_one", "全栈")
        self.assertIn("本次必须生成纯前端", frontend)
        self.assertIn("不得创建业务后端", frontend)
        self.assertIn("本次必须生成全栈", fullstack)
        self.assertIn("页面操作触发前后端联调", fullstack)
        self.assertIn("项目内 verify 不运行浏览器自动化", frontend)
        self.assertIn("项目内 verify 不运行浏览器自动化", fullstack)

    def test_zero_to_one_category_preference_balances_ready_pool(self):
        self.db.set_setting("zero_to_one_preferred_categories", ["纯前端", "全栈"])
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,project_category,
               difficulty_evidence_json,fingerprint,status,created_at,updated_at)
               VALUES('task-front','generated','zero_to_one','前端题','前端复杂任务','困难','纯前端',
                      '[]','front-ready','ready',?,?)""",
            (stamp, stamp),
        )
        self.assertEqual(self.service._preferred_zero_to_one_generation_category(), "全栈")

    def test_ready_task_selection_prefers_requested_project_shapes(self):
        self.db.set_setting("zero_to_one_preferred_categories", ["纯前端", "全栈"])
        for task_id, category, created_at in (
            ("task-backend-old", "纯后端", "2026-01-01T00:00:00+00:00"),
            ("task-fullstack-new", "全栈", "2026-01-02T00:00:00+00:00"),
        ):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,project_category,
                   difficulty_evidence_json,fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'困难',?,'[]',?,'ready',?,?)""",
                (task_id, "generated", "zero_to_one", task_id, task_id + " hard task",
                 category, task_id, created_at, created_at),
            )
        self.assertEqual(self.service._next_ready_task()["id"], "task-fullstack-new")

    def test_manual_priority_queue_is_an_ordered_ready_task_allow_list(self):
        self.db.set_setting("task_generation_zero_to_one_only", False)
        rows = (
            ("task-reserved-later", "bugfix", "修复专用状态压缩溢出", "修复专用状态压缩溢出并保留 Docker Compose 自动化验收。", "2026-01-03T00:00:00+00:00"),
            ("task-reserved-first", "bugfix", "修复精确计数边界", "修复精确计数边界并保留 Docker Compose 自动化验收。", "2026-01-02T00:00:00+00:00"),
            ("task-unrelated-old", "zero_to_one", "无关旧题", "实现一个无关旧项目并提供 Docker Compose。", "2026-01-01T00:00:00+00:00"),
        )
        for task_id, task_type, title, prompt, created_at in rows:
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
                   difficulty_evidence_json,fingerprint,estimated_module_count,
                   estimated_source_lines_min,estimated_source_lines_max,
                   estimated_minutes_min,estimated_minutes_max,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'困难','[]',?,3,80,160,60,80,'ready',?,?)""",
                (task_id, "test", task_type, title, prompt, task_id, created_at, created_at),
            )
        self.db.set_setting("manual_priority_task_pause", {
            "active": True,
            "reservedTaskIds": ["task-reserved-later", "task-reserved-first"],
        })

        self.assertEqual(self.service._next_ready_task()["id"], "task-reserved-later")
        self.db.execute("UPDATE tasks SET status='used' WHERE id='task-reserved-later'")
        self.assertEqual(self.service._next_ready_task()["id"], "task-reserved-first")
        self.db.execute("UPDATE tasks SET status='used' WHERE id='task-reserved-first'")
        self.assertIsNone(self.service._next_ready_task())

    def test_manual_bug_only_mode_ignores_zero_to_one_selection_and_uses_allow_list(self):
        self.db.set_setting("manual_bug_only_mode", True)
        self.db.set_setting("task_generation_zero_to_one_only", True)
        stamp = now_iso()
        for task_id, task_type in (("task-zero", "zero_to_one"), ("task-bug", "bugfix")):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
                   difficulty_evidence_json,fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'困难','[]',?,'ready',?,?)""",
                (task_id, "manual_bug_injection" if task_type == "bugfix" else "generated",
                 task_type, task_id, "hard task " + task_id, task_id, stamp, stamp),
            )
        self.db.set_setting("manual_priority_task_pause", {
            "active": True, "reservedTaskIds": ["task-bug"],
        })

        self.assertEqual(self.service._next_ready_task()["id"], "task-bug")
        self.assertEqual(self.service._next_ready_task("zero_to_one")["id"], "task-bug")
        self.assertEqual(self.service.automation_status()["taskSelectionMode"], "manual_bug_only")

    def test_manual_bug_only_mode_rejects_medium_bug_even_when_allow_listed(self):
        self.db.set_setting("manual_bug_only_mode", True)
        stamp = now_iso()
        for task_id, difficulty in (("task-medium-bug", "中等"), ("task-hard-bug", "困难")):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
                   difficulty_evidence_json,fingerprint,estimated_module_count,
                   estimated_source_lines_min,estimated_source_lines_max,
                   estimated_minutes_min,estimated_minutes_max,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,'[]',?,3,80,160,60,80,'ready',?,?)""",
                (task_id, "manual_bug_injection", "bugfix", task_id,
                 "bug task " + task_id, difficulty, task_id, stamp, stamp),
            )
        self.db.set_setting("manual_priority_task_pause", {
            "active": True,
            "reservedTaskIds": ["task-medium-bug", "task-hard-bug"],
        })

        self.assertEqual(self.service._next_ready_task()["id"], "task-hard-bug")
        self.assertEqual(self.service.automation_status()["readyTasks"], 1)
        with self.assertRaisesRegex(ValueError, "困难或地狱"):
            self.service.create_pair("task-medium-bug")

    def test_medium_bug_cannot_start_outside_bug_only_mode(self):
        self.insert_ready_bugfix_task("中等")
        with self.assertRaisesRegex(ValueError, "困难或地狱"):
            self.service.create_pair("task-bugfix")

    def test_medium_bug_candidate_cannot_convert_after_reproduction(self):
        source = self.insert_ready_bugfix_task("困难")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,
               preconditions,reproduction_steps_json,actual_result,expected_result,
               reproduce_count,difficulty,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("bug-medium", source["id"], "A", "a" * 40, "局部修复", "合法输入",
             '["提交请求"]', "错误", "正确", 2, "中等", "reproduced", stamp, stamp),
        )
        with self.assertRaisesRegex(ValueError, "困难或地狱"):
            self.service.convert_bug_to_task("bug-medium")
        self.assertIsNone(self.db.one("SELECT id FROM tasks WHERE source_id='bug-medium'"))

    def test_task_validation_rejects_hard_label_without_distinct_evidence(self):
        self.insert_ready_task()
        self.db.execute("UPDATE tasks SET status='candidate' WHERE id='task-1'")
        result = {
            "accepted": True, "difficulty": "困难", "difficultyEvidence": ["跨模块"],
            "banned": False, "duplicate": False, "baselineReady": True,
            "reason": "可以准入",
        }
        with patch.object(self.service.codex, "run", return_value=result):
            reviewed = self.service.validate_task("task-1")
        self.assertEqual(reviewed["status"], "rejected")
        self.assertIn("两条不同的困难难度依据", reviewed["result"]["reason"])

    def test_bug_independent_review_blocks_medium_before_pool(self):
        source = self.insert_ready_bugfix_task("困难")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-source", source["id"], "A", "A", str(self.root), "container",
             "screen", "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,
               preconditions,reproduction_steps_json,actual_result,expected_result,
               reproduce_count,difficulty,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("bug-review-medium", source["id"], "A", "a" * 40, "局部缺陷", "合法输入",
             '["提交请求"]', "错误", "正确", 2, "困难", "reproduced", stamp, stamp),
        )
        review = {
            "accepted": True, "difficulty": "中等", "difficultyEvidence": [
                "只需扩展现有条件分支", "准确基线已经提供核心状态处理",
            ], "banned": False, "duplicate": False, "baselineReady": True,
            "reason": "最小修复仅是局部判断",
        }
        with patch.object(self.service, "_generate_bugfix_task_prompt", return_value="修复合法输入的局部错误"), \
             patch.object(self.service, "_create_isolated_bug_baseline", return_value=(self.root, "d" * 40)), \
             patch.object(self.service.codex, "run", return_value=review) as run:
            with self.assertRaisesRegex(ValueError, "未证明困难难度"):
                self.service.convert_bug_to_task("bug-review-medium")
        self.assertNotIn('"difficulty": "困难"', run.call_args.args[1])
        self.assertEqual(self.db.one("SELECT status FROM bug_candidates WHERE id='bug-review-medium'")["status"],
                         "difficulty_rejected")
        self.assertIsNone(self.db.one("SELECT id FROM tasks WHERE source_id='bug-review-medium'"))

    def test_manual_bug_only_mode_blocks_non_bug_pair_and_automatic_refill(self):
        self.insert_ready_task()
        self.db.set_setting("manual_bug_only_mode", True)
        with patch.object(self.service, "_schedule_task_source") as schedule, \
             patch.object(self.service, "validate_task_async") as validate:
            self.service._schedule_refill_once()
        schedule.assert_not_called()
        validate.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Bug-only"):
            self.service.create_pair("task-1")
        with self.assertRaisesRegex(ValueError, "停用自动出题"):
            self.service.generate_tasks(1, "zero_to_one")

    def test_manual_bug_auto_refill_counts_only_allow_listed_unpaired_hard_tasks(self):
        self.db.set_setting("manual_bug_only_mode", True)
        self.db.set_setting("manual_bug_auto_refill_enabled", True)
        self.db.set_setting("manual_bug_ready_target", 6)
        stamp = now_iso()
        for task_id, difficulty in (("task-reserved", "困难"), ("task-unreserved", "地狱")):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
                   difficulty_evidence_json,fingerprint,estimated_module_count,
                   estimated_source_lines_min,estimated_source_lines_max,
                   estimated_minutes_min,estimated_minutes_max,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,'[]',?,3,80,160,60,80,'ready',?,?)""",
                (task_id, "bug_discovery", "bugfix", task_id, "hard bug " + task_id,
                 difficulty, task_id, stamp, stamp),
            )
        self.db.set_setting("manual_priority_task_pause", {
            "active": True, "reservedTaskIds": ["task-reserved"],
        })

        self.assertEqual(self.service._manual_bug_ready_count(), 1)
        with patch.object(self.service, "_schedule_task_source", return_value=True) as schedule:
            self.service._schedule_refill_once()
        schedule.assert_called_once_with("bugfix")

    def test_manual_bug_refill_stops_at_target(self):
        self.db.set_setting("manual_bug_only_mode", True)
        self.db.set_setting("manual_bug_auto_refill_enabled", True)
        self.db.set_setting("manual_bug_ready_target", 1)
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
               difficulty_evidence_json,fingerprint,estimated_module_count,
               estimated_source_lines_min,estimated_source_lines_max,
               estimated_minutes_min,estimated_minutes_max,status,created_at,updated_at)
               VALUES('task-reserved','bug_discovery','bugfix','hard bug','hard bug prompt',
                      '困难','[]','manual-target-fingerprint',3,80,160,60,80,'ready',?,?)""",
            (stamp, stamp),
        )
        self.db.set_setting("manual_priority_task_pause", {
            "active": True, "reservedTaskIds": ["task-reserved"],
        })

        with patch.object(self.service, "_schedule_task_source") as schedule:
            self.service._schedule_refill_once()
        schedule.assert_not_called()

    def test_manual_bug_admission_rejects_oversized_or_long_candidates(self):
        base = {
            "difficulty": "困难",
            "estimatedModuleCount": 3,
            "estimatedSourceLinesMin": 80,
            "estimatedSourceLinesMax": 180,
            "estimatedMinutesMin": 45,
            "estimatedMinutesMax": 90,
            "reproductionCommands": [["exec", "-T", "api", "pytest"]],
        }
        self.assertEqual(self.service._manual_bug_candidate_issues(base), [])
        self.assertEqual(self.service._manual_bug_candidate_issues(
            dict(base, estimatedMinutesMax=180)), [])

        cases = (
            ({"estimatedModuleCount": 5}, "2–4 个范围"),
            ({"estimatedSourceLinesMax": 251}, "80–250 行范围"),
            ({"estimatedMinutesMin": 91}, "45–90 分钟"),
            ({"estimatedMinutesMax": 181}, "不得超过 180 分钟"),
        )
        for changes, message in cases:
            candidate = dict(base)
            candidate.update(changes)
            self.assertTrue(
                any(message in issue for issue in self.service._manual_bug_candidate_issues(candidate)),
                (changes, self.service._manual_bug_candidate_issues(candidate)),
            )

    def test_bug_prompt_rejects_reused_fixed_opening_or_closing(self):
        stamp = now_iso()
        opening = "某类设备在一组完全合法的边界输入下会稳定给出错误裁决，现象已经在两套清洁环境中重复确认。"
        closing = "外部验收会重复提交边界、小规模、唯一、多解和无解场景，并核对原有 Docker Compose 与 verify 仍正常退出。"
        existing = opening + ("甲" * 180) + closing
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
               difficulty_evidence_json,fingerprint,status,created_at,updated_at)
               VALUES('task-template-source','bug_discovery','bugfix','existing',?,'困难','[]',
                      'template-source-fingerprint','used',?,?)""",
            (existing, stamp, stamp),
        )

        opening_issues = self.service._bug_prompt_template_issues(
            opening + ("乙" * 260) + "这是完全不同的自然结尾。",
        )
        closing_issues = self.service._bug_prompt_template_issues(
            "这是完全不同的业务开头。" + ("丙" * 260) + closing,
        )

        self.assertIn("题面复用了已有 Bug 的固定开头", opening_issues)
        self.assertIn("题面复用了已有 Bug 的固定结尾", closing_issues)

    def test_bug_prompt_rejects_reused_compose_lead_after_unique_title(self):
        stamp = now_iso()
        existing = (
            "甲场景出现错误结论。保留现有 Docker Compose 启动、健康检查和一次性 verify 验收链路时，"
            "提交合法边界输入并观察结果。" + "甲" * 180
        )
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
               difficulty_evidence_json,fingerprint,status,created_at,updated_at)
               VALUES('task-compat-source','bug_discovery','bugfix','existing',?,'困难','[]',
                      'compat-source-fingerprint','used',?,?)""",
            (existing, stamp, stamp),
        )
        another = (
            "完全不同的乙场景无法给出正确时刻。保持现有 Docker Compose 启动、健康检查和一次性 verify 验收链路可用时，"
            "核对另一组边界输入。" + "乙" * 180
        )
        issues = self.service._bug_prompt_template_issues(another)
        self.assertIn("题面复用了已有 Bug 的 Compose/verify 开头骨架", issues)

    def test_bug_prompt_rejects_generic_compose_verify_opening_or_closing(self):
        business = (
            "合法边界输入会返回错误裁决，两次清洁环境复现的结果一致。"
            + "甲" * 220
        )
        stock = "现有 Docker Compose 启动方式与 verify 链路仍须正常运行。"

        closing = self.service._bugfix_prompt_issues(business + "\n\n" + stock)
        opening = self.service._bugfix_prompt_issues(stock + "\n\n" + business)

        self.assertTrue(any("固定结尾" in issue for issue in closing), closing)
        self.assertTrue(any("固定开头" in issue for issue in opening), opening)

    def test_new_bug_prompt_may_rely_on_existing_compose_acceptance(self):
        business = (
            "合法输入完成第一次审计后，再次提交同一对象会得到前一次请求的结果。"
            "第二次请求虽然使用了新的约束，返回的明细仍指向旧约束，影响后续状态裁决。"
            "正确行为是每次请求按当前输入独立计算，同时保持同一请求内的稳定顺序。"
            "系统会核对连续请求、重试和互不相关对象的结果，以及现有代码测试的通过情况。"
        )
        self.assertFalse(any("容器验收" in issue for issue in
                             self.service._bugfix_prompt_issues(business, new_task=True)))
        with_docker = business + "开发者还要运行 Docker Compose 中的一次性验收服务。"
        self.assertFalse(any("新 Bug 题面不得包含容器" in issue for issue in
                             self.service._bugfix_prompt_issues(with_docker, new_task=True)))
        self.assertFalse(any("新 Bug 题面" in issue for issue in
                             self.service._bugfix_prompt_issues(with_docker)))

    def test_bug_prompt_rejects_generic_compose_closing_paragraph_lead(self):
        business = "合法边界输入会返回错误裁决，两次清洁环境复现的结果一致。" + "甲" * 220
        prompt = (
            business + "\n\n修复后仍须能按现有 Docker Compose 方式启动并执行既有验收流程。"
            "验收将核对目标项目的业务结论。"
        )
        issues = self.service._bugfix_prompt_issues(prompt)
        self.assertTrue(any("独立收尾段" in issue for issue in issues), issues)

    def test_bug_prompt_rejects_generic_compose_verify_sentence_in_middle(self):
        prompt = (
            "合法边界输入会被误判为无解，调用方无法得到结果。"
            "修复后应返回可行方案并保持原有排序。"
            "既有 Docker Compose 启动、健康检查和一次性 verify 验收链路须继续可用。"
            "验收会核对该输入及相邻边界的业务结果。"
        )
        issues = self.service._bugfix_prompt_issues(prompt)
        self.assertTrue(any("独立模板句" in issue for issue in issues), issues)

    def test_bug_prompt_rejects_generic_compose_verify_semicolon_clause(self):
        prompt = (
            "合法边界输入会被误判为无解，调用方无法得到结果。"
            "使用五个节点和十条候选关系后返回成本更高的方案；"
            "既有 Docker Compose 启动、健康检查和一次性 verify 验收链路须继续可用。"
            "正确行为是返回成本较低的方案，回归还应核对同价排序。"
        )
        issues = self.service._bugfix_prompt_issues(prompt)
        self.assertTrue(any("分号子句" in issue for issue in issues), issues)

    def test_balanced_automation_status_reports_active_bug_refill(self):
        self.db.set_setting("task_mix_policy", {"enabled": True, "phase": "balanced"})
        self.db.set_setting("auto_refill_enabled", True)
        self.db.set_setting("manual_bug_auto_refill_enabled", True)
        self.assertTrue(self.service.automation_status()["bugAutoRefillEnabled"])
        self.db.set_setting("auto_refill_enabled", False)
        self.assertFalse(self.service.automation_status()["bugAutoRefillEnabled"])

    def test_bug_prompt_rejects_internal_numeric_mechanism_hint(self):
        prompt = (
            "挂装裁决录入合法的严格上界后仍返回越界方案，实际结果与物理约束不符。"
            "验收会核对该输入、恰等于边界的合法输入和既有排序。"
            "不得因数值表示或比较误差把严格小于一的上界当作一。"
            "期望结果是在越界场景返回不可行，而在恰等于边界时保留可行方案。"
        )
        issues = self.service._bugfix_prompt_issues(prompt)
        self.assertTrue(any("内部数值机制" in issue for issue in issues), issues)

    def test_bug_prompt_rejects_compose_compatibility_at_sentence_edges(self):
        body = (
            "有限边界输入会把有效方案误判为退化，返回的裕量也不可信。"
            "同一输入应保留四个支撑点，并返回有限的正裕量。"
        )
        opening = "在现有 Docker Compose 服务健康后提交合法输入。" + body
        closing = body + "该场景在原有 Compose 启动和验收链路中仍须正确完成。"
        middle = (
            "有限边界输入会把有效方案误判为退化。"
            "在现有 Compose 启动和 verify 链路中回归该输入及普通输入，结果应一致。"
            "同一输入应保留四个支撑点，并返回有限的正裕量。"
        )
        self.assertTrue(any("开头句" in issue for issue in self.service._bugfix_prompt_issues(opening)))
        self.assertTrue(any("结尾句" in issue for issue in self.service._bugfix_prompt_issues(closing)))
        self.assertFalse(any("开头句" in issue or "结尾句" in issue
                             for issue in self.service._bugfix_prompt_issues(middle)))

    def test_bug_prompt_relocates_existing_compatibility_clause(self):
        draft = (
            "合法输入会把最早超限证据与峰值证据混在一起。"
            "首项证据的时间、位置和角速度必须指向同一时刻。"
            "普通标记仍应保持原有裁决。"
            "回归验收要核对最早边界及普通标记；既有 Docker Compose 启动、健康检查和一次性 verify 链路继续可用。"
        )
        fixed = self.service._relocate_bug_compatibility_clause(draft)
        self.assertEqual(fixed.count("Docker Compose"), 1)
        self.assertTrue(fixed.endswith("回归验收要核对最早边界及普通标记。"))
        self.assertFalse(any("开头句" in issue or "结尾句" in issue
                             for issue in self.service._bugfix_prompt_issues(fixed)))

    def test_bug_prompt_keeps_clean_reproduction_evidence_private(self):
        prompt = (
            "上传会话封存后，有两个文件块损坏，修复进程在恢复第一个块后中断。"
            "两次清洁 Docker 环境中的实际结果一致：重启后复核始终报告第二个块未修复。"
            "正确行为是无需重新上传原文件即可继续恢复，复核最终返回健康状态。"
            "验收会核对持久卷重启与原回执不变，并保持 Docker Compose 下的上传流程。"
        )
        issues = self.service._bugfix_prompt_issues(prompt)
        self.assertTrue(any("内部双次清洁 Docker" in issue for issue in issues), issues)
        public_prompt = prompt.replace("两次清洁 Docker 环境中的实际结果一致：", "实际结果是")
        self.assertFalse(any("内部双次清洁 Docker" in issue
                             for issue in self.service._bugfix_prompt_issues(public_prompt)))

    def test_manual_bug_refill_can_repair_failed_artifact_only_in_copy(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='artifact_failed',updated_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-failed-source", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,error,created_at,updated_at)
               VALUES('check-failed-source',?,'A',?,'observed_failed','verify failed',?,?)""",
            (pair["id"], "a" * 40, stamp, stamp),
        )
        self.db.set_setting("manual_bug_only_mode", True)
        self.db.set_setting("manual_bug_auto_refill_enabled", True)

        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append((operation, fn, args)) or True,
        ):
            self.assertTrue(self.service._schedule_task_source("bugfix"))
        self.assertEqual(submitted[0][0], "bug-source-repair-" + pair["id"])
        self.assertIs(submitted[0][1].__func__, self.service.repair_bug_source.__func__)

    def test_isolated_bug_baseline_has_one_new_root_commit(self):
        source = self.root / "source-product"
        source.mkdir()
        run_command(["git", "init", "-b", "main"], cwd=source)
        run_command(["git", "config", "user.name", "test"], cwd=source)
        run_command(["git", "config", "user.email", "test@example.com"], cwd=source)
        (source / "app.py").write_text("print('source')\n", encoding="utf-8")
        run_command(["git", "add", "-A"], cwd=source)
        run_command(["git", "commit", "-m", "source"], cwd=source)
        source_sha = run_command(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()

        baseline, baseline_sha = self.service._create_isolated_bug_baseline(
            {"source_sha": source_sha}, {"workspace_path": str(source)}, "task-isolated",
        )

        self.assertNotEqual(baseline.resolve(), source.resolve())
        self.assertNotEqual(baseline_sha, source_sha)
        self.assertEqual(
            run_command(["git", "rev-list", "--count", "HEAD"], cwd=baseline).stdout.strip(),
            "1",
        )
        self.assertEqual((baseline / "app.py").read_text(encoding="utf-8"), "print('source')\n")
        self.assertEqual(run_command(["git", "status", "--porcelain"], cwd=source).stdout.strip(), "")

    def test_generated_task_scope_budget_rejects_cluttered_prompt(self):
        prompt = "甲" * 150 + "；" + "乙" * 150 + "；丙；丁。"
        issues = generated_task_prompt_issues(
            "zero_to_one", prompt, ["a", "b", "c"], {
                "engineeringCore": "跨层状态裁决",
                "mainUserFlow": "导入后核验并处理冲突",
                "implementationModules": ["parser", "service", "api"],
                "runtimeComponents": ["api"],
                "auxiliaryMechanisms": [],
                "newOperations": ["导入", "核验"],
                "newStateSets": ["处理状态"],
            },
        )
        self.assertTrue(any("完整句子" in issue for issue in issues))
        self.assertTrue(any("单句最多" in issue for issue in issues))
        self.assertTrue(any("分号最多" in issue for issue in issues))

    def test_generated_task_scope_budget_accepts_old_system_shape(self):
        prompt = ("项目从空仓库起步，用户提交输入后应得到可核对的处理结果与错误反馈。"
                  "仓库交付 Dockerfile 和 Docker Compose，含健康检查和可配置宿主机端口，"
                  "并提供名为 verify、执行完成后自行退出并以退出码报告结果的一次性验收服务。"
                  + "".join(("甲" * 68 + "。") for _ in range(3)))
        issues = generated_task_prompt_issues(
            "zero_to_one", prompt, ["a", "b", "c"], {
                "engineeringCore": "跨层状态裁决",
                "mainUserFlow": "导入后核验并处理冲突",
                "implementationModules": ["parser", "service", "api"],
                "runtimeComponents": ["api"],
                "auxiliaryMechanisms": ["幂等导入"],
                "newOperations": ["导入", "核验"],
                "newStateSets": ["处理状态"],
            },
        )
        self.assertEqual(issues, [])

    def test_generated_tasks_reject_generic_docker_verify_closing(self):
        business = "".join(("甲" * 68 + "。") for _ in range(4))
        delivery = (
            "从空仓库交付 Dockerfile 与 Docker Compose，提供健康检查、可配置端口，"
            "名为 verify 的一次性服务自行退出并用退出码报告结果。"
        )
        for task_type in ("zero_to_one", "feature"):
            issues = generated_task_prompt_issues(task_type, business + delivery)
            self.assertTrue(any("通用 Docker/verify" in issue for issue in issues))
            issues = generated_task_prompt_issues(task_type, delivery + business)
            self.assertFalse(any("通用 Docker/verify" in issue for issue in issues))

    def test_generated_task_punctuation_near_miss_is_repaired_without_changing_words(self):
        prompt = (
            "项目从空仓库起步，用 Dockerfile 和 Docker Compose 启动，并提供名为 verify、"
            "执行完成后自行退出并用退出码报告结果的一次性验收服务。"
            + "甲" * 65 + "，" + "乙" * 65 + "。"
            + "丙" * 65 + "。" + "丁" * 65 + "。"
        )
        self.assertTrue(any("单句最多" in issue for issue in
                            generated_task_prompt_issues("zero_to_one", prompt)))
        repaired = repair_generated_task_punctuation("zero_to_one", prompt)
        self.assertNotEqual(repaired, prompt)
        self.assertEqual(generated_task_prompt_issues("zero_to_one", repaired), [])
        self.assertEqual(
            re.sub(r"[。！？!?，,；;：:]", "", repaired),
            re.sub(r"[。！？!?，,；;：:]", "", prompt),
        )

    def test_generated_task_punctuation_repair_does_not_bypass_nonformat_failure(self):
        prompt = "Playwright" + "甲" * 65 + "，" + "乙" * 65 + "。" + ("丙" * 65 + "。") * 3
        self.assertEqual(repair_generated_task_punctuation("zero_to_one", prompt), prompt)

    def test_generated_task_scope_rejects_estimate_over_180_minutes(self):
        base = {
            "engineeringCore": "跨层状态裁决",
            "mainUserFlow": "导入后核验并处理冲突",
            "implementationModules": ["parser", "service", "api"],
            "runtimeComponents": ["api"],
            "auxiliaryMechanisms": [],
            "newOperations": ["导入", "核验"],
            "newStateSets": ["处理状态"],
            "estimatedMinutesMin": 70,
            "estimatedMinutesMax": 120,
        }
        prompt = ("项目从空仓库起步，用户提交输入后应得到可核对的处理结果与错误反馈。"
                  "仓库交付 Dockerfile 和 Docker Compose，含健康检查和可配置宿主机端口，"
                  "并提供名为 verify、执行完成后自行退出并以退出码报告结果的一次性验收服务。"
                  + "".join(("甲" * 68 + "。") for _ in range(3)))
        self.assertEqual(
            generated_task_prompt_issues("zero_to_one", prompt, ["a", "b", "c"], base),
            [],
        )
        within_limit = dict(base, estimatedMinutesMax=180)
        self.assertFalse(any("完整预计工时必须合理" in issue for issue in
                             generated_task_prompt_issues(
                                 "zero_to_one", prompt, ["a", "b", "c"], within_limit)))
        over_target = dict(base, estimatedMinutesMax=181)
        self.assertTrue(any("10–180 分钟" in issue for issue in
                            generated_task_prompt_issues(
                                "zero_to_one", prompt, ["a", "b", "c"], over_target)))
        invalid = dict(base, estimatedMinutesMax=481)
        self.assertTrue(any("10–180 分钟" in issue for issue in
                            generated_task_prompt_issues(
                                "zero_to_one", prompt, ["a", "b", "c"], invalid)))
        reversed_range = dict(base, estimatedMinutesMin=100, estimatedMinutesMax=90)
        self.assertTrue(any(
            "完整预计工时必须合理" in issue
            for issue in generated_task_prompt_issues(
                "zero_to_one", prompt, ["a", "b", "c"], reversed_range,
            )
        ))

    def test_generated_task_validation_rejects_blind_review_overrun(self):
        prompt = "项目从空仓库起步，用 Dockerfile 和 Docker Compose 启动，并提供名为 verify、执行完成后自行退出并用退出码报告结果的一次性验收服务。" + "".join(("甲" * 60 + "。") for _ in range(4))
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,project_category,
               acceptance_json,difficulty,difficulty_evidence_json,estimated_minutes_min,
               estimated_minutes_max,fingerprint,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("task-estimate", "generated", "zero_to_one", "独立估时测试题", prompt,
             "纯后端", '["场景一","场景二","场景三"]', "困难", '["生成方声称 105–120 分钟"]',
             105, 120, "estimate-fingerprint", "candidate", stamp, stamp),
        )
        review = {
            "accepted": True, "difficulty": "困难", "difficultyEvidence": ["跨模块状态"],
            "banned": False, "duplicate": False, "baselineReady": True, "reason": "可验收",
            "workItems": [
                {"name": "理解需求", "phase": "development", "minMinutes": 20, "maxMinutes": 30, "basis": "梳理输入与边界"},
                {"name": "核心实现", "phase": "development", "minMinutes": 90, "maxMinutes": 180, "basis": "完成状态与算法"},
                {"name": "本地回归", "phase": "development", "minMinutes": 35, "maxMinutes": 55, "basis": "业务模块回归"},
                {"name": "后续交付", "phase": "docker_delivery", "minMinutes": 20, "maxMinutes": 40, "basis": "Docker Compose 与 verify 清洁验收"},
            ],
        }
        with patch.object(self.service.codex, "run", return_value=review) as run:
            result = self.service.validate_task("task-estimate")
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn('"estimated_minutes_max"', run.call_args.args[1])
        self.assertNotIn('"difficulty": "困难"', run.call_args.args[1])
        self.assertNotIn("生成方声称", run.call_args.args[1])
        task = self.db.one("SELECT * FROM tasks WHERE id='task-estimate'")
        self.assertEqual((task["reviewed_minutes_min"], task["reviewed_minutes_max"]), (165, 305))
        self.assertIn("超过180分钟上限", task["estimate_risk"])
        self.assertIn("超过 180 分钟", task["rejection_reason"])

    def test_historical_zero_to_one_keeps_old_verify_wording(self):
        prompt = "项目从空仓库起步，用 Dockerfile 和 Docker Compose 启动，并提供名为 verify 的可执行验收服务。" + "".join(("甲" * 60 + "。") for _ in range(4))
        issues = generated_task_prompt_issues("zero_to_one", prompt, ["a", "b", "c"])
        self.assertFalse(any("自行退出" in issue or "退出码" in issue for issue in issues))

    def test_new_generated_zero_to_one_requires_one_shot_verify_exit_code(self):
        prompt = "项目从空仓库起步，用 Dockerfile 和 Docker Compose 启动，并提供名为 verify 的可执行验收服务。" + "".join(("甲" * 60 + "。") for _ in range(4))
        issues = generated_task_prompt_issues(
            "zero_to_one", prompt, ["a", "b", "c"], {
                "engineeringCore": "跨层状态裁决",
                "mainUserFlow": "导入后核验并处理冲突",
                "implementationModules": ["parser", "service", "api"],
                "runtimeComponents": ["api"],
                "auxiliaryMechanisms": [],
                "newOperations": ["导入", "核验"],
                "newStateSets": ["处理状态"],
            },
        )
        self.assertTrue(any("自行退出并用退出码" in issue for issue in issues))

        one_shot_without_exit_code = prompt.replace(
            "名为 verify 的可执行验收服务",
            "名为 verify、执行完成后自行退出的一次性验收服务",
        )
        issues = generated_task_prompt_issues(
            "zero_to_one", one_shot_without_exit_code, ["a", "b", "c"], {
                "engineeringCore": "跨层状态裁决",
                "mainUserFlow": "导入后核验并处理冲突",
                "implementationModules": ["parser", "service", "api"],
                "runtimeComponents": ["api"],
                "auxiliaryMechanisms": [],
                "newOperations": ["导入", "核验"],
                "newStateSets": ["处理状态"],
            },
        )
        self.assertTrue(any("自行退出并用退出码" in issue for issue in issues))

    def test_generated_zero_to_one_requires_named_verify_service(self):
        prompt = "".join(("甲" * 62 + "。") for _ in range(5))
        issues = generated_task_prompt_issues("zero_to_one", prompt, ["a", "b", "c"])
        self.assertTrue(any("verify" in issue for issue in issues))

    def test_new_task_does_not_reject_compose_managed_external_service(self):
        prompt = "".join(("甲" * 62 + "。") for _ in range(5))
        candidate = {
            "engineeringCore": "local state", "mainUserFlow": "resolve",
            "implementationModules": ["parser", "service", "api"],
            "runtimeComponents": ["api"], "auxiliaryMechanisms": [],
            "newOperations": ["resolve"], "newStateSets": ["state"],
            "stack": "Python, FastAPI",
        }
        issues = generated_task_prompt_issues(
            "zero_to_one", prompt, ["本地输入核验", "将结果写入 Redis", "冲突反馈"], candidate,
        )
        self.assertFalse(any("外部运行时服务" in issue for issue in issues))

    def test_generated_feature_does_not_repeat_existing_verify_requirement(self):
        prompt = "".join(("甲" * 62 + "。") for _ in range(5))
        issues = generated_task_prompt_issues("feature", prompt, ["a", "b", "c"])
        self.assertEqual(issues, [])

    def test_new_zero_to_one_and_feature_reject_repository_browser_verification(self):
        zero_to_one = (
            "项目从空仓库起步，用 Dockerfile 和 Docker Compose 启动，并提供健康检查和可配置端口。"
            "工程师在页面导入数据并查看精确裁决结果。"
            "核心算法处理重复输入与冲突状态，错误时清除旧结论。"
            "服务需要保留稳定错误码和可核对的结果摘要。"
            "Compose 提供名为 verify、执行完成后自行退出并用退出码报告结果的一次性服务，"
            "并运行 Playwright 浏览器验收。"
        )
        issues = generated_task_prompt_issues("zero_to_one", zero_to_one, ["a", "b", "c"])
        self.assertTrue(any("浏览器验收工具" in issue for issue in issues))

        feature = (
            "在现有页面增加冲突复核流程，并保留当前接口和存量数据。"
            "用户选择两份记录后可以查看字段差异并确认裁决。"
            "确认过程必须处理版本变化和重复提交。"
            "失败时保持原记录不变并给出稳定反馈。"
            "验收改为由 Cypress 执行端到端浏览器测试。"
        )
        issues = generated_task_prompt_issues("feature", feature, ["a", "b", "c"])
        self.assertTrue(any("浏览器验收工具" in issue for issue in issues))

    def test_bugfix_prompt_rejects_browser_verification(self):
        prompt = (
            "合法边界输入在现有服务中稳定返回错误结果，两次清洁 Docker Compose 复现一致。"
            "修复后必须保持现有接口、精确计数和错误语义，并用 API 请求核对边界与回归场景。"
            "自动化验收需要执行代码测试和 HTTP 冒烟，同时运行 Playwright 浏览器测试确认页面。"
            "不得通过缩小输入范围、近似计算或改变响应字段规避该问题，现有启动方式保持不变。"
        )
        issues = self.service._bugfix_prompt_issues(prompt)
        self.assertTrue(any("浏览器自动化" in issue for issue in issues))
        self.assertTrue(contains_browser_verification([
            {"composeArgs": ["run", "--rm", "verify", "npx", "playwright", "test"]},
        ]))
        self.assertFalse(contains_browser_verification([
            {"composeArgs": ["exec", "-T", "api", "pytest", "tests/test_api.py"]},
        ]))

        policy_issues = self.service._bugfix_prompt_issues(
            (
                "合法输入在现有 Docker Compose 服务中返回错误裁决，修复后执行代码测试和 HTTP 冒烟。"
                "不能用浏览器进行验收，且必须保留当前接口字段、错误语义和自动化验收链路。"
            ) * 3
        )
        self.assertTrue(any("浏览器自动化" in issue for issue in policy_issues))

        product_browser_issues = self.service._bugfix_prompt_issues(
            (
                "用户在浏览器中编辑数据后页面显示错误裁决，修复后执行代码测试和 HTTP 冒烟。"
                "必须保留当前接口字段、错误语义、Docker Compose 和自动化验收链路。"
            ) * 3
        )
        self.assertFalse(any("浏览器自动化" in issue for issue in product_browser_issues))

    def test_bugfix_public_title_comes_from_sanitized_prompt(self):
        internal = "因唯一表永久保留历史根节点而产生二次增长"
        prompt = "# 800 项合法 OR 链的节点数增长到 320400\n\n合法输入会稳定放大资源占用。"
        self.assertEqual(
            self.service._bugfix_public_title(prompt, internal),
            "800 项合法 OR 链的节点数增长到 320400",
        )
        self.assertEqual(
            self.service._bugfix_public_title("合法边界输入被错误拒绝。后续说明。", internal),
            "合法边界输入被错误拒绝",
        )

    def test_manual_bug_source_rejects_browser_automation_baseline(self):
        workspace = self.root / "browser-source"
        workspace.mkdir()
        (workspace / "package.json").write_text(
            '{"scripts":{"test:e2e":"playwright test"}}', encoding="utf-8",
        )
        (workspace / "package-lock.json").write_text(
            '{"name":"playwright-only-lock-entry"}', encoding="utf-8",
        )
        self.assertEqual(
            self.service._bug_source_browser_automation_files(workspace),
            ["package.json"],
        )

    def test_browser_source_scan_ignores_dependency_lock_noise(self):
        workspace = self.root / "non-browser-source"
        workspace.mkdir()
        (workspace / "package.json").write_text(
            '{"scripts":{"test":"vitest run"}}', encoding="utf-8",
        )
        (workspace / "package-lock.json").write_text(
            '{"dependencies":{"playwright-core":"transitive-only"}}', encoding="utf-8",
        )
        self.assertEqual(self.service._bug_source_browser_automation_files(workspace), [])

    def test_browser_source_scan_ignores_virtual_environment_metadata(self):
        workspace = self.root / "python-source"
        dependency = workspace / ".venv" / "lib" / "python3.13" / "site-packages" / "crypto.dist-info"
        dependency.mkdir(parents=True)
        (workspace / "app.py").write_text("print('service')\n", encoding="utf-8")
        (dependency / "sbom.json").write_text(
            '{"components":[{"name":"selenium","scope":"optional"}]}', encoding="utf-8",
        )

        self.assertEqual(self.service._bug_source_browser_automation_files(workspace), [])

    def test_browser_source_scan_ignores_npm_cache_metadata(self):
        workspace = self.root / "web-source"
        dependency = workspace / ".npm-cache" / "_logs"
        dependency.mkdir(parents=True)
        (workspace / "app.ts").write_text("export const service = true;\n", encoding="utf-8")
        (dependency / "install-debug.log").write_text(
            "installed playwright-core as a transitive dependency\n", encoding="utf-8",
        )

        self.assertEqual(self.service._bug_source_browser_automation_files(workspace), [])

    def test_dependency_only_browser_rejection_is_reopened_for_rescan(self):
        self.db.audit("bug.discovery_completed", "pair", "pair-third-party-noise", {
            "arm": "B", "searchSummary": "browser dependency detected",
            "candidateIds": [],
            "browserRejected": [
                ".venv/lib/python3.13/site-packages/crypto.dist-info/sbom.json",
                ".npm-cache/_logs/install-debug.log",
            ],
        })
        source = self.db.one(
            "SELECT id FROM audit_events WHERE entity_id='pair-third-party-noise'"
        )

        self.assertEqual(self.service._reopen_dependency_only_browser_scans(), 1)
        self.assertEqual(
            self.db.one("SELECT event_type FROM audit_events WHERE id=?", (source["id"],))["event_type"],
            "bug.discovery_false_positive",
        )
        reopened = self.db.one(
            """SELECT detail_json FROM audit_events
                 WHERE entity_id='pair-third-party-noise' AND event_type='bug.discovery_reopened'"""
        )
        self.assertEqual(json.loads(reopened["detail_json"])["arm"], "B")

    def test_verify_dependency_port_conflict_is_recovered_for_safe_retry(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE tasks SET status='rejected',rejection_reason='preflight failed' WHERE id='task-1'"
        )
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='baseline_preflight_failed',error='preflight failed' WHERE id=?",
            (pair["id"],),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,error,finished_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'failed','preflight failed',?,?,?)""",
                (pair["id"] + "-" + arm.lower(), pair["id"], arm, arm,
                 str(self.root / arm), "container-" + arm, "screen-" + arm,
                 "auto_model/urm", "image", stamp, stamp, stamp),
            )
        self.db.audit("task.baseline_preflight_failed", "pair", pair["id"], {
            "task_id": "task-1",
            "checks": [
                {"name": "compose_config", "passed": True},
                {"name": "clean_start", "passed": True},
                {"name": "containers_running", "passed": True},
                {"name": "verify_service", "passed": False,
                 "detail": "Container baseline-proxy-1 Starting",
                 "command": "docker compose --profile '*' run --rm verify"},
            ],
        })

        self.assertEqual(self.service._recover_verify_dependency_port_conflicts(), 1)
        task = self.db.one("SELECT status,rejection_reason,locked_by FROM tasks WHERE id='task-1'")
        self.assertEqual(task["status"], "used")
        self.assertEqual(task["rejection_reason"], "")
        self.assertEqual(task["locked_by"], pair["id"])
        restored = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(restored["status"], "repair_pending")
        self.assertEqual(restored["stage"], "baseline_preflight_retry_pending")
        self.assertIn("基线预检依赖重建端口冲突", restored["error"])
        self.assertEqual(
            {row["status"] for row in self.db.all("SELECT status FROM arm_runs WHERE pair_id=?", (pair["id"],))},
            {"queued"},
        )
        event = self.db.one(
            """SELECT event_type FROM audit_events WHERE entity_id=?
                 AND event_type LIKE 'task.baseline_preflight_%'
                 ORDER BY id LIMIT 1""",
            (pair["id"],),
        )
        self.assertEqual(event["event_type"], "task.baseline_preflight_false_positive")
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-port-retry", pair["id"], "owner", "repo", "public", str(self.root),
             "a" * 40, "a" * 40, "a" * 40, stamp, stamp),
        )
        with patch.object(self.service, "_submit_auto") as submit:
            self.assertTrue(self.service._resume_one_baseline_preflight_retry())
        resumed = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual((resumed["status"], resumed["stage"]), ("queued", "ready_to_start"))
        submit.assert_called_once_with("start-" + pair["id"], self.service.start_pair, pair["id"])

    def test_bug_only_mode_does_not_recover_non_bug_preflight_failure(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.set_setting("manual_bug_only_mode", True)
        self.db.set_setting("manual_priority_task_pause", {
            "active": True, "reservedTaskIds": [],
        })
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='baseline_preflight_failed' WHERE id=?",
            (pair["id"],),
        )
        self.db.audit("task.baseline_preflight_failed", "pair", pair["id"], {
            "task_id": "task-1",
            "checks": [
                {"name": "compose_config", "passed": True},
                {"name": "clean_start", "passed": True},
                {"name": "containers_running", "passed": True},
                {"name": "verify_service", "passed": False,
                 "command": "docker compose run --rm verify"},
            ],
        })

        self.assertEqual(self.service._recover_verify_dependency_port_conflicts(), 0)
        self.assertEqual(
            self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))["stage"],
            "baseline_preflight_failed",
        )

    def test_bug_only_mode_quarantines_unstarted_non_bug_recovery(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.set_setting("manual_bug_only_mode", True)
        self.db.execute(
            "UPDATE pairs SET status='queued',stage='ready_to_start' WHERE id=?",
            (pair["id"],),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                (pair["id"] + "-" + arm.lower(), pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", stamp, stamp),
            )
        self.db.audit("task.baseline_preflight_recovered", "pair", pair["id"], {
            "reason": "old verifier command",
        })

        self.assertEqual(self.service._quarantine_non_bug_preflight_recoveries(), 1)
        current = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["stage"], "paused_manual_bug_only")
        self.assertIn("Bug-only", current["error"])
        self.assertEqual(
            {row["status"] for row in self.db.all(
                "SELECT status FROM arm_runs WHERE pair_id=?", (pair["id"],)
            )},
            {"failed"},
        )

    def test_task_duplicate_guard_checks_full_local_history(self):
        stamp = now_iso()
        original = "实现带断点恢复、分片摘要和幂等确认的大型扫描上传，并用 Docker Compose 验收异常恢复。"
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,'困难','[]',?,'used',?,?)""",
            ("task-history", "test", "zero_to_one", "历史扫描上传", original, "history-key", stamp, stamp),
        )
        reason = self.service._deterministic_task_duplicate({"title": "换名后的扫描上传", "prompt": original})
        self.assertIn("完全重复", reason)

    def test_bug_line_contact_duplicate_across_arms_and_paths(self):
        existing = [{
            "title": "零面积三重接触线被拆成两个端点",
            "actual_result": "结果是两个 point 风险，整段接触线未进入平面图；tripleArea=0。",
            "expected_result": "应报告一个连续线段风险，而非两个端点。",
            "source_paths_json": '["src/certify.js:206"]',
        }]
        candidate = {
            "title": "第三条带使共享边出现三重覆盖，整段零面积线被漏报",
            "actual": "boundaryTriples=[]，pointTriples 只列出两个端点，tripleArea=0。",
            "expected": "应报告从起点到终点的 triple-boundary 风险。",
            "sourcePaths": ["src/geometry.mjs:197"],
        }
        reason = self.service._bug_source_candidate_duplicate(candidate, existing)
        self.assertIn("零面积三重覆盖线误报成端点", reason)
        independent = dict(candidate, actual="连通 L 形漏拍被拆成三个 gap 风险", expected="应合并为一个连通漏拍风险")
        self.assertEqual(self.service._bug_source_candidate_duplicate(independent, existing), "")

    def test_task_duplicate_guard_rejects_a9_bug_template_and_long_shared_fragment(self):
        retired = (
            "旧 Bug\n\n前置条件：两个终端同时编辑\n\n复现步骤：\n1. 保存\n\n"
            "实际结果：远端修改丢失\n\n预期结果：保留双方修改\n\n"
            "请修复该问题，保留现有 Docker Compose 启动与验收链路，并补充覆盖复现路径的自动化验收。"
        )
        reason = self.service._deterministic_task_duplicate({"title": "另一个 Bug", "prompt": retired})
        self.assertIn("A-9", reason)

        stamp = now_iso()
        shared = "这一段连续业务验收文字故意保持完全一致用于模拟低比例模板骨架重复并验证系统能够在整体相似度较低时提前拦截"
        left_context = "".join(chr(0x4E00 + index) for index in range(80))
        right_context = "".join(chr(0x5200 + index) for index in range(80))
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,'困难','[]',?,'used',?,?)""",
            ("task-fragment", "test", "zero_to_one", "历史任务", left_context + shared,
             "fragment-key", stamp, stamp),
        )
        reason = self.service._deterministic_task_duplicate({
            "title": "新任务", "prompt": right_context + shared,
        })
        self.assertIn("重复长骨架", reason)

    def test_task_duplicate_guard_rejects_reused_delivery_sentence(self):
        stamp = now_iso()
        delivery = (
            "项目以 Dockerfile 和 Docker Compose 启动，提供健康检查和可配置宿主机端口，"
            "Compose 中名为 verify 的一次性服务完成测试后自行退出并以退出码报告结果。"
        )
        first = "海洋地震测线的重叠段需按全局误差复核，不允许局部贪心。" + delivery
        second = "无菌灌装批次须经隔离器状态和审计记录联合裁决。" + delivery
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,'困难','[]',?,'used',?,?)""",
            ("task-delivery", "test", "zero_to_one", "海洋地震测线", first,
             "delivery-key", stamp, stamp),
        )
        self.assertIn("A-9", self.service._deterministic_task_duplicate({
            "title": "无菌灌装隔离器", "prompt": second,
        }))

    def test_bug_task_cannot_reissue_parent_failure_at_new_numeric_scale(self):
        source_pair = self.insert_ready_bugfix_task("困难")
        self.db.execute(
            "UPDATE tasks SET title=?,prompt=? WHERE id='task-bugfix'",
            ("小数载荷超限被当作可行", "小数载荷超过上限仍被放行，应拒绝超载挂装。"),
        )
        bug_pair = self.service.create_pair("task-bugfix")
        duplicate = self.service._deterministic_task_duplicate({
            "source": "bug_discovery", "task_type": "bugfix",
            "parent_pair_id": bug_pair["id"],
            "title": "超大十进制载荷误判为未超限",
            "prompt": "换用更大的十进制载荷后，超出上限的草稿仍被判为可行。",
        })
        self.assertIn("同一业务越界缺陷", duplicate)
        distinct = self.service._deterministic_task_duplicate({
            "source": "bug_discovery", "task_type": "bugfix",
            "parent_pair_id": bug_pair["id"],
            "title": "挂装记录重复保存后丢失",
            "prompt": "多次保存后旧记录消失，需要保留已确认记录。",
        })
        self.assertEqual(distinct, "")

    def test_delivery_preflight_does_not_recheck_prompt_overlap(self):
        self.insert_ready_task()
        repeated = (
            "项目以 Dockerfile 和 Docker Compose 启动，提供健康检查和可配置宿主机端口，"
            "Compose 中名为 verify 的一次性服务完成测试后自行退出并以退出码报告结果。"
        )
        self.db.execute(
            "UPDATE tasks SET title=?,prompt=? WHERE id='task-1'",
            ("旧题", "".join(chr(0x4E00 + index) for index in range(90)) + repeated),
        )
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,remote_id,created_at,updated_at)
               VALUES(?,?,'qc_passed','900',?,?)""",
            ("delivery-old", pair["id"], stamp, stamp),
        )
        next_detail = {
            "task": {
                "title": "另一道题",
                "prompt": "".join(chr(0x5200 + index) for index in range(90)) + repeated,
            },
        }
        with patch.object(self.service, "pair_detail", return_value=next_detail):
            check = self.service.delivery_preflight("pair-next")
        self.assertFalse(any("题面骨架" in issue or "题目内容与已提交" in issue
                             for issue in check["blockers"]))

    def test_task_duplicate_context_includes_previous_submission_prompts(self):
        connection = sqlite3.connect(self.config.old_db_path)
        connection.execute(
            """CREATE TABLE solo_qa_prompt_history(
               remote_submission_id TEXT,repo_name TEXT,prompt TEXT,task_type TEXT,remote_status TEXT,
               submitted_at TEXT,remote_updated_at TEXT,last_synced_at TEXT)"""
        )
        connection.execute(
            "INSERT INTO solo_qa_prompt_history VALUES('900','历史冷库项目',?,'0-1代码生成','QC_PASSED',?,?,?)",
            ("冷库断电后恢复告警序列并保持去重游标", now_iso(), now_iso(), now_iso()),
        )
        connection.commit()
        connection.close()
        context = self.service._task_generation_context()
        self.assertTrue(any(item["source"] == "历史提交题库" and item["title"] == "历史冷库项目" for item in context))
        prompt = "冷库断电后恢复告警序列并保持去重游标"
        self.assertIn("历史提交题库", self.service._deterministic_task_duplicate({"title": "新生成", "prompt": prompt}))
        self.assertEqual(
            self.service._deterministic_task_duplicate({"source": "legacy", "title": "允许复用", "prompt": prompt}),
            "",
        )

    def test_submission_claim_and_remote_binding_are_idempotent(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        first = self.service.update_solo_qa_state({"pair_id": pair["id"], "status": "submitting"})
        self.assertEqual(first["status"], "submitting")
        with self.assertRaisesRegex(ValueError, "重复上传"):
            self.service.update_solo_qa_state({"pair_id": pair["id"], "status": "submitting"})
        self.service.update_solo_qa_state({
            "pair_id": pair["id"], "status": "qc_pending", "remote_id": "474", "remote_status": "SUBMITTED",
        })
        with self.assertRaisesRegex(ValueError, "禁止改绑"):
            self.service.update_solo_qa_state({
                "pair_id": pair["id"], "status": "qc_pending", "remote_id": "475", "remote_status": "SUBMITTED",
            })
        synced = self.service.update_solo_qa_state({
            "pair_id": pair["id"], "status": "qc_passed", "remote_id": "474", "remote_status": "QC_PASSED",
        })
        self.assertEqual((synced["remote_id"], synced["status"]), ("474", "qc_passed"))

    def test_confirming_gsb_keeps_pending_fix_record_in_repair_flow(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-confirm-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", arm.lower() * 40, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed','[]',?,?)""",
                ("check-confirm-" + arm, pair["id"], arm, arm.lower() * 40, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'draft',?,?)""",
            ("gsb-confirm-repair", pair["id"], "Same", "", "", "", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,remote_id,remote_status,created_at,updated_at)
               VALUES(?,?,'needs_fix','470','PENDING_FIX',?,?)""",
            ("delivery-confirm-repair", pair["id"], stamp, stamp),
        )
        self.service.confirm_gsb(
            pair["id"], "Same",
            "A 在 app.py 完成状态恢复，Docker 验收跑通关键异常路径，最终行为符合题面。",
            "B 在 app.py 也完成状态恢复，Docker 验收覆盖相同业务流程，结果与 A 接近。",
            "刘昱",
        )
        delivery = self.db.one("SELECT status,remote_id FROM delivery_submissions WHERE pair_id=?", (pair["id"],))
        self.assertEqual(delivery, {"status": "needs_fix", "remote_id": "470"})

    def test_ready_task_selection_uses_only_zero_to_one_while_catching_up(self):
        stamps = {
            "feature": "2026-01-01T00:00:00+00:00",
            "bugfix": "2026-01-02T00:00:00+00:00",
            "zero_to_one": "2026-01-03T00:00:00+00:00",
        }
        for task_type in ("zero_to_one", "feature", "bugfix"):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
                   difficulty_evidence_json,fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'困难','[]',?,'ready',?,?)""",
                ("task-ready-" + task_type, "test", task_type, task_type, "hard " + task_type + " task",
                 "ready-" + task_type, stamps[task_type], stamps[task_type]),
            )
        self.assertEqual(self.service._next_ready_task()["task_type"], "zero_to_one")

    def test_ready_task_selection_prefers_new_zero_to_one_and_rejects_duplicate_title(self):
        rows = [
            ("task-used", "test", "共享标题", "已经开发过的复杂状态恢复任务", "used", "2019-01-01T00:00:00+00:00"),
            ("task-duplicate", "test", "共享标题", "完全不同的说明也不应复用标题", "ready", "2020-01-01T00:00:00+00:00"),
            ("task-legacy", "legacy", "旧题", "旧的复杂零到一任务", "ready", "2021-01-01T00:00:00+00:00"),
            ("task-new", "generated", "新题", "新的复杂零到一任务", "ready", "2022-01-01T00:00:00+00:00"),
        ]
        for task_id, source, title, prompt, status, created_at in rows:
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
                   difficulty_evidence_json,fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'困难','[]',?,?,?,?)""",
                (task_id, source, "zero_to_one", title, prompt, task_id, status, created_at, created_at),
            )
        selected = self.service._next_ready_task()
        self.assertEqual(selected["id"], "task-new")
        rejected = self.db.one("SELECT status,rejection_reason FROM tasks WHERE id='task-duplicate'")
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("标题", rejected["rejection_reason"])

    def test_missing_feature_and_bug_tasks_use_real_completed_pair_sources(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.mark_completed_feature_source(pair["id"], stamp)
        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append((operation, fn, args)) or True,
        ):
            self.assertTrue(self.service._schedule_task_source("feature"))
            self.assertTrue(self.service._schedule_task_source("bugfix"))
        self.assertEqual(submitted[0][0], "feature-" + pair["id"])
        self.assertIs(submitted[0][1].__func__, self.service.generate_followup_feature.__func__)
        self.assertEqual(submitted[1][0], "bugs-" + pair["id"])
        self.assertIs(submitted[1][1].__func__, self.service.discover_bugs.__func__)

    def test_bug_discovery_scans_each_passed_arm_instead_of_retiring_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.mark_completed_feature_source(pair["id"], stamp)
        commit_b = "b" * 40
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            (pair["id"] + "-source-b", pair["id"], "B", "B", str(self.root),
             pair["id"] + "-container-b", pair["id"] + "-screen-b",
             "auto_model/urm", "image", commit_b, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,created_at,updated_at)
               VALUES(?,?,'B',?,'passed','[]',?,?)""",
            (pair["id"] + "-source-check-b", pair["id"], commit_b, stamp, stamp),
        )
        self.db.audit("bug.discovery_completed", "pair", pair["id"], {
            "arm": "A", "searchSummary": "A scanned", "candidateIds": [],
        })

        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append((operation, fn, args)) or True,
        ):
            self.assertTrue(self.service._schedule_task_source("bugfix"))
        self.assertEqual(submitted[0][0], "bugs-" + pair["id"])

        with patch.object(self.service, "_bug_source_browser_automation_files", return_value=[]), \
             patch.object(self.service.codex, "run", return_value={
                 "searchSummary": "B scanned", "candidates": [],
             }):
            result = self.service.discover_bugs(pair["id"])
        self.assertEqual(result["arm"], "B")
        self.assertTrue(self.service._bug_source_arm_scanned(pair["id"], "A"))
        self.assertTrue(self.service._bug_source_arm_scanned(pair["id"], "B"))

    def test_bug_discovery_with_candidate_keeps_same_arm_available_for_more_bugs(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.mark_completed_feature_source(pair["id"], stamp)
        self.db.audit("bug.discovery_completed", "pair", pair["id"], {
            "arm": "A", "searchSummary": "first independent bug found",
            "candidateIds": ["bug-first"], "eligibleCandidateIds": ["bug-first"],
            "exhausted": False,
        })

        self.assertFalse(self.service._bug_source_arm_scanned(pair["id"], "A"))
        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append((operation, fn, args)) or True,
        ):
            self.assertTrue(self.service._schedule_task_source("bugfix"))
        self.assertEqual(submitted[0][0], "bugs-" + pair["id"])
        self.assertIs(submitted[0][1].__func__, self.service.discover_bugs.__func__)

    def test_bug_discovery_exhausts_arm_only_after_empty_followup_scan(self):
        pair_id = "pair-multi-bug"
        self.db.audit("bug.discovery_completed", "pair", pair_id, {
            "arm": "A", "candidateIds": ["bug-one"], "exhausted": False,
        })
        self.assertFalse(self.service._bug_source_arm_scanned(pair_id, "A"))

        self.db.audit("bug.discovery_completed", "pair", pair_id, {
            "arm": "A", "candidateIds": [], "duplicateRejected": [],
            "exhausted": True,
        })
        self.assertTrue(self.service._bug_source_arm_scanned(pair_id, "A"))

    def test_exhausted_bug_source_stays_exhausted_after_one_day(self):
        pair_id = "pair-old-scan"
        self.db.audit("bug.discovery_completed", "pair", pair_id, {
            "arm": "A",
            "candidateIds": [], "exhausted": True,
        })
        self.db.execute(
            "UPDATE audit_events SET created_at=datetime('now','-2 days') WHERE entity_id=?",
            (pair_id,),
        )
        self.assertTrue(self.service._bug_source_arm_exhausted(pair_id, "A"))

    def test_exhausted_original_commit_does_not_hide_repaired_source(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.mark_completed_feature_source(pair["id"])
        original_sha = self.db.one(
            "SELECT commit_sha FROM arm_runs WHERE pair_id=? AND arm='A'", (pair["id"],),
        )["commit_sha"]
        repaired_sha = "f" * 40
        self.db.audit("bug.discovery_completed", "pair", pair["id"], {
            "arm": "A", "sourceSha": original_sha,
            "candidateIds": [], "exhausted": True,
        })
        self.db.audit("bug.source_repair_completed", "pair", pair["id"], {
            "sourceArm": "A", "sourceSha": original_sha,
            "baselineSha": repaired_sha, "workspacePath": str(self.root),
            "validation": {"status": "passed"},
        })
        with patch.object(self.service, "_bug_source_browser_automation_files", return_value=[]), \
             patch.object(self.service.codex, "run", return_value={
                 "searchSummary": "repaired source exhausted", "candidates": [],
             }) as codex_run:
            result = self.service.discover_bugs(pair["id"])
        codex_run.assert_called_once()
        self.assertEqual(result["arm"], "A")
        self.assertTrue(result["exhausted"])
        self.assertEqual(
            self.db.one("SELECT json_extract(detail_json,'$.sourceSha') sha FROM audit_events "
                        "WHERE event_type='bug.discovery_completed' ORDER BY id DESC LIMIT 1")["sha"],
            repaired_sha,
        )
        self.assertTrue(self.service._bug_source_arm_exhausted(pair["id"], "A", original_sha))
        self.assertTrue(self.service._bug_source_arm_exhausted(pair["id"], "A", repaired_sha))

    def test_identical_exhausted_bug_commit_skips_second_analysis(self):
        self.insert_ready_task()
        first = self.service.create_pair("task-1")
        stamp = now_iso()
        self.mark_completed_feature_source(first["id"], stamp)
        source_sha = self.db.one(
            "SELECT commit_sha FROM arm_runs WHERE pair_id=? AND arm='A'", (first["id"],),
        )["commit_sha"]
        self.db.audit("bug.discovery_completed", "pair", first["id"], {
            "arm": "A", "sourceSha": source_sha,
            "candidateIds": [], "exhausted": True,
        })
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("task-same-commit", "test", "zero_to_one", "same commit", "same commit source",
             "困难", '["跨模块状态"]', "fingerprint-same-commit", "ready", stamp, stamp),
        )
        second = self.service.create_pair("task-same-commit")
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed' WHERE id=?", (second["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-same-commit", second["id"], "A", "A", str(self.root), "container-same",
             "screen-same", "auto_model/urm", "image", source_sha, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?, 'A',?,'passed',?,?)""",
            ("check-same-commit", second["id"], source_sha, stamp, stamp),
        )
        with patch.object(self.service.codex, "run") as codex_run:
            result = self.service.discover_bugs(second["id"])
        codex_run.assert_not_called()
        self.assertEqual(result["candidateIds"], [])
        self.assertTrue(result["exhausted"])

    def test_discarded_delivery_is_not_reused_as_feature_or_bug_source(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=?,updated_at=? WHERE id=?",
            (stamp, stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,created_at,updated_at)
               VALUES('delivery-discarded',?,'discarded',?,?)""",
            (pair["id"], stamp, stamp),
        )
        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ):
            self.assertTrue(self.service._schedule_task_source("feature"))
            self.assertTrue(self.service._schedule_task_source("bugfix"))
        self.assertEqual(submitted, ["generate-zero-to-one", "generate-zero-to-one"])

    def test_failed_winner_artifact_is_not_retried_as_feature_source(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.mark_completed_feature_source(pair["id"])
        self.db.execute(
            "UPDATE artifact_checks SET status='observed_failed' WHERE pair_id=? AND arm='A'",
            (pair["id"],),
        )
        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ):
            self.assertTrue(self.service._schedule_task_source("feature"))
        self.assertEqual(submitted, ["generate-zero-to-one"])

    def test_latest_zero_to_one_gets_at_most_three_feature_tasks_then_new_project(self):
        self.insert_ready_task()
        older = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=?,updated_at=? WHERE id=?",
            (stamp, stamp, older["id"]),
        )
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at)
               VALUES('task-latest-root','test','zero_to_one','latest root','hard project','困难','[]',
                      'latest-root','ready',?,?)""",
            (stamp, stamp),
        )
        latest = self.service.create_pair("task-latest-root")
        later = datetime.now(timezone.utc).isoformat()
        self.mark_completed_feature_source(latest["id"], later)
        statuses = ("used", "rejected", "used")
        for index, status in enumerate(statuses):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   parent_pair_id,fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'困难','[]',?,?,?,?,?)""",
                (f"feature-limit-{index}", "generated_followup", "feature", f"feature {index}",
                 f"hard feature {index}", latest["id"], f"feature-limit-{index}", status, later, later),
            )
            submitted = []
            with patch.object(
                self.service, "_submit_auto",
                side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
            ):
                self.assertTrue(self.service._schedule_task_source("feature"))
            expected = "feature-" + latest["id"] if index < 2 else "generate-zero-to-one"
            self.assertEqual(submitted, [expected])

        # The older project is still below its limit, but saturation of the
        # latest root deliberately starts a fresh 0–1 instead of mining old roots.
        self.assertFalse(self.db.one(
            "SELECT 1 FROM tasks WHERE parent_pair_id=? AND task_type='feature'",
            (older["id"],),
        ))
        with self.assertRaisesRegex(ValueError, "最多生成 3 个 Feature"):
            self.service.generate_followup_feature(latest["id"])

    def test_feature_limit_groups_legacy_rows_by_baseline_repository(self):
        for index in range(4):
            status = "used" if index < 3 else "candidate"
            suffix = ".git" if index == 3 else ""
            created_at = "2026-09-18T00:00:0%d+00:00" % index
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   baseline_path,baseline_repo_url,baseline_sha,fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'困难','[]',?,?,?,?,?,?,?)""",
                (f"legacy-feature-{index}", "legacy", "feature", "same legacy project",
                 f"hard feature {index}", str(self.root),
                 "https://github.com/example/same-project" + suffix, "a" * 40,
                 f"legacy-feature-{index}", status, created_at, created_at),
            )
        with patch.object(self.service.codex, "run") as run:
            result = self.service.validate_task("legacy-feature-3")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("最多保留 3 个 Feature", result["result"]["reason"])
        run.assert_not_called()

    def test_refill_validates_existing_candidates_before_generating_more(self):
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
               difficulty_evidence_json,fingerprint,status,created_at,updated_at)
               VALUES('task-old-candidate','test','zero_to_one','candidate','hard task',
                      '困难','[]','old-candidate','candidate',?,?)""",
            (stamp, stamp),
        )
        with patch.object(self.service, "_schedule_available_task_sources") as schedule, \
             patch.object(self.service, "validate_task_async") as validate:
            self.service._schedule_refill_once()
        validate.assert_called_once_with("task-old-candidate")
        schedule.assert_not_called()

    def test_refill_starts_all_available_sources_without_a_type_quota(self):
        scheduled = []
        with patch.object(
            self.service, "_schedule_task_source",
            side_effect=lambda kind: scheduled.append(kind) or True,
        ):
            self.assertTrue(self.service._schedule_available_task_sources())
        self.assertEqual(scheduled, ["bugfix", "feature", "zero_to_one"])

    def test_language_framework_field_keeps_only_technology_names(self):
        self.assertEqual(
            normalize_stack(
                "Python 3.13、FastAPI、Pydantic、SQLAlchemy/Alembic、持久化后台 worker；"
                "TypeScript、React、Vite；pytest、Vitest、Playwright；Docker、Docker Compose"
            ),
            "Python 3.13, FastAPI, TypeScript, React",
        )
        self.assertEqual(
            normalize_stack(
                "Python 3.13、FastAPI、Pydantic、pytest、Docker Compose；"
                "沿用现有算法，不引入外部服务。"
            ),
            "Python 3.13, FastAPI",
        )

    def test_language_framework_keeps_only_primary_languages_and_frameworks(self):
        self.assertEqual(
            normalize_language_framework(
                "Go 1.25, Gin, shopspring/decimal, testify, Docker, Docker Compose"
            ),
            "Go 1.25, Gin",
        )
        self.assertEqual(
            normalize_language_framework(
                "Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker"
            ),
            "Python 3.12, FastAPI, TypeScript, React",
        )
        self.assertEqual(
            normalize_language_framework("TypeScript, React, Vite, Vitest, Playwright, Docker Compose"),
            "TypeScript, React",
        )

    def test_original_prompts_are_staggered_between_a_and_b(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm, sent_at in (("A", stamp), ("B", None)):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,prompt_sent_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pair["id"] + "-" + arm.lower(), pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 "developing" if sent_at else "running", sent_at, stamp, stamp),
            )
        b_arm = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm='B'", (pair["id"],))
        with patch("pairwise_console.service.time.sleep") as sleep, \
             patch.object(self.service.claude, "send_prompt") as send_prompt:
            self.service._send_prompt_with_pair_stagger(pair["id"], b_arm, "same prompt")
        self.assertEqual(send_prompt.call_count, 1)
        waited = sleep.call_args.args[0]
        self.assertGreaterEqual(waited, 30)
        self.assertLessEqual(waited, 31)

    def test_baseline_preflight_rejects_feature_before_claude_launch(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        repo_root = self.root / "baseline-preflight"
        (repo_root / "A").mkdir(parents=True)
        self.db.execute("UPDATE tasks SET task_type='feature' WHERE id='task-1'")
        self.db.execute(
            "UPDATE pairs SET status='queued',stage='ready_to_start',baseline_sha=? WHERE id=?",
            ("b" * 40, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-preflight", pair["id"], "owner", "repo", "public", str(repo_root),
             "b" * 40, "b" * 40, "b" * 40, stamp, stamp),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                ("arm-preflight-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", stamp, stamp),
            )
        failed = {
            "status": "failed", "error": "缺少 Docker Compose 或 Dockerfile",
            "checks": [{"name": "compose_file", "passed": False, "detail": "未找到 Compose 文件"}],
        }
        with patch.object(self.service.artifacts, "preflight", return_value=failed), \
             patch.object(self.service.claude, "launch") as launch, \
             patch.object(self.service, "_submit") as submit:
            result = self.service.start_pair(pair["id"])
        launch.assert_not_called()
        submit.assert_called_once()
        self.assertEqual(result["stage"], "baseline_preflight_failed")
        self.assertEqual(self.db.one("SELECT status FROM tasks WHERE id='task-1'")["status"], "rejected")

    def test_prompt_is_canonicalized_before_native_send(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE tasks SET prompt=? WHERE id='task-1'",
            ("First paragraph.\r\n\r\nSecond paragraph.\n\n\nThird paragraph.\n",),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pair["id"] + "-a", pair["id"], "A", "A", str(self.root / "A"),
             "container-A", "screen-A", "auto_model/urm", "image", "running", stamp, stamp),
        )
        arm = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm='A'", (pair["id"],))
        with patch.object(self.service.claude, "send_prompt") as send_prompt:
            self.service._send_prompt_with_pair_stagger(pair["id"], arm, "stale fallback")
        expected = "First paragraph.\nSecond paragraph.\nThird paragraph."
        self.assertEqual(send_prompt.call_args.args[1], expected)
        task = self.db.one("SELECT prompt FROM tasks WHERE id='task-1'")
        self.assertEqual(task["prompt"], expected)
        audit = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id='task-1' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(audit["event_type"], "task.prompt_canonicalized_for_native_trace")

    def test_claude_prompt_canonicalization_matches_native_shape(self):
        self.assertEqual(
            self.service.claude.canonical_prompt("A\r\n\r\nB\n \nC\n\n\n"),
            "A\nB\nC",
        )

    def test_failed_ready_pair_cannot_be_revived_by_a_queued_start(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='ready_to_start' WHERE id=?",
            (pair["id"],),
        )
        with self.assertRaisesRegex(ValueError, "Pair 已停止"):
            self.service.start_pair(pair["id"])

    def test_duplicate_start_after_pair_enters_development_is_idempotent(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?",
            (pair["id"],),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,prompt_sent_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
                (pair["id"] + "-" + arm.lower(), pair["id"], arm, arm,
                 str(self.root / arm), "container-" + arm, "screen-" + arm,
                 "auto_model/urm", "image", stamp, stamp, stamp),
            )
        with patch.object(self.service.claude, "launch") as launch:
            result = self.service.start_pair(pair["id"])
        launch.assert_not_called()
        self.assertEqual(result["stage"], "development")
        self.assertEqual(len(result["arms"]), 2)

    def test_pair_starts_only_one_arm_when_one_terminal_slot_is_free(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        repo_root = self.root / "terminal-cap-repo"
        (repo_root / "A").mkdir(parents=True)
        (repo_root / "B").mkdir(parents=True)
        self.db.execute(
            "UPDATE pairs SET status='queued',stage='ready_to_start',baseline_sha=? WHERE id=?",
            ("b" * 40, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-terminal-cap", pair["id"], "owner", "repo", "public", str(repo_root),
             "b" * 40, "b" * 40, "b" * 40, stamp, stamp),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                ("arm-terminal-cap-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", stamp, stamp),
            )

        launched = []

        def launch(run):
            launched.append(run["arm"])
            self.db.execute(
                "UPDATE arm_runs SET status='running',image_id='image-id' WHERE id=?",
                (run["id"],),
            )

        with patch.object(self.service.claude, "prepare_arm"), \
             patch.object(self.service.claude, "reset_unsent_arm"), \
             patch.object(self.service.claude, "launch", side_effect=launch), \
             patch.object(self.service.claude, "wait_until_ready"), \
             patch.object(self.service.claude, "materialize_repository"), \
             patch.object(self.service, "_send_prompt_with_pair_stagger"), \
             patch.object(self.service, "_submit_monitor"), \
             patch.object(self.service, "_available_development_arm_slots", side_effect=[1, 0]):
            result = self.service.start_pair(pair["id"])

        self.assertEqual(launched, ["A"])
        self.assertEqual(result["stage"], "development")
        statuses = {item["arm"]: item["status"] for item in result["arms"]}
        self.assertEqual(statuses, {"A": "running", "B": "queued"})
        audit = self.db.one(
            "SELECT detail_json FROM audit_events WHERE event_type='claude.pair_arm_deferred_for_capacity'"
        )
        self.assertEqual(json.loads(audit["detail_json"])["arms"], ["B"])

    def test_deferred_bug_pair_reuses_successful_baseline_preflight(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute("UPDATE tasks SET task_type='bugfix' WHERE id='task-1'")
        stamp = now_iso()
        repo_root = self.root / "deferred-preflight-repo"
        (repo_root / "A").mkdir(parents=True)
        (repo_root / "B").mkdir(parents=True)
        self.db.execute(
            "UPDATE pairs SET status='queued',stage='ready_to_start',baseline_sha=? WHERE id=?",
            ("b" * 40, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-deferred-preflight", pair["id"], "owner", "repo", "public", str(repo_root),
             "b" * 40, "b" * 40, "b" * 40, stamp, stamp),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                ("arm-deferred-preflight-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", stamp, stamp),
            )
        passed = {
            "status": "passed", "error": "", "compose_file": str(repo_root / "A" / "docker-compose.yml"),
            "checks": [],
        }
        with patch.object(self.service.artifacts, "preflight", return_value=passed) as preflight, \
             patch.object(self.service.claude, "prepare_arm"), \
             patch.object(self.service.claude, "reset_unsent_arm"), \
             patch.object(self.service, "_launch_arm_if_capacity", return_value=False):
            first = self.service.start_pair(pair["id"])
            second = self.service.start_pair(pair["id"])

        self.assertEqual(first["stage"], "ready_to_start")
        self.assertEqual(second["stage"], "ready_to_start")
        self.assertEqual(preflight.call_count, 1)
        self.assertEqual(
            len(self.db.all(
                "SELECT id FROM audit_events WHERE event_type='task.baseline_preflight_passed' AND entity_id=?",
                (pair["id"],),
            )),
            1,
        )

    def test_scheduler_starts_deferred_arm_as_soon_as_a_terminal_slot_opens(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?",
            (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)""",
            ("arm-deferred-B", pair["id"], "B", "B", str(self.root / "deferred-B"),
             "container-B", "screen-B", "auto_model/urm", "image", stamp, stamp),
        )
        submitted = []
        with patch.object(self.service, "_available_development_arm_slots", return_value=1), \
             patch.object(
                 self.service, "_submit_monitor",
                 side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
             ):
            self.service._schedule_pending_arm_retries(pair["id"])
        self.assertEqual(submitted, ["retry-recover-arm-deferred-B"])

    def test_scheduler_prioritizes_half_completed_pair_before_fresh_pair(self):
        self.db.set_setting("max_pairs_parallel", 3)
        self.db.set_setting("max_claude_terminals", 3)
        self.insert_ready_task()
        partial = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?",
            (partial["id"],),
        )
        for arm, status, commit in (
            ("A", "completed", "a" * 40),
            ("B", "queued", ""),
        ):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-priority-" + arm, partial["id"], arm, arm,
                 str(self.root / ("priority-" + arm)), "container-priority-" + arm,
                 "screen-priority-" + arm, "auto_model/urm", "image", status,
                 commit, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("task-fresh", "test", "zero_to_one", "fresh hard project",
             "Build another hard project with Docker Compose", "困难", '["跨模块状态"]',
             "fingerprint-fresh", "ready", stamp, stamp),
        )
        fresh = self.service.create_pair("task-fresh")
        self.db.execute(
            "UPDATE pairs SET status='queued',stage='ready_to_start' WHERE id=?",
            (fresh["id"],),
        )
        scheduled = []
        with patch.object(self.service, "_schedule_pending_arm_retries",
                          side_effect=lambda pair_id: scheduled.append("pending-" + pair_id)), \
             patch.object(self.service, "_schedule_active_arm_monitors"), \
             patch.object(self.service, "_schedule_completed_arm_validations"), \
             patch.object(self.service, "_schedule_checkpoint_pushes"), \
             patch.object(
                 self.service, "_submit_auto",
                 side_effect=lambda operation, fn, *args: scheduled.append(operation) or True,
             ), patch.object(self.service, "_schedule_refill_once"):
            self.service._schedule_auto_pipeline_once()

        self.assertEqual(self.service._priority_partial_pair_ids(), {partial["id"]})
        self.assertIn("pending-" + partial["id"], scheduled)
        self.assertNotIn("start-" + fresh["id"], scheduled)

    def test_exhausted_half_pair_does_not_take_the_next_terminal(self):
        self.db.set_setting("development_max_attempts", 2)
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """UPDATE pairs SET status='running',stage='development',
               development_failure_count=2 WHERE id=?""",
            (pair["id"],),
        )
        for arm, status, commit in (
            ("A", "completed", "a" * 40),
            ("B", "waiting_retry", ""),
        ):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-exhausted-priority-" + arm, pair["id"], arm, arm,
                 str(self.root / ("exhausted-priority-" + arm)),
                 "container-exhausted-priority-" + arm,
                 "screen-exhausted-priority-" + arm, "auto_model/urm", "image",
                 status, commit, stamp, stamp),
            )

        submitted = []
        with patch.object(
            self.service, "_submit_monitor",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ), patch.object(self.service, "_finish_exhausted_pair_after_peer") as finish:
            self.service._schedule_pending_arm_retries(pair["id"])

        self.assertEqual(self.service._priority_partial_pair_ids(), set())
        self.assertEqual(submitted, [])
        finish.assert_called_once_with(pair["id"])

    def test_duplicate_terminal_reservation_does_not_reown_active_arm(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?)""",
            ("arm-already-owned", pair["id"], "A", "A", str(self.root / "already-owned"),
             "container-already-owned", "screen-already-owned", "auto_model/urm", "image",
             stamp, stamp),
        )
        self.assertIsNone(self.service._launch_arm_if_capacity({"id": "arm-already-owned"}))
        self.assertEqual(
            self.db.one("SELECT status FROM arm_runs WHERE id='arm-already-owned'")["status"],
            "developing",
        )
        self.assertIsNotNone(self.db.one(
            "SELECT id FROM audit_events "
            "WHERE event_type='claude.terminal_reservation_already_claimed' "
            "AND entity_id='arm-already-owned'",
        ))

    def test_terminal_reservation_rechecks_pair_failure_budget_inside_lock(self):
        self.db.set_setting("development_max_attempts", 2)
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET development_failure_count=2 WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'waiting_retry',?,?)""",
            ("arm-stale-budget-launch", pair["id"], "A", "A",
             str(self.root / "stale-budget-launch"), "container-stale-budget-launch",
             "screen-stale-budget-launch", "auto_model/urm", "image", stamp, stamp),
        )

        with patch.object(self.service.claude, "launch") as launch:
            result = self.service._launch_arm_if_capacity(
                {"id": "arm-stale-budget-launch", "pair_id": pair["id"]},
            )

        self.assertIsNone(result)
        launch.assert_not_called()
        self.assertEqual(
            self.db.one(
                "SELECT status FROM arm_runs WHERE id='arm-stale-budget-launch'"
            )["status"],
            "waiting_retry",
        )
        self.assertIsNotNone(self.db.one(
            "SELECT id FROM audit_events "
            "WHERE event_type='claude.terminal_reservation_blocked_by_failure_budget' "
            "AND entity_id='arm-stale-budget-launch'",
        ))

    def test_repository_failure_releases_the_pair_slot(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        with patch.object(
            self.service.git, "create_pair_repository", side_effect=RuntimeError("push rejected"),
        ):
            with self.assertRaisesRegex(RuntimeError, "push rejected"):
                self.service.prepare_pair_repository(pair["id"])
        failed = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["stage"], "repository_failed")
        self.assertIn("push rejected", failed["error"])

    def test_cancel_pair_stops_only_the_selected_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO pairs(id,task_id,chain_id,status,stage,created_at,updated_at)
               VALUES('pair-unrelated','task-1',?,'running','development',?,?)""",
            (pair["chain_id"], stamp, stamp),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?)""",
                ("arm-cancel-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-cancel-" + arm, "screen-cancel-" + arm,
                 "auto_model/urm", "image", stamp, stamp),
            )

        def archive(arm, reason, **_kwargs):
            self.db.execute(
                "UPDATE arm_runs SET status='failed',error=?,updated_at=? WHERE id=?",
                (reason, stamp, arm["id"]),
            )
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],))

        with patch.object(self.service.claude, "archive_failed_attempt", side_effect=archive) as stop:
            result = self.service.cancel_pair(pair["id"], "改用新版短题")
        self.assertEqual(result, {"pairId": pair["id"], "status": "cancelled", "stoppedArms": ["A", "B"]})
        self.assertEqual(stop.call_count, 2)
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "cancelled", "stage": "cancelled"},
        )
        self.assertEqual(
            self.db.one("SELECT status FROM pairs WHERE id='pair-unrelated'")["status"],
            "running",
        )

    def test_completed_arm_is_scheduled_for_validation_before_peer_finishes(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        for arm, status, sha in (("A", "completed", "a" * 40), ("B", "developing", "")):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,trace_path,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-early-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", status,
                 str(self.root / "traces" / arm), sha, stamp, stamp),
            )
        with patch.object(self.service, "_inspect_trace", return_value=(Path("A.jsonl"), "2.1.269", [])), \
             patch.object(self.service, "_submit_auto", return_value=True) as submit:
            self.service._refresh_pair_after_arm(pair["id"])
        submit.assert_called_once_with(
            "artifact-%s-A-%s" % (pair["id"], "a" * 12),
            self.service._validate_completed_arm, pair["id"], "A",
        )
        self.assertEqual(
            self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))["stage"],
            "development",
        )

    def test_pair_records_only_after_both_current_commits_pass(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?",
            (pair["id"],),
        )
        stamp = now_iso()
        for arm, status, sha in (("A", "completed", "a" * 40), ("B", "developing", "")):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-gate-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", status,
                 sha, stamp, stamp),
            )

        def passed_check(pair_id, arm, workspace, commit_sha):
            check_id = "check-gate-" + arm
            self.db.execute(
                """INSERT OR REPLACE INTO artifact_checks
                   (id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                (check_id, pair_id, arm, commit_sha, stamp, stamp),
            )
            return self.db.one("SELECT * FROM artifact_checks WHERE id=?", (check_id,))

        with patch.object(self.service.artifacts, "validate", side_effect=passed_check):
            self.service._validate_pair_artifacts(pair["id"], ["A"])
        self.assertEqual(
            self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))["stage"],
            "development",
        )
        self.db.execute(
            "UPDATE arm_runs SET status='completed',commit_sha=? WHERE pair_id=? AND arm='B'",
            ("b" * 40, pair["id"]),
        )
        with patch.object(self.service.artifacts, "validate", side_effect=passed_check), \
             patch.object(self.service, "_submit_auto", return_value=True):
            self.service._validate_pair_artifacts(pair["id"], ["B"])
        self.assertEqual(
            self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))["stage"],
            "difficulty_review",
        )

    def _prepare_pair_for_difficulty_review(self, task_id="task-1"):
        if task_id == "task-1":
            self.insert_ready_task()
        pair = self.service.create_pair(task_id)
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='difficulty_review' WHERE id=?",
            (pair["id"],),
        )
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,result,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?)""",
                ("arm-difficulty-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", sha,
                 "implemented and verified", stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?,?)""",
                ("check-difficulty-" + arm, pair["id"], arm, sha,
                 '[{"name":"verify_service","passed":true,"detail":"business scenarios passed"}]',
                 stamp, stamp),
            )
        return pair

    def test_actual_difficulty_review_passes_and_moves_to_recording(self):
        pair = self._prepare_pair_for_difficulty_review()
        result = {
            "aDifficulty": "困难", "bDifficulty": "地狱", "difficulty": "困难",
            "reason": "两侧都实现了跨模块状态恢复、并发一致性和异常链路，真实验收覆盖了关键边界。",
            "evidence": ["state.py 的事务恢复", "Docker verify_service 覆盖并发冲突"],
        }
        with patch.object(self.service.codex, "run", return_value=result):
            review = self.service.reassess_actual_difficulty(pair["id"])
        self.assertEqual(review["status"], "passed")
        self.assertEqual(review["assessed_difficulty"], "困难")
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "running", "stage": "recording"},
        )
        self.assertEqual(
            self.db.one("SELECT difficulty FROM tasks WHERE id='task-1'")["difficulty"],
            "困难",
        )

    def test_actual_difficulty_review_rejects_medium_and_discards_pair(self):
        pair = self._prepare_pair_for_difficulty_review()
        result = {
            "aDifficulty": "中等", "bDifficulty": "中等", "difficulty": "中等",
            "reason": "实际交付只沿现有结构增加局部数据流和输入校验，没有架构取舍或复杂状态链路。",
            "evidence": ["只改动局部处理函数", "Docker 验收仅覆盖常规输入校验"],
        }
        with patch.object(self.service.codex, "run", return_value=result):
            review = self.service.reassess_actual_difficulty(pair["id"])
        self.assertEqual(review["status"], "rejected")
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "failed", "stage": "difficulty_rejected"},
        )
        delivery = self.db.one("SELECT status,error FROM delivery_submissions WHERE pair_id=?", (pair["id"],))
        self.assertEqual(delivery["status"], "discarded")
        self.assertIn("低于困难/地狱", delivery["error"])

    def test_actual_difficulty_review_rejects_medium_bugfix_without_promotion(self):
        pair = self._prepare_pair_for_difficulty_review()
        self.db.execute("UPDATE tasks SET task_type='bugfix' WHERE id='task-1'")
        result = {
            "aDifficulty": "中等", "bDifficulty": "中等", "difficulty": "中等",
            "reason": "真实缺陷需要理解跨模块数据流并修正边界处理，但不涉及架构重设计。",
            "evidence": ["两次清洁环境已复现", "Docker 验收覆盖原始缺陷路径"],
        }
        with patch.object(self.service.codex, "run", return_value=result):
            review = self.service.reassess_actual_difficulty(pair["id"])
        self.assertEqual(review["status"], "rejected")
        self.assertEqual(review["assessed_difficulty"], "中等")
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "failed", "stage": "difficulty_rejected"},
        )
        self.assertEqual(self.db.one("SELECT difficulty FROM tasks WHERE id='task-1'")["difficulty"], "困难")
        self.assertEqual(
            self.db.one("SELECT status FROM delivery_submissions WHERE pair_id=?", (pair["id"],))["status"],
            "discarded",
        )

    def test_evidence_filter_finds_any_manual_rerecord_attempt(self):
        pair = self._prepare_pair_for_difficulty_review()
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO recording_attempts(
                 id,pair_id,arm,commit_sha,path,interaction_mode,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'manual','passed',?,?)""",
            ("manual-rerecord", pair["id"], "A", "a" * 40, str(self.root / "manual.mp4"), stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO recording_attempts(
                 id,pair_id,arm,commit_sha,path,interaction_mode,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'manual','recording',?,?)""",
            ("manual-active", pair["id"], "B", "b" * 40, str(self.root / "active.mp4"), stamp, stamp),
        )
        handler = Handler.__new__(Handler)
        handler.server = MagicMock(db=self.db)
        manual = handler._evidence_page({"manual_rerecorded": ["yes"], "page": ["1"], "size": ["20"]})
        automatic = handler._evidence_page({"manual_rerecorded": ["no"], "page": ["1"], "size": ["20"]})
        self.assertEqual([(row["arm"], row["manual_rerecorded"]) for row in manual["items"]], [("A", 1)])
        self.assertEqual([(row["arm"], row["manual_rerecorded"]) for row in automatic["items"]], [("B", 0)])

    def test_active_recording_is_available_outside_the_current_evidence_page(self):
        pair = self._prepare_pair_for_difficulty_review()
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO recording_attempts(
                 id,pair_id,arm,commit_sha,path,interaction_mode,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'manual','recording',?,?)""",
            ("manual-active", pair["id"], "A", "a" * 40, str(self.root / "manual.mp4"), stamp, stamp),
        )
        handler = Handler.__new__(Handler)
        handler.server = MagicMock(db=self.db)

        active = handler._active_recording()

        self.assertEqual(active["id"], "manual-active")
        self.assertEqual(active["pair_id"], pair["id"])
        self.assertEqual(active["interaction_mode"], "manual")
        self.assertEqual(active["title"], "hard-project")

    def test_colloquial_api_route_starts_preview_operation(self):
        handler = Handler.__new__(Handler)
        service = MagicMock()
        service.colloquialize_gsb_async.return_value = "gsb-colloquial-test"
        handler.server = MagicMock(service=service)
        handler._path_query = MagicMock(return_value=("/api/pairs/pair-test/gsb/colloquialize", {}))
        source = {"verdict": "Same", "aReason": "A reason long enough for preview", "bReason": "B reason long enough for preview"}
        handler._body = MagicMock(return_value=source)
        handler._json = MagicMock()

        handler.do_POST()

        service.colloquialize_gsb_async.assert_called_once_with("pair-test", source)
        handler._json.assert_called_once_with(202, {"operationId": "gsb-colloquial-test"})

    def test_batch_colloquial_api_starts_jobs_with_stale_safe_sources(self):
        handler = Handler.__new__(Handler)
        service = MagicMock()
        source = {"verdict": "Same", "aReason": "A merge.py has enough evidence", "bReason": "B rebase.ts has enough evidence"}
        service.gsb_colloquial_source.side_effect = [source, source]
        service.colloquialize_gsb_async.side_effect = ["op-one", "op-two"]
        handler.server = MagicMock(service=service)
        handler._path_query = MagicMock(return_value=("/api/gsb-reviews/colloquialize", {}))
        handler._body = MagicMock(return_value={"pairIds": ["pair-one", "pair-two"]})
        handler._json = MagicMock()

        handler.do_POST()

        self.assertEqual(service.colloquialize_gsb_async.call_count, 2)
        handler._json.assert_called_once_with(202, {"count": 2, "jobs": [
            {"pairId": "pair-one", "operationId": "op-one", "source": source},
            {"pairId": "pair-two", "operationId": "op-two", "source": source},
        ]})

    def test_batch_colloquial_apply_api_returns_service_summary(self):
        handler = Handler.__new__(Handler)
        service = MagicMock()
        items = [{"pairId": "pair-one", "source": {}, "preview": {}}]
        summary = {"applied": 1, "skipped": 0, "failed": 0, "results": []}
        service.apply_gsb_colloquial_batch.return_value = summary
        handler.server = MagicMock(service=service)
        handler._path_query = MagicMock(return_value=("/api/gsb-reviews/colloquialize/apply", {}))
        handler._body = MagicMock(return_value={"items": items})
        handler._json = MagicMock()

        handler.do_POST()

        service.apply_gsb_colloquial_batch.assert_called_once_with(items)
        handler._json.assert_called_once_with(200, summary)

    def test_full_monitor_capacity_does_not_starve_user_operations(self):
        release = threading.Event()
        started = threading.Event()
        start_lock = threading.Lock()
        start_count = 0

        def monitor():
            nonlocal start_count
            with start_lock:
                start_count += 1
                if start_count == 8:
                    started.set()
            release.wait(5)

        try:
            for index in range(8):
                self.service._submit_monitor("test-monitor-%d" % index, monitor)
            self.assertTrue(started.wait(2), "monitor pool did not reach full capacity")
            completed = threading.Event()
            self.service._submit("test-user-operation", completed.set)
            self.assertTrue(completed.wait(2), "user operation was starved by arm monitors")
        finally:
            release.set()

    def test_duplicate_arm_monitor_returns_without_entering_second_loop(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def exclusive(pair_id, arm_id, prompt):
            calls.append((pair_id, arm_id, prompt))
            entered.set()
            release.wait(2)
            return {"status": "done"}

        with patch.object(self.service, "_monitor_arm_exclusive", side_effect=exclusive):
            first_result = []
            worker = threading.Thread(target=lambda: first_result.append(
                self.service._monitor_arm("pair-lock", "arm-lock-A", "prompt")
            ))
            worker.start()
            self.assertTrue(entered.wait(1))
            duplicate = self.service._monitor_arm("pair-lock", "arm-lock-A", "prompt")
            release.set()
            worker.join(2)

        self.assertEqual(len(calls), 1)
        self.assertEqual(duplicate, {})
        self.assertEqual(first_result, [{"status": "done"}])

    def test_trace_snapshot_refresh_is_serialized_per_arm(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-trace-lock", "container_name": "container-trace-lock"}
        active = 0
        maximum_active = 0
        entered = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        results = []

        def fake_copy(command, **_kwargs):
            nonlocal active, maximum_active
            snapshot = Path(command[-1])
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
                first = not entered.is_set()
                entered.set()
            if first:
                release.wait(2)
            (snapshot / "session.jsonl").write_text("\n".join((
                json.dumps({"type": "user", "promptId": "prompt-1", "message": {"content": prompt}}),
                json.dumps({"type": "assistant", "message": {
                    "stop_reason": "end_turn", "content": [{"type": "text", "text": "Done"}],
                }}),
                json.dumps({"type": "system", "subtype": "turn_duration"}),
            )), encoding="utf-8")
            with guard:
                active -= 1
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            first = threading.Thread(target=lambda: results.append(
                self.service.claude.trace_state(arm, prompt)
            ))
            second = threading.Thread(target=lambda: results.append(
                self.service.claude.trace_state(arm, prompt)
            ))
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(2)
            second.join(2)

        self.assertEqual(maximum_active, 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item["complete"] for item in results))

    def test_trace_monitor_io_error_does_not_count_as_development_failure(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
            ("arm-monitor-io-A", pair["id"], "A", "A", str(self.root / "workspace-monitor-io"),
             "container-monitor-io", "screen-monitor-io", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )

        def finish_after_retry(_seconds):
            self.db.execute(
                "UPDATE arm_runs SET status='failed',updated_at=? WHERE id='arm-monitor-io-A'",
                (now_iso(),),
            )

        with patch.object(self.service.claude, "trace_state", side_effect=FileNotFoundError(".trace-snapshot")), \
             patch.object(self.service.claude, "runtime_alive", return_value=True), \
             patch.object(self.service.claude, "business_progress", return_value={
                 "has_code": False, "last_modified": 0.0, "paths": [],
             }), \
             patch("pairwise_console.service.time.sleep", side_effect=finish_after_retry):
            self.service._monitor_arm(pair["id"], "arm-monitor-io-A", "Build a hard project with Docker Compose")

        current = self.db.one("SELECT development_failure_count FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current["development_failure_count"], 0)
        failures = self.db.one(
            "SELECT COUNT(*) count FROM audit_events WHERE event_type='claude.attempt_failed' AND entity_id=?",
            ("arm-monitor-io-A",),
        )
        self.assertEqual(failures["count"], 0)
        warning = self.db.one(
            "SELECT detail_json FROM audit_events WHERE event_type='claude.trace_monitor_warning' AND entity_id=?",
            ("arm-monitor-io-A",),
        )
        self.assertIsNotNone(warning)

    def test_monitor_recovery_does_not_reactivate_a_replaced_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='running',stage='replaced',error='already replaced' WHERE id=?",
            (pair["id"],),
        )
        self.service._resume_active_monitors()
        current = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "failed", "stage": "replaced"})

    def test_user_paths_and_github_credential_helper_are_portable(self):
        self.assertEqual(OLD_APP_DIR, Path.home() / "Library/Application Support/Claude Eval Console")
        with patch("pairwise_console.gitops.shutil.which", return_value="/usr/local/bin/gh"), \
                patch("pairwise_console.gitops.run_command") as command:
            GitOps._github_git(["ls-remote", "origin"])
        args = command.call_args.args[0]
        self.assertIn("credential.helper=!/usr/local/bin/gh auth git-credential", args)
        self.assertEqual(command.call_args.kwargs["env"]["GIT_CONFIG_GLOBAL"], "/dev/null")

    def test_pair_requires_ready_hard_task_and_creates_chain(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.assertEqual(pair["status"], "queued")
        self.assertEqual(pair["stage"], "repository")
        chain = self.db.one("SELECT * FROM project_chains WHERE id=?", (pair["chain_id"],))
        self.assertEqual(chain["followup_required"], 1)
        self.assertEqual(self.db.one("SELECT status FROM tasks WHERE id='task-1'")["status"], "used")

    def test_pair_parallelism_has_a_hard_ceiling_of_five(self):
        self.db.set_setting("max_pairs_parallel", 9)
        stamp = now_iso()
        for index in range(6):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-limit-{index}", "test", "zero_to_one", f"hard-{index}", "Build a hard project",
                 "困难", '["跨模块状态"]', f"fingerprint-limit-{index}", "ready", stamp, stamp),
            )
        for index in range(5):
            self.service.create_pair(f"task-limit-{index}")
        with self.assertRaisesRegex(ValueError, "最多 5 个 Pair"):
            self.service.create_pair("task-limit-5")

    def test_concurrent_pair_creation_cannot_exceed_five(self):
        self.db.set_setting("max_pairs_parallel", 5)
        stamp = now_iso()
        for index in range(6):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-race-{index}", "test", "zero_to_one", f"hard-race-{index}", "Build a hard project",
                 "困难", '["并发状态"]', f"fingerprint-race-{index}", "ready", stamp, stamp),
            )
        barrier = threading.Barrier(6)
        outcomes = []
        outcome_lock = threading.Lock()

        def create(index):
            barrier.wait()
            try:
                self.service.create_pair(f"task-race-{index}")
                outcome = "created"
            except ValueError as exc:
                outcome = str(exc)
            with outcome_lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=create, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(outcomes.count("created"), 5)
        self.assertEqual(self.db.one("SELECT COUNT(*) count FROM pairs")["count"], 5)
        self.assertTrue(any("最多 5 个 Pair" in outcome for outcome in outcomes))

    def test_operator_restart_archives_active_arm_without_charging_pair_failure(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        arm_id = pair["id"] + "-a"
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',development_failure_count=1 WHERE id=?",
            (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,status,prompt_sent_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
            (arm_id, pair["id"], "A", "A", str(self.root / "workspace"),
             "container-a", "screen-a", "auto_model/urm", "image", stamp, stamp, stamp),
        )
        with patch.object(self.service, "_restart_arm_from_baseline",
                          return_value={"id": arm_id, "status": "developing"}) as restart, \
             patch.object(self.service, "_submit_monitor", return_value=True) as submit:
            self.service.restart_active_arm(pair["id"], "A")
        self.assertEqual(restart.call_args.kwargs["count_development_failure"], False)
        self.assertEqual(restart.call_args.kwargs["count_error_retry"], False)
        self.assertEqual(self.db.one(
            "SELECT development_failure_count FROM pairs WHERE id=?", (pair["id"],),
        )["development_failure_count"], 1)
        self.assertEqual(self.db.one(
            "SELECT status FROM arm_runs WHERE id=?", (arm_id,),
        )["status"], "manual_preparing")
        self.assertTrue(submit.called)

    def test_concurrent_repository_preparation_is_serialized_per_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        local_root = self.root / "repository-lock"
        local_root.mkdir()
        active = 0
        maximum_active = 0
        calls_lock = threading.Lock()

        def prepare_repo(*_args):
            nonlocal active, maximum_active
            with calls_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.08)
            with calls_lock:
                active -= 1
            return {"local_root": str(local_root), "status": "ready"}

        with patch.object(self.service.git, "create_pair_repository", side_effect=prepare_repo), \
             patch.object(self.service.claude, "prepare_arm"):
            threads = [threading.Thread(
                target=self.service.prepare_pair_repository, args=(pair["id"],),
            ) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(maximum_active, 1)

    def test_one_click_automation_is_persistent_and_preserves_pair_target(self):
        self.db.set_setting("max_pairs_parallel", 1)
        with patch.object(self.service, "_schedule_auto_pipeline_once") as schedule:
            status = self.service.set_auto_pipeline(True)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["targetPairs"], 1)
        self.assertEqual(self.db.setting("max_pairs_parallel"), 1)
        schedule.assert_called_once_with()
        stopped = self.service.set_auto_pipeline(False)
        self.assertFalse(stopped["enabled"])

    def test_recording_revalidation_ignores_development_pair_capacity(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.set_setting("max_pairs_parallel", 1)
        self.db.execute(
            """INSERT INTO pairs(id,task_id,chain_id,status,stage,created_at,updated_at)
               VALUES('pair-active-development','task-1',?,'running','development',?,?)""",
            (pair["chain_id"], stamp, stamp),
        )
        self.db.execute(
            """UPDATE pairs SET status='repair_pending',stage='recording_revalidation_pending',
               winner='B better',error='stale evidence',completed_at=?,updated_at=? WHERE id=?""",
            (stamp, stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-revalidation-A", pair["id"], "A", "A", str(self.root / "revalidation-A"),
             "container-revalidation-A", "screen-revalidation-A", "auto_model/urm", "image",
             "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            ("check-revalidation-A", pair["id"], "A", "a" * 40, "passed", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,error,created_at,updated_at)
               VALUES(?,?,?, ?,?,?)""",
            ("delivery-revalidation", pair["id"], "needs_review", "old evidence", stamp, stamp),
        )

        self.assertTrue(self.service._resume_one_recording_revalidation_pair())

        current = self.db.one("SELECT status,stage,winner,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "running", "stage": "recording", "winner": "", "error": ""})
        self.assertEqual(
            self.db.one("SELECT status FROM artifact_checks WHERE id='check-revalidation-A'")["status"],
            "passed",
        )
        self.assertEqual(
            self.db.one("SELECT status,error FROM delivery_submissions WHERE pair_id=?", (pair["id"],)),
            {"status": "needs_review", "error": ""},
        )

    def test_manual_full_pair_retry_resets_both_arms_only_after_operator_queue(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        local_root = self.root / "manual-full-retry"
        local_root.mkdir()
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,main_sha,
               a_sha,b_sha,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("repo-manual-full-retry", pair["id"], "owner", "repo", "public", str(local_root),
             "b" * 40, "b" * 40, "b" * 40, "ready", stamp, stamp),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,prompt_sent_at,session_id,trace_path,error,
                   attempt_no,api_retry_count,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'failed',?,?,?,?,2,1,?,?)""",
                ("arm-manual-full-" + arm, pair["id"], arm, arm,
                 str(self.root / ("old-" + arm)), "container-manual-full-" + arm,
                 "screen-manual-full-" + arm, "auto_model/urm", "image", stamp,
                 "old-session", "/old/trace.jsonl", "504", stamp, stamp),
            )
        self.db.execute(
            """UPDATE pairs SET status='repair_pending',stage='manual_full_retry_pending',
               development_failure_count=2,error='504',updated_at=? WHERE id=?""",
            (stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            ("delivery-manual-full", pair["id"], "discarded", "failed", stamp, stamp),
        )

        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ):
            self.assertTrue(self.service._resume_one_manual_full_pair_retry())

        current = self.db.one(
            "SELECT status,stage,development_failure_count,error FROM pairs WHERE id=?",
            (pair["id"],),
        )
        self.assertEqual(current, {
            "status": "queued", "stage": "ready_to_start",
            "development_failure_count": 0, "error": "",
        })
        arms = self.db.all(
            """SELECT arm,status,prompt_sent_at,session_id,trace_path,attempt_no,api_retry_count
                 FROM arm_runs WHERE pair_id=? ORDER BY arm""",
            (pair["id"],),
        )
        self.assertEqual([arm["status"] for arm in arms], ["queued", "queued"])
        self.assertTrue(all(not arm["prompt_sent_at"] and not arm["session_id"] and not arm["trace_path"] for arm in arms))
        self.assertTrue(all(arm["attempt_no"] == 1 and arm["api_retry_count"] == 0 for arm in arms))
        self.assertEqual(submitted, ["start-" + pair["id"]])

    def test_prompt_mismatch_retry_resets_both_arms_from_common_baseline(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        local_root = self.root / "prompt-mismatch-retry"
        local_root.mkdir()
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,main_sha,
               a_sha,b_sha,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("repo-prompt-mismatch", pair["id"], "owner", "repo", "public", str(local_root),
             "b" * 40, "a" * 40, "c" * 40, "ready", stamp, stamp),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,session_id,prompt_id,trace_path,commit_sha,
                   attempt_no,error_retry_count,api_retry_count,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,3,2,4,?,?)""",
                ("arm-prompt-mismatch-" + arm, pair["id"], arm, arm,
                 str(self.root / ("old-mismatch-" + arm)), "container-mismatch-" + arm,
                 "screen-mismatch-" + arm, "auto_model/urm", "image", "session-" + arm,
                 "prompt-" + arm, "/old/trace.jsonl", arm.lower() * 40, stamp, stamp),
            )
        self.db.execute(
            """UPDATE pairs SET status='repair_pending',stage='prompt_mismatch_retry_pending',
               development_failure_count=2,error='prompt mismatch',updated_at=? WHERE id=?""",
            (stamp, pair["id"]),
        )

        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ):
            self.assertTrue(self.service._resume_one_manual_full_pair_retry())

        current = self.db.one(
            "SELECT status,stage,development_failure_count,error FROM pairs WHERE id=?",
            (pair["id"],),
        )
        self.assertEqual(current, {
            "status": "queued", "stage": "ready_to_start",
            "development_failure_count": 0, "error": "",
        })
        arms = self.db.all(
            """SELECT arm,status,workspace_path,session_id,prompt_id,trace_path,commit_sha,
                      attempt_no,error_retry_count,api_retry_count
                 FROM arm_runs WHERE pair_id=? ORDER BY arm""",
            (pair["id"],),
        )
        self.assertEqual([arm["status"] for arm in arms], ["queued", "queued"])
        self.assertEqual(
            [arm["workspace_path"] for arm in arms],
            [str(local_root / "workspaces" / "A"), str(local_root / "workspaces" / "B")],
        )
        self.assertTrue(all(
            not arm["session_id"] and not arm["prompt_id"] and not arm["trace_path"]
            and not arm["commit_sha"] for arm in arms
        ))
        self.assertTrue(all(
            arm["attempt_no"] == 1 and arm["error_retry_count"] == 0
            and arm["api_retry_count"] == 0 for arm in arms
        ))
        self.assertEqual(submitted, ["start-" + pair["id"]])
        self.assertIsNotNone(self.db.one(
            """SELECT id FROM audit_events
                 WHERE event_type='claude.prompt_mismatch_retry_resumed' AND entity_id=?""",
            (pair["id"],),
        ))

    def test_automation_consumes_existing_ready_tasks_before_refill(self):
        stamp = now_iso()
        for index in range(4):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-auto-{index}", "test", "zero_to_one", f"hard-auto-{index}",
                 f"Build a distinct hard project number {index} with Docker Compose", "困难", '["跨模块状态"]',
                 f"fingerprint-auto-{index}", "ready", stamp, stamp),
            )
        submitted = []
        with patch.object(self.service, "_submit_auto", side_effect=lambda operation, fn, *args: submitted.append(operation) or True), \
             patch.object(self.service, "_schedule_refill_once") as refill:
            status = self.service._schedule_auto_pipeline_once()
        self.assertEqual(status["activePairs"], 3)
        self.assertEqual(status["readyTasks"], 1)
        self.assertEqual(len(self.db.all("SELECT id FROM pairs")), 3)
        self.assertEqual(len([item for item in submitted if item.startswith("repo-pair-")]), 3)
        refill.assert_not_called()

    def test_automation_scheduler_respects_configured_pair_target(self):
        self.db.set_setting("max_pairs_parallel", 3)
        stamp = now_iso()
        for index in range(4):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-target-{index}", "test", "zero_to_one", f"hard-target-{index}",
                 f"Build a distinct hard target project number {index} with Docker Compose",
                 "困难", '["跨模块状态"]', f"fingerprint-target-{index}", "ready", stamp, stamp),
            )
        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ), patch.object(self.service, "_schedule_refill_once") as refill:
            status = self.service._schedule_auto_pipeline_once()
        self.assertEqual(status["targetPairs"], 3)
        self.assertEqual(status["activePairs"], 3)
        self.assertEqual(status["readyTasks"], 1)
        self.assertEqual(len(self.db.all("SELECT id FROM pairs")), 3)
        self.assertEqual(len([item for item in submitted if item.startswith("repo-pair-")]), 3)
        refill.assert_not_called()

    def test_scheduler_skips_ready_task_claimed_by_replacement(self):
        self.insert_ready_task()
        task = self.db.one("SELECT * FROM tasks WHERE id='task-1'")

        def claimed_elsewhere(task_id):
            self.db.execute("UPDATE tasks SET status='used' WHERE id=?", (task_id,))
            raise ValueError("任务已被另一条调度路径占用")

        with patch.object(self.service, "_next_ready_task", side_effect=[task, None]), \
             patch.object(self.service, "create_pair", side_effect=claimed_elsewhere), \
             patch.object(self.service, "_submit_auto") as submitted, \
             patch.object(self.service, "_schedule_refill_once"):
            self.service._schedule_auto_pipeline_once()
        submitted.assert_not_called()

    def test_automation_refills_when_a_completed_pair_releases_a_slot(self):
        stamp = now_iso()
        for index in range(5):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-cycle-{index}", "test", "zero_to_one", f"hard-cycle-{index}",
                 f"Build a distinct lifecycle project number {index} with Docker Compose", "困难", '["跨模块状态"]',
                 f"fingerprint-cycle-{index}", "ready", stamp, stamp),
            )
        with patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_refill_once"):
            self.service._schedule_auto_pipeline_once()
        first = self.db.one("SELECT id FROM pairs ORDER BY created_at,id LIMIT 1")
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=?,updated_at=? WHERE id=?",
            (stamp, stamp, first["id"]),
        )
        with patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_refill_once"):
            status = self.service._schedule_auto_pipeline_once()
        self.assertEqual(status["activePairs"], 3)
        self.assertEqual(len(self.db.all("SELECT id FROM pairs")), 4)

    def test_recording_and_gsb_release_pair_start_capacity(self):
        self.db.set_setting("max_pairs_parallel", 4)
        stamp = now_iso()
        for index in range(6):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-postprocess-{index}", "test", "zero_to_one", f"hard-postprocess-{index}",
                 f"Build a distinct hard project {index} with Docker Compose", "困难", '["跨模块状态"]',
                 f"fingerprint-postprocess-{index}", "ready", stamp, stamp),
            )
        with patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_refill_once"):
            first_status = self.service._schedule_auto_pipeline_once()
            self.assertEqual(first_status["activePairs"], 4)
            first = self.db.one("SELECT id FROM pairs ORDER BY created_at,id LIMIT 1")
            self.db.execute(
                "UPDATE pairs SET status='running',stage='recording' WHERE id=?", (first["id"],),
            )
            recording_status = self.service._schedule_auto_pipeline_once()
            self.assertEqual(recording_status["activePairs"], 4)
            self.assertEqual(recording_status["postprocessingPairs"], 1)
            self.assertEqual(self.db.one("SELECT COUNT(*) count FROM pairs")["count"], 5)
            second = self.db.one(
                "SELECT id FROM pairs WHERE id<>? ORDER BY created_at,id LIMIT 1", (first["id"],),
            )
            self.db.execute(
                "UPDATE pairs SET status='review',stage='gsb_confirmation' WHERE id=?", (second["id"],),
            )
            gsb_status = self.service._schedule_auto_pipeline_once()
            self.assertEqual(gsb_status["activePairs"], 4)
            self.assertEqual(gsb_status["postprocessingPairs"], 2)
            self.assertEqual(self.db.one("SELECT COUNT(*) count FROM pairs")["count"], 6)

    def test_recording_and_gsb_backlog_does_not_block_new_pair(self):
        self.db.set_setting("max_pairs_parallel", 4)
        stamp = now_iso()
        for index in range(9):
            task_id = f"task-independent-postprocess-{index}"
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, "test", "zero_to_one", f"hard-independent-{index}",
                 f"Build a distinct hard project {index} with Docker Compose", "困难", '["跨模块状态"]',
                 f"fingerprint-independent-{index}", "ready", stamp, stamp),
            )
            if index < 8:
                pair = self.service.create_pair(task_id)
                self.db.execute(
                    "UPDATE pairs SET status='running',stage=? WHERE id=?",
                    ("recording" if index < 4 else "gsb_ready", pair["id"]),
                )
        with patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_next_automatic_recording"), \
             patch.object(self.service, "_schedule_refill_once"):
            status = self.service._schedule_auto_pipeline_once()
        self.assertEqual(status["postprocessingPairs"], 8)
        self.assertEqual(status["activePairs"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) count FROM pairs")["count"], 9)

    def test_gsb_waits_for_both_artifact_checks_and_required_recordings(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='gsb_ready' WHERE id=?", (pair["id"],),
        )
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-gsb-gate-" + arm, pair["id"], arm, arm, str(self.root),
                 "container-gsb-gate-" + arm, "screen-gsb-gate-" + arm,
                 "auto_model/urm", "image", sha, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?, 'A',?,'passed',?,?)""",
            ("check-gsb-gate-A", pair["id"], "a" * 40, stamp, stamp),
        )
        submitted = []
        with patch.object(self.service, "_submit_auto", side_effect=lambda key, *_: submitted.append(key) or True), \
             patch.object(self.service, "_schedule_next_automatic_recording"), \
             patch.object(self.service, "_schedule_refill_once"):
            self.service._schedule_auto_pipeline_once()
            self.assertIn("artifact-" + pair["id"] + "-B-" + "b" * 12, submitted)
            self.assertNotIn("gsb-" + pair["id"], submitted)
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?, 'B',?,'passed',?,?)""",
                ("check-gsb-gate-B", pair["id"], "b" * 40, stamp, stamp),
            )
            submitted.clear()
            self.service._schedule_auto_pipeline_once()
            self.assertNotIn("gsb-" + pair["id"], submitted)
            self.assertEqual(
                self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))["stage"],
                "recording",
            )
            for arm in ("A", "B"):
                self.db.execute(
                    """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,commit_match,status,
                       created_at,updated_at) VALUES(?,?,?,?,?,1,'passed',?,?)""",
                    ("rec-gsb-gate-" + arm, pair["id"], arm,
                     str(self.root / (arm + ".mp4")), arm.lower() * 40, stamp, stamp),
                )
            self.db.execute("UPDATE pairs SET stage='gsb_ready' WHERE id=?", (pair["id"],))
            submitted.clear()
            self.service._schedule_auto_pipeline_once()
        self.assertIn("gsb-" + pair["id"], submitted)

    def test_scheduler_resumes_prepared_pair_after_docker_daemon_recovers(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """UPDATE pairs SET status='failed',stage='ready_to_start',
               error='Cannot connect to the Docker daemon. Is the docker daemon running?',updated_at=?
               WHERE id=?""",
            (stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,remote_url,
               local_root,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-environment-resume", pair["id"], "owner", "repo", "public", "",
             str(self.root / "repo"), stamp, stamp),
        )
        submitted = []
        docker = MagicMock(returncode=0)
        with patch("pairwise_console.service.run_command", return_value=docker), \
             patch.object(
                 self.service, "_submit_auto",
                 side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
             ):
            self.assertTrue(self.service._resume_one_environment_failed_pair())
        self.assertEqual(submitted, ["start-" + pair["id"]])
        resumed = self.db.one("SELECT status,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(resumed, {"status": "queued", "error": ""})

    def test_gsb_confirmation_strips_backticks_and_completes_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO gsb_reviews(id,pair_id,verdict,reason,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("gsb-1", pair["id"], "Same", "两边都完成了相同功能，但各有一些可以复核的实现差异。", "draft", stamp, stamp),
        )
        self.db.execute(
            "UPDATE pairs SET stage='recording',error='旧录像失败信息' WHERE id=?",
            (pair["id"],),
        )
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-confirm-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-confirm-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,width,height,duration_seconds,status,created_at,updated_at)
                   VALUES(?,?,?,?,1280,720,30,'passed',?,?)""",
                ("rec-" + arm, pair["id"], arm, str(self.root / (arm + ".mov")), stamp, stamp),
            )
        result = self.service.confirm_gsb(
            pair["id"], "A better",
            "A 在 app/main.py 的 create 方法完成主要流程和异常路径，pytest 与接口冒烟结果稳定。",
            "B 在 app/main.py 的 create 方法完成主要流程，但 pytest 显示异常路径仍有可复现失败。",
            "刘昱",
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stage"], "completed")
        self.assertEqual(result["error"], "")
        self.assertEqual(result["gsb"]["confirmed_by"], "刘昱")
        self.assertEqual(result["gsb"]["draft_verdict"], "Same")
        self.assertEqual(result["gsb"]["final_verdict"], "A better")
        self.assertEqual(result["delivery"]["status"], "ready_to_submit")
        with self.assertRaisesRegex(ValueError, "不得引用录像"):
            self.service.confirm_gsb(
                pair["id"], "A better",
                "A 在 app/main.py 完成主要流程，录像显示结果稳定。",
                "B 在 app/main.py 的异常路径仍有可复现失败。", "刘昱",
            )

    def test_generated_gsb_self_corrects_missing_locators_and_is_default_confirmed(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute("UPDATE pairs SET status='running',stage='gsb_ready' WHERE id=?", (pair["id"],))
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-gsb-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""", ("check-gsb-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'passed',?,?)""", ("rec-gsb-" + arm, pair["id"], arm,
                    str(self.root / (arm + ".mp4")), sha, stamp, stamp),
            )
        result_payload = {
            "verdict": "A better",
            "aReason": "A 在 app/main.py 的 create 方法完成全部主要流程，pytest 覆盖异常路径与持久化结果，因此更倾向 A。",
            "bReason": "B 在 app/main.py 的 create 方法完成核心流程，但 pytest 显示异常恢复仍有可复现偏差，因此不优先。",
            "evidence": ["A Docker 通过", "B 异常路径失败"],
        }
        first_payload = dict(result_payload)
        first_payload.update({
            "aReason": "A 完成了全部主要流程，异常路径与持久化结果都有可见验收证据，因此更倾向 A。",
            "bReason": "B 完成了核心流程，但异常恢复仍有可复现偏差，因此相比 A 不优先。",
        })
        with patch.object(self.service.codex, "run", side_effect=[first_payload, result_payload]) as mocked_run:
            review = self.service.generate_gsb(pair["id"])
        self.assertEqual(mocked_run.call_count, 2)
        self.assertEqual(review["a_reason"], result_payload["aReason"])
        self.assertEqual(review["b_reason"], result_payload["bReason"])
        self.assertNotIn("preference_reason", review)
        self.assertNotIn("偏好依据：", review["reason"])
        self.assertEqual(review["status"], "confirmed")
        self.assertEqual(review["confirmed_by"], "刘昱（按授权默认确认）")
        completed = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(completed, {"status": "completed", "stage": "completed"})

    def test_gsb_accepts_unstartable_delivery_without_failure_recording(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute("UPDATE pairs SET status='running',stage='gsb_ready' WHERE id=?", (pair["id"],))
        for arm, check_status, error in (
            ("A", "observed_failed", "Docker Compose 清洁启动失败"),
            ("B", "passed", ""),
        ):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-failed-gsb-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("check-failed-gsb-" + arm, pair["id"], arm, sha, check_status,
                 '[{"name":"clean_start","passed":false,"detail":"dependency path missing"}]' if error else "[]",
                 error, stamp, stamp),
            )
            if check_status == "passed":
                self.db.execute(
                    """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,commit_match,review_status,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,1,'confirmed','passed',?,?)""",
                    ("rec-failed-gsb-" + arm, pair["id"], arm, str(self.root / (arm + ".mp4")),
                     sha, stamp, stamp),
                )
        payload = {
            "verdict": "B better",
            "aReason": "A 在 compose.yaml 的启动流程因依赖路径缺失而失败，清洁 Docker 无法启动，原始交付不可用。",
            "bReason": "B 在 compose.yaml 完成相同功能并通过清洁 Docker 验收，因此相较无法启动的 A 更可靠。",
            "evidence": ["A clean_start failed", "B Docker passed"],
        }
        with patch.object(self.service.codex, "run", return_value=payload) as run:
            review = self.service.generate_gsb(pair["id"])
        self.assertEqual(review["status"], "confirmed")
        self.assertIn("observed_failed", run.call_args.args[1])
        self.assertNotIn('"recording":', run.call_args.args[1])
        final = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(final, {"status": "completed", "stage": "completed"})
        delivery = self.db.one("SELECT status FROM delivery_submissions WHERE pair_id=?", (pair["id"],))
        self.assertEqual(delivery["status"], "ready_to_submit")

    def test_recording_scheduler_skips_observed_failed_artifact(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='recording',updated_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        for arm, check_status in (("A", "observed_failed"), ("B", "passed")):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-record-select-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                ("check-record-select-" + arm, pair["id"], arm, sha, check_status, stamp, stamp),
            )

        with patch.object(self.service, "start_recording") as start:
            self.service._schedule_next_automatic_recording([
                self.db.one("SELECT * FROM pairs WHERE id=?", (pair["id"],)),
            ])

        start.assert_called_once_with(pair["id"], "B", manual=False)

    def test_bug_fix_business_failure_blocks_recording_even_after_docker_passes(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        sha = "a" * 40
        self.db.execute(
            "UPDATE tasks SET task_type='bugfix',repair_verification_json=? WHERE id='task-1'",
            ('[{"scenario":"original"}]',),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-bug-record-a", pair["id"], "A", "A", str(self.root), "container-a", "screen-a",
             "auto_model/urm", "image", sha, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,'passed',?,?)""",
            ("check-bug-record-a", pair["id"], "A", sha, stamp, stamp),
        )
        arm = self.db.one("SELECT * FROM arm_runs WHERE id='arm-bug-record-a'")
        with patch("pairwise_console.service.clean_commands", return_value={
            "passed": False, "commands": [{"businessFailed": True}],
        }):
            self.assertEqual(self.service._verify_fixed_bug(pair["id"], arm), "observed_failed")
        check = self.db.one("SELECT status FROM artifact_checks WHERE id='check-bug-record-a'")
        self.assertEqual(check["status"], "observed_failed")
        with patch.object(self.service.recordings, "start", return_value={
            "capture_mode": "failure_evidence",
        }) as start:
            result = self.service.start_recording(pair["id"], "A")
            self.assertEqual(result["capture_mode"], "failure_evidence")
            start.assert_called_once_with(pair["id"], "A", 0, 0, manual=False, demo_override=None)

    def test_gsb_recheck_persists_split_review_suggestions(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed',?,?)""",
            ("gsb-recheck-source", pair["id"], "A better", "A：原 A 评价 B：原 B 评价",
             "原 A 评价有足够的具体事实与验收依据。", "原 B 评价说明了真实存在的交付差异。", stamp, stamp),
        )
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-recheck-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-recheck-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,sha256,duration_seconds,
                   commit_match,status,review_status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,30,1,'passed','confirmed',?,?)""",
                ("rec-recheck-" + arm, pair["id"], arm, str(self.root / (arm + ".mp4")),
                sha, arm * 64, stamp, stamp),
            )
        self.db.execute(
            "UPDATE recordings SET attempt_id='recording-b-evidence' WHERE pair_id=? AND arm='B'",
            (pair["id"],),
        )
        self.db.audit("recording.finished", "recording_attempt", "recording-b-evidence", {
            "status": "passed",
            "recorderEvents": [{"event": "interaction", "ok": True, "status": 200,
                                "path": "/api/v1/fold/ensemble"}],
        })
        self.assertEqual(
            self.service._gsb_evidence_bundle(pair["id"])["recordings"][1]["recorderEvents"][0]["status"],
            200,
        )
        payload = {
            "status": "suggested_revision",
            "suggestedVerdict": "A better",
            "suggestedAReason": "A 的 app/main.py 开发说明与 docker compose run verify 后续独立验收已明确区分。",
            "suggestedBReason": "B 的 app/main.py 接口偏差由 pytest 验证，评价写明了实际行为和客观后果。",
            "issues": ["原评价需要明确验收发生阶段。"],
            "evidenceRefs": ["checks[A]", "checks[B]"],
        }
        independent = {
            "verdict": "A better", "aReason": payload["suggestedAReason"],
            "bReason": payload["suggestedBReason"], "evidence": ["checks[A]", "checks[B]"],
        }
        with patch.object(self.service.codex, "run", side_effect=[independent, payload]) as mocked_run:
            result = self.service._recheck_gsb(pair["id"])
        self.assertEqual(mocked_run.call_count, 2)
        self.assertNotIn("原 A 评价", mocked_run.call_args_list[0].args[1])
        self.assertNotIn('"recordings":', mocked_run.call_args_list[0].args[1])
        self.assertIn("原 A 评价", mocked_run.call_args_list[1].args[1])
        self.assertEqual(result["result_status"], "suggested_revision")
        self.assertEqual(result["suggested_a_reason"], payload["suggestedAReason"])
        self.assertEqual(result["suggested_b_reason"], payload["suggestedBReason"])
        self.assertEqual(self.db.one("SELECT COUNT(*) count FROM gsb_rechecks")["count"], 1)
        before_rerecord = self.service.gsb_evidence_version(
            pair["id"], "A better",
            self.service._compose_gsb_reason(
                "原 A 评价有足够的具体事实与验收依据。", "原 B 评价说明了真实存在的交付差异。"
            ),
        )
        self.db.execute(
            """UPDATE recordings SET sha256='new-video-hash',duration_seconds=45,
               started_at='9999-01-01T00:00:00+00:00',finished_at='9999-01-01T00:00:45+00:00',
               updated_at='9999-01-01T00:00:45+00:00' WHERE pair_id=? AND arm='B'""",
            (pair["id"],),
        )
        self.db.audit("recording.finished", "pair", pair["id"], {"arm": "B", "status": "passed"})
        after_rerecord = self.service.gsb_evidence_version(
            pair["id"], "A better",
            self.service._compose_gsb_reason(
                "原 A 评价有足够的具体事实与验收依据。", "原 B 评价说明了真实存在的交付差异。"
            ),
        )
        self.assertEqual(before_rerecord, after_rerecord)
        batch = self.service.apply_latest_gsb_rechecks([pair["id"]])
        self.assertEqual(batch["applied"], 1)
        applied = self.db.one("SELECT * FROM gsb_rechecks WHERE id=?", (result["id"],))
        self.assertTrue(applied["applied_at"])
        review = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair["id"],))
        self.assertEqual(review["a_reason"], payload["suggestedAReason"])
        self.assertEqual(review["b_reason"], payload["suggestedBReason"])
        self.assertEqual(review["status"], "confirmed")

    def test_colloquial_preview_does_not_persist_until_human_confirmation(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        original_a = "A 在 merge.py 修好了保存问题，接口测试通过，不过浏览器流程没有执行。"
        original_b = "B 在 rebase.ts 完成了页面流程，Docker 验收通过，但新增测试是否运行无法确认。"
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed',?,?)""",
            ("gsb-colloquial-source", pair["id"], "Same",
             self.service._compose_gsb_reason(original_a, original_b),
             original_a, original_b, stamp, stamp),
        )
        rewritten = {
            "verdict": "Same",
            "aReason": "A 在 merge.py 把保存问题修好了，接口也实际测过，不过浏览器流程还没跑。",
            "bReason": "B 在 rebase.ts 把页面流程接好了，Docker 验收通过，不过还不能确认新增测试是否运行。",
        }
        with patch("pairwise_console.service.rewrite_preview", return_value=rewritten):
            operation = self.service.colloquialize_gsb_async(pair["id"], {
                "verdict": "Same", "aReason": original_a, "bReason": original_b,
            })
            for _ in range(50):
                result = self.service.operation(operation)
                if result["status"] != "running":
                    break
                time.sleep(0.01)
        self.assertEqual(result, {"id": operation, "status": "completed", "result": rewritten})
        stored = self.db.one("SELECT verdict,a_reason,b_reason FROM gsb_reviews WHERE pair_id=?", (pair["id"],))
        self.assertEqual(stored, {"verdict": "Same", "a_reason": original_a, "b_reason": original_b})

    def test_batch_colloquial_apply_updates_current_source_and_skips_stale_source(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        original_a = "A 在 merge.py 修好了保存问题，接口测试通过，不过浏览器流程没有执行。"
        original_b = "B 在 rebase.ts 完成了页面流程，Docker 验收通过，但新增测试是否运行无法确认。"
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,confirmed_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed','审核人',?,?)""",
            ("gsb-batch-colloquial", pair["id"], "Same",
             self.service._compose_gsb_reason(original_a, original_b),
             original_a, original_b, stamp, stamp),
        )
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-batch-colloquial-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-batch-colloquial-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
        source = self.service.gsb_colloquial_source(pair["id"])
        preview = {
            "verdict": "Same",
            "aReason": "A 在 merge.py 把保存问题修好了，接口也测过了，不过浏览器流程还没跑。",
            "bReason": "B 在 rebase.ts 把页面流程接好了，Docker 验收通过，不过新增测试还无法确认。",
        }

        applied = self.service.apply_gsb_colloquial_batch([{
            "pairId": pair["id"], "source": source, "preview": preview,
        }])

        self.assertEqual((applied["applied"], applied["skipped"], applied["failed"]), (1, 0, 0))
        review = self.db.one("SELECT verdict,a_reason,b_reason,confirmed_by FROM gsb_reviews WHERE pair_id=?", (pair["id"],))
        self.assertEqual(review, {"verdict": "Same", "a_reason": preview["aReason"],
                                  "b_reason": preview["bReason"], "confirmed_by": "审核人"})
        stale = self.service.apply_gsb_colloquial_batch([{
            "pairId": pair["id"], "source": source, "preview": preview,
        }])
        self.assertEqual((stale["applied"], stale["skipped"], stale["failed"]), (0, 1, 0))
        self.assertIn("原文已变化", stale["results"][0]["reason"])

    def test_gsb_recheck_cannot_pass_reasons_without_evidence_locators(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed',?,?)""",
            ("gsb-locator-source", pair["id"], "Same", "A：评价 A B：评价 B",
             "A 的结果完整，验收结果稳定。", "B 的结果完整，验收结果也稳定。", stamp, stamp),
        )
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-locator-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-locator-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
        payload = {
            "status": "passed", "suggestedVerdict": "Same",
            "suggestedAReason": "A 完成了核心要求，后续验收结果稳定，没有发现影响交付的问题。",
            "suggestedBReason": "B 也完成了核心要求，后续验收结果相同，因此两边表现接近。",
            "issues": [], "evidenceRefs": ["checks[A]", "checks[B]"],
        }
        independent = {
            "verdict": "Same", "aReason": payload["suggestedAReason"],
            "bReason": payload["suggestedBReason"], "evidence": ["checks[A]", "checks[B]"],
        }
        with patch.object(self.service.codex, "run", side_effect=[independent, payload, payload]) as mocked_run:
            result = self.service._recheck_gsb(pair["id"])
        self.assertEqual(result["result_status"], "suggested_revision")
        self.assertIn("A 评价缺少可核对的具体证据", result["issues_json"])
        self.assertIn("B 评价缺少可核对的具体证据", result["issues_json"])
        self.assertEqual(mocked_run.call_count, 3)

    def test_gsb_recheck_rewrites_mechanical_case_lists_even_when_model_first_passes(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        mechanical_a = (
            "A 在 rehearsal.spec.ts 第15、16、21、22项覆盖边界，"
            "83个单测、23个端到端测试和38秒录像通过，未见已发生的功能缺陷。"
        )
        mechanical_b = (
            "B 在 rehearsal.spec.ts 第15、17、19、21至24项覆盖边界，"
            "82个单测、25个端到端测试和38秒录像通过。"
        )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed',?,?)""",
            ("gsb-mechanical", pair["id"], "Same",
             self.service._compose_gsb_reason(mechanical_a, mechanical_b),
             mechanical_a, mechanical_b, stamp, stamp),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?)""",
                ("arm-mechanical-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", stamp, stamp),
            )
        first = {
            "status": "passed", "suggestedVerdict": "Same",
            "suggestedAReason": mechanical_a, "suggestedBReason": mechanical_b,
            "issues": [], "evidenceRefs": ["arms[A]", "arms[B]"],
        }
        revised_a = (
            "A 在 rehearsal.spec.ts 实际跑过边界两侧、写回和失效处理，"
            "这些流程都通过，功能链路完整。"
        )
        revised_b = (
            "B 在 rehearsal.spec.ts 也验证了平行边、非法输入和写回后重新考证，"
            "现有结果与 A 没有明显差距。"
        )
        second = {
            "status": "passed", "suggestedVerdict": "Same",
            "suggestedAReason": revised_a, "suggestedBReason": revised_b,
            "issues": [], "evidenceRefs": ["arms[A]", "arms[B]"],
        }
        independent = {
            "verdict": "Same", "aReason": revised_a, "bReason": revised_b,
            "evidence": ["arms[A]", "arms[B]"],
        }
        with patch.object(self.service.codex, "run", side_effect=[independent, first, second]) as mocked_run:
            result = self.service._recheck_gsb(pair["id"])
        self.assertEqual(mocked_run.call_count, 3)
        self.assertEqual(result["result_status"], "suggested_revision")
        self.assertEqual(result["suggested_a_reason"], revised_a)
        self.assertIn("机械罗列测试编号", result["issues_json"])

    def test_new_evidence_review_and_delivery_schema_is_available(self):
        recording_columns = {row["name"] for row in self.db.all("PRAGMA table_info(recordings)")}
        self.assertTrue({"commit_sha", "commit_match", "steps_json", "direct_url", "attempt_id", "capture_mode", "entry_url",
                         "review_status", "reviewed_by", "reviewed_at"} <= recording_columns)
        task_columns = {row["name"] for row in self.db.all("PRAGMA table_info(tasks)")}
        self.assertIn("project_category", task_columns)
        self.assertTrue({
            "estimated_module_count", "estimated_source_lines_min", "estimated_source_lines_max",
            "estimated_minutes_min", "estimated_minutes_max", "complexity_axes_json",
        } <= task_columns)
        attempt_columns = {row["name"] for row in self.db.all("PRAGMA table_info(recording_attempts)")}
        self.assertIn("interaction_mode", attempt_columns)
        bug_columns = {row["name"] for row in self.db.all("PRAGMA table_info(bug_candidates)")}
        self.assertIn("source_paths_json", bug_columns)
        self.assertTrue({
            "estimated_module_count", "estimated_source_lines_min", "estimated_source_lines_max",
            "estimated_minutes_min", "estimated_minutes_max", "complexity_axes_json",
        } <= bug_columns)
        gsb_columns = {row["name"] for row in self.db.all("PRAGMA table_info(gsb_reviews)")}
        self.assertTrue({"draft_verdict", "final_verdict", "evidence_version", "a_reason", "b_reason", "preference_reason"} <= gsb_columns)
        recheck_columns = {row["name"] for row in self.db.all("PRAGMA table_info(gsb_rechecks)")}
        self.assertTrue({"applied_at", "applied_by"} <= recheck_columns)
        arm_columns = {row["name"] for row in self.db.all("PRAGMA table_info(arm_runs)")}
        self.assertTrue({"attempt_no", "error_retry_count"} <= arm_columns)
        self.assertIsNotNone(self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='gsb_rechecks'"))
        self.assertIsNotNone(self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='delivery_submissions'"))
        delivery_columns = {row["name"] for row in self.db.all("PRAGMA table_info(delivery_submissions)")}
        self.assertTrue({"payload_sha256", "remote_status", "qc_summary", "remote_updated_at"} <= delivery_columns)
        self.assertIsNotNone(self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='recording_attempts'"))
        self.assertEqual(self.db.setting("gsb_recheck_model"), "gpt-6-astra")
        self.assertEqual(self.db.setting("gsb_recheck_effort"), "high")

    def test_database_restart_does_not_promote_passed_medium_bugfix_to_hard(self):
        self.insert_ready_bugfix_task("困难")
        pair = self.service.create_pair("task-bugfix")
        self.db.execute("UPDATE tasks SET difficulty='中等' WHERE id='task-bugfix'")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO difficulty_reviews(
                 id,pair_id,original_difficulty,a_difficulty,b_difficulty,assessed_difficulty,
                 reason,evidence_json,status,reviewed_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("difficulty-medium", pair["id"], "中等", "中等", "中等", "中等",
             "两侧都只需局部跨模块修改", '["实际改动属于中等"]', "passed", stamp, stamp, stamp),
        )

        self.db.initialize()

        review = self.db.one("SELECT assessed_difficulty FROM difficulty_reviews WHERE pair_id=?", (pair["id"],))
        task = self.db.one("SELECT difficulty FROM tasks WHERE id='task-bugfix'")
        self.assertEqual(review["assessed_difficulty"], "中等")
        self.assertEqual(task["difficulty"], "中等")

    def test_browser_recording_attempt_is_promoted_only_after_it_passes(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = (arm.lower() * 40)[:40]
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-browser-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            attempt_id = "attempt-browser-" + arm
            self.db.execute(
                """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,capture_mode,entry_url,
                   width,height,duration_seconds,sha256,status,started_at,finished_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,'browser','http://127.0.0.1:9000',1280,720,30,?,'passed',?,?,?,?)""",
                (attempt_id, pair["id"], arm, sha, str(self.root / (arm + ".webm")), arm * 64,
                 stamp, stamp, stamp, stamp),
            )
            self.service.recordings._promote(attempt_id)
        rows = self.db.all("SELECT * FROM recordings WHERE pair_id=? ORDER BY arm", (pair["id"],))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "passed" and row["capture_mode"] == "browser" for row in rows))
        self.assertTrue(all(row["review_status"] == "confirmed" and row["reviewed_at"] for row in rows))
        updated = self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(updated["stage"], "gsb_ready")

    def test_one_completed_arm_recording_does_not_advance_while_peer_develops(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        a_sha = "a" * 40
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-partial-A", pair["id"], "A", "A", str(self.root), "container-A",
             "screen-A", "auto_model/urm", "image", a_sha, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?)""",
            ("arm-partial-B", pair["id"], "B", "B", str(self.root), "container-B",
             "screen-B", "auto_model/urm", "image", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,'passed',?,?)""",
            ("check-partial-A", pair["id"], "A", a_sha, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,capture_mode,entry_url,
               width,height,duration_seconds,sha256,status,started_at,finished_at,created_at,updated_at)
               VALUES(?,?,?,?,?,'browser','http://127.0.0.1:9000',1280,720,12,?,'passed',?,?,?,?)""",
            ("attempt-partial-A", pair["id"], "A", a_sha, str(self.root / "A.webm"),
             "a" * 64, stamp, stamp, stamp, stamp),
        )
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?",
            (pair["id"],),
        )

        self.service.recordings._promote("attempt-partial-A")

        current = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "running", "stage": "development"})
        self.assertIsNotNone(self.db.one(
            "SELECT id FROM recordings WHERE pair_id=? AND arm='A' AND status='passed'",
            (pair["id"],),
        ))

    def test_frontend_automatic_recording_requires_reviewable_duration(self):
        self.assertEqual(minimum_recording_duration_seconds("auto", "纯前端"), 0)
        self.assertEqual(minimum_recording_duration_seconds("auto", "全栈"), 0)
        self.assertEqual(minimum_recording_duration_seconds("manual", "纯前端"), 0)
        self.assertEqual(minimum_recording_duration_seconds("auto", "纯后端"), 0)
        self.assertEqual(minimum_recording_duration_seconds("auto", "纯前端", False), 0)

    def test_api_recording_persists_the_demonstrated_business_endpoint(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        sha = "b" * 40
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-api-evidence", pair["id"], "B", "B", str(self.root), "container", "screen",
             "auto_model/urm", "image", sha, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,capture_mode,entry_url,
               width,height,duration_seconds,sha256,status,started_at,finished_at,created_at,updated_at)
               VALUES(?,?,?,?,?,'browser','http://127.0.0.1:9000/health/ready',1280,720,30,?,'passed',?,?,?,?)""",
            ("attempt-api-evidence", pair["id"], "B", sha, str(self.root / "B.webm"),
             "b" * 64, stamp, stamp, stamp, stamp),
        )

        self.service.recordings._promote(
            "attempt-api-evidence",
            {"path": "/v1/retention/status", "method": "get", "body": None},
        )

        recording = self.db.one("SELECT steps_json FROM recordings WHERE pair_id=?", (pair["id"],))
        self.assertEqual(
            json.loads(recording["steps_json"]),
            [{"path": "/v1/retention/status", "method": "get", "body": None}],
        )

    def test_rerecording_same_commits_preserves_confirmed_gsb(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = (arm.lower() * 40)[:40]
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-rerecord-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,sha256,width,height,duration_seconds,
                   attempt_id,commit_match,review_status,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,1280,720,30,?,1,'confirmed','passed',?,?)""",
                ("rec-old-" + arm, pair["id"], arm, str(self.root / (arm + "-old.mp4")), sha,
                 arm * 64, "attempt-old-" + arm, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,confirmed_by,
               confirmed_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed','刘昱',?,?,?)""",
            ("gsb-rerecord", pair["id"], "Same", "A：A 完成要求。 B：B 完成要求。",
             "A 完成要求。", "B 完成要求。", stamp, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO gsb_rechecks(id,pair_id,evidence_version,input_verdict,input_reason,result_status,
               model,reasoning_effort,created_at) VALUES(?,?,?,?,?,'passed','gpt-6-astra','high',?)""",
            ("recheck-rerecord", pair["id"], "old-version", "Same", "原评价", stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,created_at,updated_at)
               VALUES(?,?,'submitted',?,?)""",
            ("delivery-rerecord", pair["id"], stamp, stamp),
        )
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',winner='Same',completed_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,capture_mode,entry_url,
               width,height,duration_seconds,sha256,status,started_at,finished_at,created_at,updated_at)
               VALUES(?,?,?,?,?,'browser','http://127.0.0.1:9000',1280,720,42,?,'passed',?,?,?,?)""",
            ("attempt-new-A", pair["id"], "A", "a" * 40, str(self.root / "A-new.mp4"),
             "n" * 64, stamp, stamp, stamp, stamp),
        )

        self.service.recordings._promote("attempt-new-A")

        review = self.db.one("SELECT status,confirmed_by FROM gsb_reviews WHERE pair_id=?", (pair["id"],))
        self.assertEqual(review, {"status": "confirmed", "confirmed_by": "刘昱"})
        self.assertIsNotNone(self.db.one("SELECT id FROM gsb_rechecks WHERE id='recheck-rerecord'"))
        current = self.db.one("SELECT status,stage,winner FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "completed", "stage": "completed", "winner": "Same"})
        delivery = self.db.one("SELECT status FROM delivery_submissions WHERE pair_id=?", (pair["id"],))
        self.assertEqual(delivery["status"], "ready_to_submit")

    def test_recording_stop_immediately_enters_saving_state(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        output = self.root / "manual.mp4"
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,status,created_at,updated_at)
               VALUES(? ,?,'A',?,?, 'recording',?,?)""",
            ("attempt-stop", pair["id"], "a" * 40, str(output), stamp, stamp),
        )
        process = MagicMock()
        process.poll.return_value = None
        self.service.recordings._processes["attempt-stop"] = process

        row = self.service.recordings.stop(pair["id"], "A")

        self.assertEqual(row["status"], "stopping")
        self.assertTrue(Path(str(output) + ".stop").is_file())
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id='attempt-stop' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(event["event_type"], "recording.stop_requested")

    def test_service_restart_recovers_a_completed_stop_save(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,status,created_at,updated_at)
               VALUES(?,?,'B',?,?, 'stopping',?,?)""",
            ("attempt-recover", pair["id"], "b" * 40, str(self.root / "recovered.mp4"), stamp, stamp),
        )
        inspected = {
            "ok": True, "sha256": "c" * 64, "width": 1280, "height": 720,
            "duration_seconds": 32.5, "error": "",
        }
        with patch("pairwise_console.recording.inspect_recording", return_value=inspected), \
             patch.object(RecordingManager, "_promote") as promote:
            RecordingManager(self.config, self.db)

        recovered = self.db.one("SELECT * FROM recording_attempts WHERE id='attempt-recover'")
        self.assertEqual(recovered["status"], "passed")
        self.assertEqual(recovered["width"], 1280)
        promote.assert_called_once_with("attempt-recover")

    def test_api_only_recording_discovers_documented_excellon_request(self):
        workspace = self.root / "api-only"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            "# API\n\n### `POST /drill-files/statistics`\n\n"
            "Content-Type: text/plain\n\nM48 and METRIC are required.\n",
            encoding="utf-8",
        )

        demo = RecordingManager._discover_api_demo(workspace)

        self.assertEqual(demo["path"], "/drill-files/statistics")
        self.assertEqual(demo["headers"], {"content-type": "text/plain"})
        self.assertTrue(demo["body"].startswith("M48\nMETRIC\n"))

    def test_api_recording_uses_task_endpoint_readme_example(self):
        workspace = self.root / "api-task-example"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            """# API

```bash
curl -X POST http://localhost:8000/api/old \\
  -H 'Content-Type: application/json' \\
  -d '{"value": "old"}'
```

```bash
curl -X POST http://localhost:8000/api/v1/container-numbers/consensus \\
  -H 'Content-Type: application/json' \\
  -d '{"readings": ["CSQU3054384", "CSQU3054784"]}'
```
""",
            encoding="utf-8",
        )

        demo = RecordingManager._discover_api_demo(
            workspace,
            "新增 POST /api/v1/container-numbers/consensus，并保持原接口兼容。",
        )

        self.assertEqual(demo["path"], "/api/v1/container-numbers/consensus")
        self.assertEqual(demo["method"], "post")
        self.assertEqual(demo["headers"], {"content-type": "application/json"})
        self.assertEqual(
            demo["body"],
            {"readings": ["CSQU3054384", "CSQU3054784"]},
        )

    def test_api_recording_curl_data_without_explicit_method_is_post(self):
        workspace = self.root / "api-curl-implicit-post"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            """# API

```bash
curl -s http://localhost:8000/api/v1/deconvolve \\
  -H 'Content-Type: application/json' \\
  -d '{"peaks": [{"mz": "500.000000", "intensity": 1000}], "charges": [1]}'
```
""",
            encoding="utf-8",
        )
        demo = RecordingManager._discover_api_demo(workspace)
        self.assertEqual(demo["method"], "post")
        self.assertEqual(demo["path"], "/api/v1/deconvolve")
        self.assertEqual(demo["body"]["charges"], [1])

    def test_api_recording_uses_target_section_json_when_curl_is_absent(self):
        workspace = self.root / "api-target-json"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            """# API

### `POST /inspect`

```json
{"points": [{"x": 1, "y": 2}]}
```

### `POST /trace`

请求体：

```json
{"width": 600, "height": 400, "rows": 4, "cols": 10,
 "rotation": 0, "vertices": [{"x": 0, "y": 0}, {"x": 60, "y": 100}]}
```

响应说明。
""",
            encoding="utf-8",
        )

        demo = RecordingManager._discover_api_demo(
            workspace,
            "在现有能力上加入 POST /trace，返回裂纹路径。",
        )

        self.assertEqual(demo["path"], "/trace")
        self.assertEqual(demo["body"]["vertices"][1], {"x": 60, "y": 100})

    def test_api_recording_matches_documented_route_from_function_name(self):
        workspace = self.root / "api-function-route"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            """# API

### `POST /trace`

```json
{"width": 600, "height": 400, "rows": 4, "cols": 10,
 "rotation": 0, "vertices": [{"x": 59, "y": 99}, {"x": 61, "y": 101}]}
```
""",
            encoding="utf-8",
        )

        demo = RecordingManager._discover_api_demo(
            workspace,
            "修复 trace_polyline 对短线段仍物化全部网格边界的问题。",
        )

        self.assertEqual(demo["path"], "/trace")
        self.assertEqual(demo["body"]["width"], 600)

    def test_recording_upload_fixture_prefers_valid_feature_sample(self):
        workspace = self.root / "upload-fixture"
        fixtures = workspace / "e2e" / "fixtures"
        fixtures.mkdir(parents=True)
        (workspace / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
        (fixtures / "invalid.json").write_text('{"broken": true}', encoding="utf-8")
        (fixtures / "conflict.json").write_text('[{"cue":"A"}]', encoding="utf-8")
        (fixtures / "isolation.json").write_text('[{"cue":"A"},{"cue":"B"}]', encoding="utf-8")

        selected = RecordingManager._discover_upload_fixture(workspace)

        self.assertEqual(selected, fixtures / "isolation.json")

    def test_api_recording_uses_documented_multistep_workflow_without_named_route(self):
        workspace = self.root / "api-workflow"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            """# 灭菌柜 API

```bash
curl -X POST localhost:8080/devices -H 'Content-Type: application/json' \\
  -d '{"device_id":"STER-01","name":"灭菌柜 1"}'
curl -X PUT localhost:8080/devices/STER-01/policy -H 'Content-Type: application/json' \\
  -d '{"ttl_seconds":45}'
curl -X POST localhost:8080/devices/STER-01/windows
curl localhost:8080/devices/STER-01/status
```
""",
            encoding="utf-8",
        )

        demo = RecordingManager._discover_api_demo(
            workspace,
            "请新增设备策略更新接口，并保证设备超时隔离规则生效。",
        )

        self.assertEqual(len(demo["steps"]), 4)
        self.assertEqual(demo["steps"][0]["path"], "/devices")
        self.assertEqual(demo["steps"][0]["body"]["device_id"], "STER-01")
        self.assertEqual(demo["steps"][1]["method"], "put")
        self.assertEqual(demo["steps"][1]["path"], "/devices/STER-01/policy")
        self.assertEqual(demo["steps"][3]["method"], "get")

    def test_api_recording_uses_parameterless_documented_get_without_curl(self):
        workspace = self.root / "api-readonly-fallback"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            """# Event API

| 路径 | 用途 |
|---|---|
| `GET /health/ready` | 就绪检查 |

#### `GET /v1/producers/{name}`
#### `GET /v1/streams/read?cursor=<opaque>&pageSize=100`
#### `GET /v1/consumer-groups` / `DELETE /v1/consumer-groups/{name}`
#### `GET /v1/retention/status`
""",
            encoding="utf-8",
        )

        demo = RecordingManager._discover_api_demo(
            workspace,
            "实现多流事件日志与崩溃可恢复消费服务。",
        )

        self.assertEqual(demo["path"], "/v1/retention/status")
        self.assertEqual(demo["method"], "get")
        self.assertEqual(demo["headers"], {"accept": "application/json"})
        self.assertIsNone(demo["body"])

    def test_api_recording_prefers_table_post_and_request_example(self):
        workspace = self.root / "api-table-post"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            """# CAM API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1` | 版本信息 |
| POST | `/v1/audit` | 提交刀路并审计 |

### 请求

```json
{"tool_radius":"1.00","side":"left","contour":[]}
```

### 响应

```json
{"status":"safe"}
```
""",
            encoding="utf-8",
        )

        demo = RecordingManager._discover_api_demo(workspace, "复核闭合刀路")

        self.assertEqual(demo["path"], "/v1/audit")
        self.assertEqual(demo["method"], "post")
        self.assertEqual(demo["body"]["tool_radius"], "1.00")

    def test_api_recording_does_not_submit_a_documented_response_as_request(self):
        workspace = self.root / "api-response-only"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            """# Folding API

### `POST /api/v1/fold`

响应（存在最优结构）：

```json
{"status":"OPTIMAL","sequence":"GAAAAAAAAAAAAAAAAAAC","unique":true,
 "primary":{"structure":"(..................)"},"witness":null}
```
""",
            encoding="utf-8",
        )

        self.assertIsNone(RecordingManager._discover_api_demo(workspace))

    def test_api_recording_ignores_response_example_even_when_prompt_names_route(self):
        workspace = self.root / "api-response-target"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            """# Folding API

### `POST /api/v1/fold/ensemble`

请求包含 sequence 和 positions。

响应：

```json
{"sequence":"GAAAAAAAAAAAAAAAAAAC","length":20,"total":"6","positions":[]}
```
""",
            encoding="utf-8",
        )

        self.assertIsNone(RecordingManager._discover_api_demo(
            workspace, "提交 POST /api/v1/fold/ensemble 复核结果。"
        ))

    def test_api_recording_reads_parameterless_get_from_markdown_table(self):
        workspace = self.root / "api-table-get"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            """# API

| method | path | purpose |
|---|---|---|
| GET | `/healthz` | health check |
| GET | `/v1/profile` | supported profile and limits |
| GET | `/v1/items/{id}` | item detail |
""",
            encoding="utf-8",
        )

        demo = RecordingManager._discover_api_demo(workspace)

        self.assertEqual(demo["path"], "/v1/profile")
        self.assertEqual(demo["method"], "get")

    def test_api_recording_uses_literal_acceptance_request_when_readme_has_only_tables(self):
        workspace = self.root / "api-acceptance-example"
        acceptance = workspace / "acceptance"
        acceptance.mkdir(parents=True)
        (workspace / "README.md").write_text(
            """# API

| method | path | purpose |
|---|---|---|
| POST | `/api/v1/evidence-sets` | create a set |
| GET | `/api/v1/evidence-sets/{id}` | read a set |
| GET | `/healthz` | health check |
""",
            encoding="utf-8",
        )
        (acceptance / "run.py").write_text(
            """import httpx
API = "http://127.0.0.1:8080"
response = httpx.post(
    API + "/api/v1/evidence-sets",
    json={"client_request_id": "accept-create"},
)
""",
            encoding="utf-8",
        )

        demo = RecordingManager._discover_api_demo(workspace)

        self.assertEqual(demo["path"], "/api/v1/evidence-sets")
        self.assertEqual(demo["method"], "post")
        self.assertEqual(demo["body"], {"client_request_id": "accept-create"})

    def test_api_recording_discovers_javascript_request_without_readme(self):
        workspace = self.root / "api-js-acceptance-example"
        tests = workspace / "test"
        tests.mkdir(parents=True)
        (tests / "http.api.test.mjs").write_text(
            """async function call(base, method, path, options) {}
const signer = { publicB64Url: 'generated-at-runtime' };
await call(base, 'POST', '/v1/devices', {
  body: { deviceId: 'demo', publicKey: signer.publicB64Url },
});
""",
            encoding="utf-8",
        )

        demo = RecordingManager._discover_api_demo(workspace)

        self.assertEqual(demo["path"], "/v1/devices")
        self.assertEqual(demo["method"], "post")
        self.assertEqual(demo["body"]["deviceId"], "recording-demo-device")
        self.assertEqual(len(demo["body"]["publicKey"]), 43)

    def test_api_recording_loads_project_json_referenced_by_curl(self):
        workspace = self.root / "api-curl-file-example"
        examples = workspace / "examples"
        examples.mkdir(parents=True)
        payload = {
            "variables": ["A", "B"],
            "observations": [
                {"id": "m1", "left": "A", "right": "B", "xor_value": 1, "cost": 3},
            ],
            "references": [{"variable": "A", "value": 0}],
            "budget": 3,
        }
        (examples / "request.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )
        (workspace / "README.md").write_text(
            """# API

```bash
curl -X POST http://localhost:${HOST_PORT:-8000}/api/v1/adjudicate \\
  -H 'content-type: application/json' \\
  --data @examples/request.json
```
""",
            encoding="utf-8",
        )

        demo = RecordingManager._discover_api_demo(
            workspace, "提交实验数据并取得图谱裁决",
        )

        self.assertEqual(demo["path"], "/api/v1/adjudicate")
        self.assertEqual(demo["method"], "post")
        self.assertEqual(demo["body"], payload)

    def test_api_only_recording_accepts_health_endpoint(self):
        def response_for(request, timeout=0):
            if request.full_url.endswith("/healthz"):
                response = MagicMock()
                response.status = 200
                return response
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)

        with patch("pairwise_console.recording.urlopen", side_effect=response_for):
            url = RecordingManager._wait_for_url(8123)

        self.assertEqual(url, "http://127.0.0.1:8123/healthz")

    def test_api_only_recording_accepts_nested_health_endpoint(self):
        def response_for(request, timeout=0):
            if request.full_url.endswith("/health/ready"):
                response = MagicMock()
                response.status = 200
                return response
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)

        with patch("pairwise_console.recording.urlopen", side_effect=response_for):
            url = RecordingManager._wait_for_url(8123)

        self.assertEqual(url, "http://127.0.0.1:8123/health/ready")

    def test_fullstack_recording_prefers_root_frontend_over_swagger_docs(self):
        requested = []

        def response_for(request, timeout=0):
            requested.append(request.full_url)
            response = MagicMock()
            response.status = 200
            response.geturl.return_value = request.full_url
            response.headers.get.return_value = "text/html; charset=utf-8"
            response.read.return_value = b"<html><main>Workbench</main></html>"
            return response

        with patch("pairwise_console.recording.urlopen", side_effect=response_for):
            url = RecordingManager._wait_for_url(8123, require_frontend=True)

        self.assertEqual(url, "http://127.0.0.1:8123/")
        self.assertNotIn("http://127.0.0.1:8123/docs", requested)

    def test_g18_double_full_quota_blocks_only_excess_new_submissions(self):
        self.insert_ready_task()
        candidate = self.service.create_pair("task-1")
        fixed_now = datetime(2026, 9, 29, 11, 30, tzinfo=timezone.utc)
        stamp = now_iso()
        for index in range(9):
            pair_id = "pair-g18-%02d" % index
            self.db.execute(
                """INSERT INTO pairs(id,task_id,chain_id,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (pair_id, "task-1", candidate["chain_id"], stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO gsb_reviews(id,pair_id,a_score_delivery,b_score_delivery,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                ("gsb-g18-%02d" % index, pair_id, 5, 5 if index == 0 else 4, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO delivery_submissions(id,pair_id,status,remote_id,submitted_at,created_at,updated_at)
                   VALUES(?,?,'qc_passed',?,?,?,?)""",
                ("delivery-g18-%02d" % index, pair_id, str(19000 + index),
                 "2026-09-29T10:00:00" if index % 2 else "2026-09-29T10:00:00+00:00",
                 stamp, stamp),
            )
        issue = self.service._g18_double_full_issue(candidate["id"], 5, 5, fixed_now)
        self.assertIn("G18 当日双侧满分占比超限", issue)
        self.assertEqual(self.service._g18_double_full_issue(candidate["id"], 5, 4, fixed_now), "")
        self.db.execute("DELETE FROM delivery_submissions WHERE pair_id='pair-g18-08'")
        self.assertEqual(self.service._g18_double_full_issue(candidate["id"], 5, 5, fixed_now), "")
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,remote_id,submitted_at,created_at,updated_at)
               VALUES(?,?,'qc_passed',?,?,?,?)""",
            ("delivery-g18-08", "pair-g18-08", "19008", "2026-09-29T10:00:00", stamp, stamp),
        )
        self.db.execute("UPDATE gsb_reviews SET b_score_delivery=4 WHERE pair_id='pair-g18-00'")
        self.assertEqual(self.service._g18_double_full_issue(candidate["id"], 5, 5, fixed_now), "")
        self.db.execute("UPDATE gsb_reviews SET b_score_delivery=5 WHERE pair_id='pair-g18-00'")
        self.db.execute("UPDATE delivery_submissions SET submitted_at='2026-09-28T15:59:59+00:00' WHERE pair_id='pair-g18-00'")
        self.assertEqual(self.service._g18_double_full_issue(candidate["id"], 5, 5, fixed_now), "")
        self.assertEqual(self.service._solo_qa_submission_day("2026-09-29T16:00:00"),
                         datetime(2026, 9, 30).date())
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,status,a_score_delivery,b_score_delivery,created_at,updated_at)
               VALUES(?,?,'confirmed',5,5,?,?)""",
            ("gsb-g18-candidate", candidate["id"], stamp, stamp),
        )
        with patch.object(self.service, "_g18_double_full_issue", return_value="G18 配额测试拦截") as quota:
            preflight = self.service.delivery_preflight(candidate["id"])
        self.assertIn("G18 配额测试拦截", preflight["blockers"])
        quota.assert_called_once_with(candidate["id"], 5, 5)

    def test_delivery_preflight_allows_style_suggestion_but_blocks_fact_conflict(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = (arm.lower() * 40)[:40]
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,session_id,prompt_id,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?)""",
                ("arm-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm, "screen-" + arm,
                 "auto_model/urm", "image", "session-" + arm, "prompt-" + arm, sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,sha256,width,height,duration_seconds,
                   commit_match,status,created_at,updated_at) VALUES(?,?,?,?,?,?,1280,720,30,1,'passed',?,?)""",
                ("rec-" + arm, pair["id"], arm, str(self.root / (arm + ".mov")), sha, arm * 64, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,draft_verdict,draft_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'draft',?,?)""",
            ("gsb-complete", pair["id"], "Same", "A 和 B 均完成主要要求，验收结果一致，最终交付没有影响使用的差异。",
             "Same", "A 和 B 均完成主要要求，验收结果一致，最终交付没有影响使用的差异。", stamp, stamp),
        )
        self.service.confirm_gsb(
            pair["id"], "Same",
            "A 的 app/main.py 完成全部主要要求，docker compose run verify 显示核心流程可用，与 B 接近。",
            "B 的 app/main.py 也完成全部主要要求，docker compose run verify 呈现相同结果，因此判为 Same。",
            "刘昱",
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'ready',?,?)""",
            ("repo-preflight", pair["id"], "owner", "repo", "public", str(self.root), stamp, stamp),
        )
        check = self.service.delivery_preflight(pair["id"])
        self.assertTrue(check["eligible"])
        self.assertTrue(check["warnings"])
        self.db.execute(
            "UPDATE delivery_submissions SET status='needs_review',error='待独立复核：业务验收与录像结论冲突' WHERE pair_id=?",
            (pair["id"],),
        )
        held = self.service.delivery_preflight(pair["id"])
        self.assertIn("交付证据待独立复核，暂不可提交", held["blockers"])
        self.service.confirm_gsb(
            pair["id"], "Same",
            "A 的 app/main.py 完成全部主要要求，docker compose run verify 显示核心流程可用，与 B 接近。",
            "B 的 app/main.py 也完成全部主要要求，docker compose run verify 呈现相同结果，因此判为 Same。",
            "刘昱",
        )
        preserved_hold = self.db.one(
            "SELECT status,error FROM delivery_submissions WHERE pair_id=?", (pair["id"],),
        )
        self.assertEqual(preserved_hold["status"], "needs_review")
        self.assertTrue(preserved_hold["error"].startswith("待独立复核："))
        self.db.execute(
            "UPDATE delivery_submissions SET status='ready_to_submit',error='' WHERE pair_id=?", (pair["id"],),
        )
        self.db.execute("UPDATE tasks SET task_type='bugfix' WHERE id='task-1'")
        verifier_hash = hashlib.sha256(b"v1:[]").hexdigest()
        self.db.execute(
            """INSERT INTO bug_verification_results(pair_id,arm,commit_sha,verifier_hash,status,evidence_json,created_at)
               VALUES(?,?,?,?,'observed_failed','{}',?)""",
            (pair["id"], "A", "a" * 40, verifier_hash, stamp),
        )
        bug_conflict = self.service.delivery_preflight(pair["id"])
        self.assertIn("A 指定 Bug 独立验收失败，与产物通过结论冲突，须复核", bug_conflict["blockers"])
        self.db.execute("DELETE FROM bug_verification_results WHERE pair_id=?", (pair["id"],))
        self.db.execute("UPDATE tasks SET task_type='zero_to_one' WHERE id='task-1'")
        self.db.execute(
            """UPDATE gsb_reviews SET verdict='B better',final_verdict='B better',
               a_score_delivery=5,b_score_delivery=4 WHERE pair_id=?""",
            (pair["id"],),
        )
        conflict = self.service.delivery_preflight(pair["id"])
        self.assertFalse(conflict["eligible"])
        self.assertIn("GSB 优劣方向与交付评分相反，须独立复核后才能提交", conflict["blockers"])
        self.db.execute(
            """UPDATE gsb_reviews SET verdict='A better',final_verdict='A better',
               a_score_delivery=3,b_score_delivery=4 WHERE pair_id=?""",
            (pair["id"],),
        )
        reverse_conflict = self.service.delivery_preflight(pair["id"])
        self.assertFalse(reverse_conflict["eligible"])
        self.assertIn("GSB 优劣方向与交付评分相反，须独立复核后才能提交", reverse_conflict["blockers"])
        self.db.execute(
            """UPDATE gsb_reviews SET verdict='Same',final_verdict='Same',
               a_score_delivery=0,b_score_delivery=0 WHERE pair_id=?""",
            (pair["id"],),
        )
        self.db.execute("UPDATE tasks SET project_category='纯后端' WHERE id='task-1'")
        self.db.execute(
            "UPDATE recordings SET status='failed',commit_match=0,review_status='' WHERE pair_id=?",
            (pair["id"],),
        )
        backend_without_video = self.service.delivery_preflight(pair["id"])
        self.assertFalse(backend_without_video["eligible"])
        self.assertIn("A 录像未通过", backend_without_video["blockers"])
        self.assertIn("B 录像未通过", backend_without_video["blockers"])
        self.db.execute(
            "UPDATE recordings SET status='passed',commit_match=1,review_status='confirmed' WHERE pair_id=?",
            (pair["id"],),
        )
        self.db.execute("UPDATE git_repositories SET visibility='private' WHERE pair_id=?", (pair["id"],))
        private = self.service.delivery_preflight(pair["id"])
        self.assertFalse(private["eligible"])
        self.assertIn("GitHub 仓库不是公开仓库，SOLO-QA 无法核验分支与提交", private["blockers"])
        self.db.execute("UPDATE git_repositories SET visibility='public' WHERE pair_id=?", (pair["id"],))
        self.db.execute(
            """UPDATE delivery_submissions SET status='needs_fix',remote_id='remote-1',
               remote_status='PENDING_FIX' WHERE pair_id=?""",
            (pair["id"],),
        )
        self.service.confirm_gsb(
            pair["id"], "Same",
            "A 的 app/main.py 完成全部主要要求，docker compose run verify 显示核心流程可用，与 B 接近。",
            "B 的 app/main.py 也完成全部主要要求，docker compose run verify 呈现相同结果，因此判为 Same。",
            "刘昱",
        )
        self.assertEqual(
            self.db.one("SELECT status FROM delivery_submissions WHERE pair_id=?", (pair["id"],)),
            {"status": "needs_fix"},
        )
        review = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair["id"],))
        version = self.service.gsb_evidence_version(pair["id"], review["verdict"], review["reason"])
        self.db.execute(
            """INSERT INTO gsb_rechecks(id,pair_id,evidence_version,input_verdict,input_reason,result_status,
               suggested_verdict,suggested_reason,model,reasoning_effort,created_at)
               VALUES(?,?,?,?,?,'fact_conflict',?,?, 'gpt-6-astra','high',?)""",
            ("recheck-1", pair["id"], version, review["verdict"], review["reason"], "A better",
             "A 的验收更完整，B 存在会影响主要流程的问题，因此 A 更好。", stamp),
        )
        blocked = self.service.delivery_preflight(pair["id"])
        self.assertFalse(blocked["eligible"])
        self.assertIn("模型复检发现公开理由存在事实冲突", blocked["blockers"])
        handler = object.__new__(Handler)
        handler.server = MagicMock(db=self.db)
        self.assertEqual(handler._deliveries_page({"readiness": ["ready"]})["total"], 0)

        self.db.execute("UPDATE gsb_rechecks SET applied_at=?,applied_by='tester' WHERE id='recheck-1'", (stamp,))
        applied = self.service.delivery_preflight(pair["id"])
        self.assertTrue(applied["eligible"])
        self.assertNotIn("模型复检发现公开理由存在事实冲突", applied["blockers"])
        page = handler._deliveries_page({"readiness": ["ready"]})
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["readiness"], "ready")
        self.db.execute(
            "UPDATE delivery_submissions SET status='not_submitted',remote_id='' WHERE pair_id=?",
            (pair["id"],),
        )
        pending = handler._deliveries_page({"submission_status": ["ready_to_submit"]})
        self.assertEqual(pending["total"], 1)
        self.assertEqual(pending["items"][0]["submission_status"], "ready_to_submit")

    def test_delivery_xlsx_is_a_valid_workbook(self):
        payload, filename = build_xlsx([{
            "project_number": "chain-1", "pair_id": "pair-1", "title": "任务", "task_type": "feature",
            "difficulty": "困难", "prompt": "实现复杂功能", "main_sha": "1" * 40,
            "a_session_id": "sa", "a_prompt_id": "pa", "a_commit": "2" * 40,
            "b_session_id": "sb", "b_prompt_id": "pb", "b_commit": "3" * 40,
            "verdict": "A better", "a_reason": "A 的实际交付更完整。", "b_reason": "B 的主流程存在可复现问题。",
            "preference_reason": "A 的主要功能更可靠。",
            "readiness": "ready", "submission_status": "ready_to_submit",
        }])
        self.assertTrue(filename.endswith(".xlsx"))
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
            sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("项目编号", sheet)
        self.assertIn("pair-1", sheet)

    def test_delivery_list_exposes_each_missing_material(self):
        row = {
            "a_session_id": "session-a", "a_prompt_id": "prompt-a", "a_commit": "a" * 40,
            "a_check_status": "failed", "a_recording_status": "passed", "a_recording_match": 1,
            "a_recording_review_status": "confirmed",
            "b_session_id": "", "b_prompt_id": "", "b_commit": "", "b_check_status": None,
            "b_recording_status": None, "b_recording_match": 0, "b_recording_review_status": "pending",
            "gsb_status": "draft", "recheck_status": "fact_conflict",
        }
        result = Handler._decorate_delivery(row)
        self.assertEqual(result["readiness"], "blocked")
        self.assertNotIn("A Docker 验收失败", result["readiness_issues"])
        self.assertIn(
            "A Docker 验收失败，已作为最终 GSB 证据保留",
            result["readiness_warnings"],
        )
        self.assertIn("B 缺少 SessionID", result["readiness_issues"])
        self.assertIn("B 缺少通过的 Docker 验收", result["readiness_issues"])
        self.assertNotIn("B 缺少合格录像", result["readiness_issues"])
        self.assertIn("GSB 尚未确认", result["readiness_issues"])
        self.assertIn("复检发现公开理由存在事实冲突", result["readiness_issues"])

    def test_delivery_list_requires_recording_for_pure_backend(self):
        row = {
            "project_category": "纯后端",
            "a_session_id": "session-a", "a_prompt_id": "prompt-a", "a_commit": "a" * 40,
            "a_check_status": "passed", "a_recording_status": None,
            "b_session_id": "session-b", "b_prompt_id": "prompt-b", "b_commit": "b" * 40,
            "b_check_status": "passed", "b_recording_status": None,
            "gsb_status": "confirmed",
        }
        result = Handler._decorate_delivery(row)
        self.assertEqual(result["readiness"], "blocked")
        self.assertIn("A 缺少合格录像", result["readiness_issues"])
        self.assertIn("B 缺少合格录像", result["readiness_issues"])

    def test_missing_evidence_filter_requires_backend_recording(self):
        self.insert_ready_task()
        self.db.execute("UPDATE tasks SET project_category='纯后端' WHERE id='task-1'")
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-backend-evidence", pair["id"], "A", "A", str(self.root / "A"),
             "container-a", "screen-a", "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?, 'passed',?,?)""",
            ("check-backend-evidence", pair["id"], "A", "a" * 40, stamp, stamp),
        )
        handler = Handler.__new__(Handler)
        handler.server = MagicMock(db=self.db)
        self.assertEqual(handler._evidence_page({"missing": ["1"]})["total"], 1)
        backend_evidence = handler._evidence_page({"q": [pair["id"]]})["items"]
        self.assertIsNone(backend_evidence[0]["recording_status"])
        self.assertIsNone(backend_evidence[0]["commit_match"])
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        backend_delivery = handler._deliveries_page({"q": [pair["id"]]})["items"][0]
        self.assertIsNone(backend_delivery["a_recording_status"])
        self.db.execute("UPDATE tasks SET project_category='纯前端' WHERE id='task-1'")
        self.assertEqual(handler._evidence_page({"missing": ["1"]})["total"], 1)

    def test_delivery_list_allows_an_applied_fact_conflict_recheck(self):
        row = {
            "a_session_id": "session-a", "a_prompt_id": "prompt-a", "a_commit": "a" * 40,
            "a_check_status": "passed", "a_recording_status": "passed", "a_recording_match": 1,
            "a_recording_review_status": "confirmed",
            "b_session_id": "session-b", "b_prompt_id": "prompt-b", "b_commit": "b" * 40,
            "b_check_status": "passed", "b_recording_status": "passed", "b_recording_match": 1,
            "b_recording_review_status": "confirmed", "gsb_status": "confirmed",
            "recheck_status": "fact_conflict", "recheck_applied_at": now_iso(),
        }
        result = Handler._decorate_delivery(row)
        self.assertEqual(result["readiness"], "ready")
        self.assertNotIn("复检发现公开理由存在事实冲突", result["readiness_issues"])

    def test_historical_import_excludes_bug_and_medium(self):
        source = sqlite3.connect(str(self.config.old_db_path))
        source.executescript("""
        CREATE TABLE runs(id TEXT,repo_name TEXT,task_type TEXT,task_difficulty TEXT,language_framework TEXT,
          repo_path TEXT,repo_url TEXT,base_sha TEXT,first_prompt TEXT,status_detail TEXT,phase TEXT,
          created_at TEXT,deleted_at TEXT);
        """)
        rows = [
            ("1","hard-zero","0-1 代码生成","困难","Python","","","abc","hard prompt","","complete","2026-01-01",None),
            ("2","hard-bug","Bug 修复","困难","Python","","","def","bug prompt","","complete","2026-01-02",None),
            ("3","medium-zero","0-1 代码生成","中等","Python","","","ghi","medium prompt","","complete","2026-01-03",None),
        ]
        source.executemany("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        source.commit(); source.close()
        result = import_historical_tasks(self.db, self.config.old_db_path)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) count FROM tasks")["count"], 1)
        self.assertEqual(self.db.one("SELECT project_category FROM tasks")["project_category"], "纯后端")

    def test_dashboard_counts_each_pair_once_and_groups_task_and_system_types(self):
        self.insert_ready_task()
        self.db.execute("UPDATE tasks SET project_category='全栈' WHERE id='task-1'")
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-dashboard-" + arm, pair["id"], arm, arm * 8, stamp, stamp),
            )
        result = dashboard(self.db)
        self.assertEqual(result["summary"]["totalPairs"], 1)
        self.assertEqual(result["taskTypes"], [{"task_type": "zero_to_one", "count": 1}])
        self.assertEqual(result["projectCategories"], [{"project_category": "全栈", "count": 1}])
        self.assertNotIn("recentPairs", result)
        self.assertEqual(result["summary"]["completedPairs24h"], 0)
        self.assertEqual(len(result["trend24h"]), 24)

    def test_manual_recording_attempt_is_saved_as_manual_mode(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        compose = self.root / "compose.yaml"
        compose.write_text("services: {}\n", encoding="utf-8")
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-manual", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,compose_file,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'passed',?,?)""",
            ("check-manual", pair["id"], "A", "a" * 40, str(compose), stamp, stamp),
        )
        with patch("pairwise_console.recording.threading.Thread.start"):
            attempt = self.service.start_recording(pair["id"], "A", manual=True)
        self.assertEqual(attempt["interaction_mode"], "manual")

    def test_recording_demo_override_rejects_external_and_nonbusiness_paths(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        for path in ("https://example.com/api", "//example.com/api", "/health", "/docs"):
            with self.assertRaises(ValueError):
                self.service.start_recording(
                    pair["id"], "A", demo_override={"path": path, "method": "post", "body": {}},
                )

    def test_invalidating_current_recording_preserves_attempt_history(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'passed',?,?)""",
            ("attempt-old", pair["id"], "A", "a" * 40, str(self.root / "old.mp4"), stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,attempt_id,commit_match,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,1,'passed',?,?)""",
            ("recording-old", pair["id"], "A", str(self.root / "old.mp4"),
             "a" * 40, "attempt-old", stamp, stamp),
        )

        self.assertEqual(self.service._invalidate_recordings(pair["id"], ["A"], "Arm 已重新开发"), 1)
        self.assertIsNone(self.db.one("SELECT id FROM recordings WHERE pair_id=?", (pair["id"],)))
        self.assertIsNotNone(self.db.one("SELECT id FROM recording_attempts WHERE id='attempt-old'"))
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id=? ORDER BY id DESC LIMIT 1", (pair["id"],),
        )
        self.assertEqual(event["event_type"], "recording.current_invalidated")

    def test_failed_artifact_cannot_start_delivery_recording(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-failed-recording", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,error,created_at,updated_at)
               VALUES(?,?,?,?, 'failed',?,?,?,?)""",
            ("check-failed", pair["id"], "A", "a" * 40,
             '[{"name":"compose_file","passed":false,"detail":"未找到 Compose 文件"}]',
             "缺少 Docker Compose", stamp, stamp),
        )
        with self.assertRaisesRegex(ValueError, "Docker 产物验收尚未形成最终结论"):
            self.service.start_recording(pair["id"], "A")

    def test_observed_artifact_failure_cannot_start_recording(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-observed-recording", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,error,created_at,updated_at)
               VALUES(?,?,?,?, 'observed_failed',?,?,?,?)""",
            ("check-observed", pair["id"], "A", "a" * 40,
             '[{"name":"verify_service","passed":false,"command":"docker compose run --rm verify","exit_code":1,"detail":"ModuleNotFoundError: app"}]',
             "Docker 验收未全部通过", stamp, stamp),
        )
        with patch.object(self.service.recordings, "_launch"):
            attempt = self.service.start_recording(pair["id"], "A")
        self.assertEqual(attempt["capture_mode"], "failure_evidence")
        self.assertEqual(attempt["interaction_mode"], "failure")
        self.assertEqual(attempt["status"], "starting")
        Path(attempt["path"]).with_suffix(".failure.html").write_text(
            "验收失败证据录像 · 非功能通过", encoding="utf-8",
        )
        events = [{"event": "finished", "demonstration": {
            "interactionMode": "failure", "ok": True,
        }}]
        self.assertEqual(
            self.service.recordings._recording_integrity_error(
                attempt["id"], {"sha256": "evidence-hash"}, events, require_interaction=False,
            ), "",
        )
        self.db.execute(
            "UPDATE recording_attempts SET status='passed',sha256=?,width=1280,height=720,duration_seconds=8 WHERE id=?",
            ("evidence-hash", attempt["id"]),
        )
        self.service.recordings._promote(attempt["id"])
        media = self.db.one(
            "SELECT capture_mode,status FROM recordings WHERE pair_id=? AND arm='A'", (pair["id"],),
        )
        self.assertEqual(media, {"capture_mode": "failure_evidence", "status": "passed"})
        self.assertEqual("observed_failed", self.db.one(
            "SELECT status FROM artifact_checks WHERE id='check-observed'"
        )["status"])

    def test_one_shot_verify_timeout_and_missing_app_use_failure_evidence(self):
        compose = self.root / "docker-compose.yml"
        compose.write_text("services:\n  verify:\n    image: example\n", encoding="utf-8")
        timed_out = {
            "status": "observed_failed",
            "checks_json": json.dumps([{
                "name": "verify_service", "passed": False, "exit_code": 124,
                "detail": "自动验收超过 90 秒，已强制终止并清理容器",
            }]),
        }
        missing_app = {
            "status": "observed_failed",
            "checks_json": json.dumps([{
                "name": "application_service_present", "passed": False,
                "detail": "Compose 中没有可运行的应用服务",
            }]),
        }
        business_failure = {
            "status": "observed_failed",
            "checks_json": json.dumps([{
                "name": "verify_service", "passed": False, "exit_code": 1,
                "detail": "业务断言失败",
            }]),
        }

        self.assertFalse(RecordingManager._runtime_recording_is_allowed(timed_out, compose))
        self.assertFalse(RecordingManager._runtime_recording_is_allowed(missing_app, compose))
        self.assertTrue(RecordingManager._runtime_recording_is_allowed(business_failure, compose))

    def test_private_bug_failure_with_passing_docker_checks_records_real_page(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        compose = self.root / "docker-compose.yml"
        compose.write_text("services:\n  web:\n    image: example\n", encoding="utf-8")
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-private-failure", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,compose_file,checks_json,error,created_at,updated_at)
               VALUES(?,?,?,?, 'observed_failed',?,?,?,?,?)""",
            ("check-private-failure", pair["id"], "A", "a" * 40, str(compose),
             '[{"name":"health","passed":true},{"name":"verify_service","passed":true}]',
             "边界业务校验失败", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO bug_verification_results(pair_id,arm,commit_sha,verifier_hash,status,evidence_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (pair["id"], "A", "a" * 40, "verifier", "observed_failed", "{}", stamp),
        )
        with patch.object(self.service.recordings, "_launch"):
            attempt = self.service.start_recording(pair["id"], "A")
        self.assertEqual(attempt["capture_mode"], "browser")
        self.assertEqual(attempt["interaction_mode"], "auto")
        self.assertEqual("observed_failed", self.db.one(
            "SELECT status FROM artifact_checks WHERE id='check-private-failure'"
        )["status"])

    def test_failure_evidence_requires_an_actual_failure_record(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-no-failure-output", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,error,created_at,updated_at)
               VALUES(?,?,?,?, 'observed_failed','[]','',?,?)""",
            ("check-no-failure-output", pair["id"], "A", "a" * 40, stamp, stamp),
        )
        with patch.object(self.service.recordings, "_launch"):
            attempt = self.service.start_recording(pair["id"], "A")
        check = self.db.one("SELECT * FROM artifact_checks WHERE id='check-no-failure-output'")
        self.service.recordings._launch_failure_evidence(
            attempt["id"], self.root, Path(attempt["path"]), check,
        )
        result = self.db.one("SELECT status,error FROM recording_attempts WHERE id=?", (attempt["id"],))
        self.assertEqual(result["status"], "failed")
        self.assertIn("缺少可核对的原始失败输出", result["error"])

    def test_failed_artifact_is_preserved_for_gsb_without_claude_repair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-artifact-" + arm, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", arm.lower() * 40, stamp, stamp),
            )
        checks = {
            "A": {"status": "failed", "error": "缺少 Docker Compose 或 Dockerfile"},
            "B": {"status": "passed", "error": ""},
        }

        def validate(pair_id, arm, _workspace, commit_sha):
            item = checks[arm]
            check_id = "check-artifact-" + arm
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (check_id, pair_id, arm, commit_sha, item["status"], item["error"], stamp, stamp),
            )
            return self.db.one("SELECT * FROM artifact_checks WHERE id=?", (check_id,))

        with patch.object(self.service.artifacts, "validate", side_effect=validate), \
             patch.object(self.service, "_restart_arm_from_delivered_commit") as retry, \
             patch.object(self.service, "_submit_monitor") as submit, \
             patch.object(self.service, "_submit_auto") as background:
            result = self.service._validate_pair_artifacts(pair["id"])
        self.assertEqual(result["preserved"], ["A"])
        retry.assert_not_called()
        submit.assert_not_called()
        self.assertEqual(
            self.db.one("SELECT status FROM artifact_checks WHERE id='check-artifact-A'")["status"],
            "observed_failed",
        )
        current = self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current["stage"], "difficulty_review")
        background.assert_called_once_with("difficulty-" + pair["id"], self.service.reassess_actual_difficulty, pair["id"])

    def test_pending_artifact_retry_is_preserved_for_gsb_without_claude(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        baseline = "b" * 40
        delivered = "a" * 40
        stamp = now_iso()
        repo_root = self.root / "pair-repo"
        (repo_root / "A").mkdir(parents=True)
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',baseline_sha=? WHERE id=?",
            (baseline, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-retry", pair["id"], "owner", "repo", "public", str(repo_root),
             baseline, delivered, baseline, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'running','',?,?,?)""",
            ("arm-retry-A", pair["id"], "A", "A", str(self.root / "runtime-A"),
             "container-A", "screen-A", "auto_model/urm", "image",
             "轨迹首轮 User Prompt 重跑启动失败：Terminal 未就绪", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,error,created_at,updated_at)
               VALUES(?,?, 'A',?,'failed','clean start failed',?,?)""",
            ("check-retry-A", pair["id"], delivered, stamp, stamp),
        )
        prepared = repo_root / "A"
        with patch.object(self.service.claude, "reset_unsent_arm"), \
             patch.object(self.service.git, "prepare_arm_commit", return_value=prepared) as prepare, \
             patch.object(self.service.claude, "launch"), \
             patch.object(self.service.claude, "wait_until_ready"), \
             patch.object(self.service.claude, "materialize_repository") as materialize, \
             patch.object(self.service, "_send_prompt_with_pair_stagger") as send_prompt, \
             patch.object(self.service, "_monitor_arm", return_value={"status": "completed"}):
            result = self.service._recover_pending_retry(
                pair["id"], "arm-retry-A", "Build a hard project with Docker Compose"
            )
        prepare.assert_not_called()
        materialize.assert_not_called()
        send_prompt.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["commit_sha"], delivered)
        self.assertEqual(
            self.db.one("SELECT status FROM artifact_checks WHERE id='check-retry-A'")["status"],
            "observed_failed",
        )

    def test_artifact_retry_compares_new_work_with_delivered_commit(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        baseline = "b" * 40
        delivered = "a" * 40
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET baseline_sha=? WHERE id=?", (baseline, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-source", pair["id"], "owner", "repo", "public", str(self.root),
             baseline, delivered, baseline, stamp, stamp),
        )
        self.assertEqual(self.service._arm_comparison_sha(pair["id"], "A"), delivered)
        self.assertEqual(self.service._arm_comparison_sha(pair["id"], "B"), baseline)

    def test_scheduler_recovers_only_stale_unsent_retry(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'running',?,?)""",
            ("arm-stale-A", pair["id"], "A", "A", str(self.root / "runtime-A"),
             "container-A", "screen-A", "auto_model/urm", "image",
             "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
        )
        with patch.object(self.service, "_submit_monitor") as submit:
            self.service._schedule_pending_arm_retries(pair["id"])
        submit.assert_called_once_with(
            "retry-recover-arm-stale-A", self.service._recover_pending_retry,
            pair["id"], "arm-stale-A", "Build a hard project with Docker Compose",
        )

    def test_pending_prompt_retry_reuses_its_occupied_terminal_slot(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,image_id,status,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,'waiting_retry',?,?,?)""",
            ("arm-mismatched-A", pair["id"], "A", "A", str(self.root / "mismatched-A"),
             "container-mismatched-A", "screen-mismatched-A", "auto_model/urm", "image",
             "sha256:occupied", "轨迹首轮 User Prompt 与数据库原题面不一致",
             "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
        )
        with patch.object(self.service, "_available_development_arm_slots", return_value=0), \
             patch.object(self.service, "_submit_monitor", return_value=True) as submit:
            self.service._schedule_pending_arm_retries(pair["id"])
        submit.assert_called_once_with(
            "retry-recover-arm-mismatched-A", self.service._recover_pending_retry,
            pair["id"], "arm-mismatched-A", "Build a hard project with Docker Compose",
        )

    def test_pending_prompt_retry_archives_native_trace_before_reset(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,image_id,status,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,'waiting_retry',?,?,?)""",
            ("arm-archive-prompt-A", pair["id"], "A", "A", str(self.root / "archive-A"),
             "container-archive-A", "screen-archive-A", "auto_model/urm", "image",
             "sha256:occupied", "轨迹首轮 User Prompt 与数据库原题面不一致",
             now_iso(), now_iso()),
        )
        with patch.object(self.service.claude, "archive_failed_attempt") as archive, \
             patch.object(self.service.claude, "reset_unsent_arm") as reset, \
             patch.object(self.service, "_launch_arm_if_capacity", return_value=None):
            self.service._recover_pending_retry(
                pair["id"], "arm-archive-prompt-A", "Build a hard project with Docker Compose",
            )
        archive.assert_called_once()
        self.assertFalse(archive.call_args.kwargs["count_development_failure"])
        self.assertFalse(archive.call_args.kwargs["count_error_retry"])
        reset.assert_not_called()

    def test_pending_retry_hands_off_to_canonical_monitor_operation(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        baseline = "b" * 40
        stamp = now_iso()
        repo_root = self.root / "pending-retry-repo"
        canonical = repo_root / "A"
        canonical.mkdir(parents=True)
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',baseline_sha=? WHERE id=?",
            (baseline, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-pending-monitor", pair["id"], "owner", "repo", "public", str(repo_root),
             baseline, baseline, baseline, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'waiting_retry',?,?)""",
            ("arm-pending-monitor-A", pair["id"], "A", "A", str(self.root / "pending-workspace"),
             "container-pending-monitor", "screen-pending-monitor", "auto_model/urm", "image",
             stamp, stamp),
        )

        with patch.object(self.service.claude, "reset_unsent_arm"), \
             patch.object(self.service.claude, "launch"), \
             patch.object(self.service.claude, "wait_until_ready"), \
             patch.object(self.service.claude, "materialize_repository"), \
             patch.object(self.service, "_send_prompt_with_pair_stagger"), \
             patch.object(self.service, "_submit_monitor", return_value=True) as submit:
            result = self.service._recover_pending_retry(
                pair["id"], "arm-pending-monitor-A", "Build a hard project with Docker Compose",
            )

        submit.assert_called_once_with(
            "monitor-arm-pending-monitor-A", self.service._monitor_arm,
            pair["id"], "arm-pending-monitor-A", "Build a hard project with Docker Compose",
        )
        self.assertEqual(result["id"], "arm-pending-monitor-A")

    def test_checkpointed_arm_retries_only_git_push(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        traces = self.root / "completed-traces"
        traces.mkdir()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,trace_path,result,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'exported',?,?,?,?)""",
            ("arm-checkpoint-A", pair["id"], "A", "A", str(self.root / "workspace-A"),
             "container-A", "screen-A", "auto_model/urm", "image", str(traces),
             "finished", stamp, stamp),
        )
        delivered = "d" * 40
        with patch.object(self.service.git, "push_arm", return_value=delivered) as push:
            result = self.service._finish_checkpointed_arm(pair["id"], "arm-checkpoint-A")
        push.assert_called_once_with(pair["id"], "A")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["commit_sha"], delivered)
        self.assertEqual(result["trace_path"], str(traces))

    def test_completed_no_code_waits_until_prompt_deadline_before_failure(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.set_setting("first_prompt_stop_minutes", 60)
        workspace = self.root / "no-code-workspace"
        (workspace / ".git").mkdir(parents=True)
        traces = self.root / "no-code-traces"
        traces.mkdir()
        stamp = now_iso()
        arm_id = "arm-completed-no-code-A"
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,trace_path,prompt_sent_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'checkpointing',?,?,?,?)""",
            (arm_id, pair["id"], "A", "A", str(workspace), "container-A", "screen-A",
             "auto_model/urm", "image", str(traces), stamp, stamp, stamp),
        )
        with patch.object(self.service.claude, "has_business_code", return_value=False), \
             patch.object(self.service, "_handle_attempt_failure") as failure:
            deferred = self.service._finish_checkpointed_arm(pair["id"], arm_id)
            self.assertEqual(deferred["status"], "checkpointing")
            self.assertIn("等待发题后 60 分钟", deferred["error"])
            self.service._finish_checkpointed_arm(pair["id"], arm_id)
            failure.assert_not_called()
            self.assertEqual(self.db.one(
                "SELECT COUNT(*) AS n FROM audit_events WHERE event_type='claude.completed_no_code_deferred' AND entity_id=?",
                (arm_id,),
            )["n"], 1)

            old_stamp = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
            self.db.execute("UPDATE arm_runs SET prompt_sent_at=? WHERE id=?", (old_stamp, arm_id))
            self.service._finish_checkpointed_arm(pair["id"], arm_id)
            failure.assert_called_once()
            self.assertIn("没有形成相对初始环境的代码产出", failure.call_args.args[3])

    def test_scheduler_recovers_checkpoint_interrupted_before_trace_export(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        workspace = self.root / "workspace-A"
        workspace.mkdir()
        traces = self.root / "recovered-traces"
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,trace_path,result,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'checkpointing','',?,?,?)""",
            ("arm-export-recovery-A", pair["id"], "A", "A", str(workspace),
             "container-A", "screen-A", "auto_model/urm", "image", "finished", stamp, stamp),
        )

        with patch.object(self.service, "_submit_auto") as submit:
            self.service._schedule_checkpoint_pushes(pair["id"])
        submit.assert_called_once_with(
            "checkpoint-push-arm-export-recovery-A", self.service._resume_checkpointed_arm,
            pair["id"], "arm-export-recovery-A",
        )

        def export_trace(_arm):
            traces.mkdir()
            self.db.execute(
                "UPDATE arm_runs SET status='exported',trace_path=?,updated_at=? WHERE id=?",
                (str(traces), now_iso(), "arm-export-recovery-A"),
            )
            return traces

        delivered = "f" * 40
        with patch.object(self.service.claude, "export_and_stop", side_effect=export_trace) as export, \
             patch.object(self.service.git, "push_arm", return_value=delivered) as push:
            result = self.service._resume_checkpointed_arm(pair["id"], "arm-export-recovery-A")
        export.assert_called_once()
        push.assert_called_once_with(pair["id"], "A")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["trace_path"], str(traces))

    def test_scheduler_does_not_duplicate_checkpoint_work_owned_by_live_monitor(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'checkpointing',?,?)""",
            ("arm-live-checkpoint-A", pair["id"], "A", "A", str(self.root / "workspace-A"),
             "container-A", "screen-A", "auto_model/urm", "image", stamp, stamp),
        )
        monitor = MagicMock()
        monitor.done.return_value = False
        self.service._futures["monitor-arm-live-checkpoint-A"] = monitor

        with patch.object(self.service, "_submit_auto") as submit:
            self.service._schedule_checkpoint_pushes(pair["id"])
        submit.assert_not_called()

    def test_checkpointed_push_failure_preserves_code_and_trace(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        traces = self.root / "completed-traces"
        traces.mkdir()
        workspace = self.root / "workspace-A"
        workspace.mkdir()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,trace_path,result,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'checkpointing',?,?,?,?)""",
            ("arm-checkpoint-fail-A", pair["id"], "A", "A", str(workspace),
             "container-A", "screen-A", "auto_model/urm", "image", str(traces),
             "finished", stamp, stamp),
        )
        with patch.object(self.service.git, "push_arm", side_effect=TimeoutError("network timeout")):
            with self.assertRaisesRegex(TimeoutError, "network timeout"):
                self.service._finish_checkpointed_arm(pair["id"], "arm-checkpoint-fail-A")
        current = self.db.one("SELECT status,trace_path,error FROM arm_runs WHERE id='arm-checkpoint-fail-A'")
        self.assertEqual(current["status"], "checkpointing")
        self.assertEqual(current["trace_path"], str(traces))
        self.assertIn("等待重试 Git 推送", current["error"])
        self.assertTrue(workspace.is_dir())

    def test_stale_checkpoint_push_failure_does_not_overwrite_completed_arm(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        traces = self.root / "completed-traces"
        traces.mkdir()
        delivered = "e" * 40
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,trace_path,result,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'checkpointing',?,?,?,?)""",
            ("arm-race-A", pair["id"], "A", "A", str(self.root / "workspace-A"),
             "container-A", "screen-A", "auto_model/urm", "image", str(traces),
             "finished", stamp, stamp),
        )

        def stale_push(*_args):
            self.db.execute(
                """UPDATE arm_runs SET status='completed',commit_sha=?,error='',
                   finished_at=?,updated_at=? WHERE id='arm-race-A'""",
                (delivered, stamp, stamp),
            )
            raise RuntimeError("remote ref changed while pushing")

        with patch.object(self.service.git, "push_arm", side_effect=stale_push):
            result = self.service._finish_checkpointed_arm(pair["id"], "arm-race-A")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["commit_sha"], delivered)
        self.assertEqual(result["error"], "")

    def test_compose_port_variables_are_all_isolated(self):
        compose = self.root / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  api:\n    ports: ['${API_PORT:-8000}:8000']\n"
            "  web:\n    ports: ['${WEB_PORT:-8080}:80']\n",
            encoding="utf-8",
        )
        env, assigned = isolated_compose_environment(compose)
        self.assertEqual(env["API_PORT"], assigned["API_PORT"])
        self.assertEqual(env["WEB_PORT"], assigned["WEB_PORT"])
        self.assertNotEqual(assigned["API_PORT"], assigned["WEB_PORT"])
        self.assertTrue(all(value.isdigit() for value in assigned.values()))

    def test_bug_reproduction_uses_isolated_ports_for_every_compose_command(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        compose = self.root / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  api:\n    image: example\n    ports: ['${API_PORT:-8000}:8000']\n",
            encoding="utf-8",
        )
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("arm-bug-reproduction", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", "completed", "abc123", stamp, stamp),
        )
        command = [{
            "composeArgs": ["run", "--rm", "verify"],
            "expectedExitCode": 0,
            "expectedOutputContains": "reproduced",
        }]
        self.db.execute(
            """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,preconditions,
               reproduction_steps_json,reproduction_commands_json,actual_result,expected_result,difficulty,
               difficulty_evidence_json,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("bug-isolated-ports", pair["id"], "A", "abc123", "port-safe reproduction", "ready",
             '["run verifier"]', json.dumps(command), "reproduced", "fixed", "困难", '[]',
             "awaiting_reproduction", stamp, stamp),
        )
        with patch("pairwise_console.service.clean_commands", return_value={
            "passed": True, "commands": [], "assignedPorts": {"API_PORT": "49152"},
        }) as run:
            result = self.service.reproduce_bug("bug-isolated-ports")

        self.assertEqual(result["status"], "reproduced")
        self.assertTrue(run.call_args_list)
        self.assertEqual(run.call_count, 2)
        self.assertNotEqual(run.call_args_list[0].args[2], run.call_args_list[1].args[2])
        attempts = json.loads(result["reproduction_results_json"])
        self.assertEqual([item["assignedPorts"]["API_PORT"] for item in attempts], ["49152", "49152"])

    def test_bug_reproduction_rejects_oracle_that_does_not_fail_on_baseline(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("arm-bug-oracle", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", "completed", "a" * 40, stamp, stamp),
        )
        command = {"composeArgs": ["run", "--rm", "verify"], "expectedExitCode": 0,
                   "expectedOutputContains": "bug"}
        repair = [
            {**command, "scenario": scenario, "expectedOutputContains": "fixed",
             "failureOutputContains": "bug"}
            for scenario in ("original", "boundary", "regression")
        ]
        self.db.execute(
            """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,preconditions,
               reproduction_steps_json,reproduction_commands_json,actual_result,expected_result,difficulty,
               difficulty_evidence_json,repair_verification_json,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("bug-invalid-oracle", pair["id"], "A", "a" * 40, "invalid oracle", "ready",
             '["run verifier"]', json.dumps([command]), "bug", "fixed", "困难", '["跨模块"]',
             json.dumps(repair), "awaiting_reproduction", stamp, stamp),
        )
        with patch("pairwise_console.service.clean_commands", side_effect=[
            {"passed": True, "commands": [{"businessFailed": True}]},
            {"passed": True, "commands": [{"businessFailed": True}]},
            {"passed": True, "commands": [{"businessFailed": False}]},
        ]) as run:
            result = self.service.reproduce_bug("bug-invalid-oracle")

        self.assertEqual(run.call_count, 3)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("未能在缺陷基线确认业务失败", result["error"])
        self.assertEqual(len(json.loads(result["reproduction_results_json"])), 2)
        self.assertIsNotNone(self.db.one(
            "SELECT id FROM audit_events WHERE event_type='bug.invalid_repair_oracle_rejected' "
            "AND entity_id='bug-invalid-oracle'"
        ))

    def test_manual_bug_reproduction_retires_old_browser_candidate_once(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,preconditions,
               reproduction_steps_json,reproduction_commands_json,actual_result,expected_result,difficulty,
               difficulty_evidence_json,estimated_module_count,estimated_source_lines_min,
               estimated_source_lines_max,estimated_minutes_min,estimated_minutes_max,
               complexity_axes_json,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("bug-old-browser", pair["id"], "A", "a" * 40, "old browser candidate", "ready",
             '["click"]', '[{"composeArgs":["run","--rm","playwright"],"expectedExitCode":0,"expectedOutputContains":"bad"}]',
             "bad", "good", "困难", '["跨模块"]', 3, 80, 180, 45, 90,
             '["状态一致性"]', "awaiting_reproduction", stamp, stamp),
        )
        self.db.set_setting("manual_bug_only_mode", True)

        result = self.service.reproduce_bug("bug-old-browser")

        self.assertEqual(result["status"], "difficulty_rejected")
        self.assertIn("浏览器", result["error"])

    def test_host_port_collision_reuses_completed_commits(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-port-" + arm, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", arm.lower() * 40, stamp, stamp),
            )
        collision = {
            "status": "failed", "error": "Docker Compose 清洁启动失败",
            "checks_json": json.dumps([{
                "name": "clean_start", "passed": False,
                "detail": "Bind for 0.0.0.0:8080 failed: port is already allocated",
            }]),
        }
        checks = [{**collision, "arm": "A"}, {**collision, "arm": "B"}]
        with patch.object(self.service.artifacts, "validate", side_effect=checks), \
             patch.object(self.service, "_restart_arm_from_delivered_commit") as retry:
            result = self.service._validate_pair_artifacts(pair["id"])
        retry.assert_not_called()
        self.assertEqual(result["reused"], ["A", "B"])
        current = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "running", "stage": "artifact_validation"})
        arms = self.db.all("SELECT status,commit_sha FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair["id"],))
        self.assertEqual([arm["status"] for arm in arms], ["completed", "completed"])
        self.assertEqual([arm["commit_sha"] for arm in arms], ["a" * 40, "b" * 40])

    def test_container_name_collision_is_environment_failure(self):
        check = {
            "error": "Docker Compose 清洁启动失败",
            "checks_json": json.dumps([{
                "name": "clean_start", "passed": False,
                "detail": 'Conflict. The container name "/sealing-desk-web" is already in use by container "abc".',
            }]),
        }
        self.assertTrue(self.service._artifact_environment_failure(check))
        check["checks_json"] = json.dumps([{
            "name": "verify_service", "passed": False,
            "detail": "Business verifier rejected the configured container name",
        }])
        self.assertFalse(self.service._artifact_environment_failure(check))

    def test_recording_failure_waits_for_manual_rerecording(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-reuse-" + arm, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-reuse-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='recording_failed',error='old failure' WHERE id=?",
            (pair["id"],),
        )
        self.assertFalse(self.service._resume_one_reusable_pair())
        current = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "failed", "stage": "recording_failed", "error": "old failure"})
        arms = self.db.all("SELECT status,commit_sha FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair["id"],))
        self.assertEqual([arm["status"] for arm in arms], ["completed", "completed"])
        self.assertEqual([arm["commit_sha"] for arm in arms], ["a" * 40, "b" * 40])

    def test_pure_backend_with_passed_artifacts_waits_for_recordings(self):
        self.insert_ready_task()
        self.db.execute("UPDATE tasks SET project_category='纯后端' WHERE id='task-1'")
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-backend-record-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-backend-record-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
        self.db.execute(
            "UPDATE pairs SET status='running',stage='recording',error='' WHERE id=?",
            (pair["id"],),
        )

        self.service.refresh_recording_stage(pair["id"])
        current = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "running", "stage": "recording", "error": ""})
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) count FROM recordings WHERE pair_id=?", (pair["id"],),
        )["count"], 0)

    def test_recording_failure_is_not_silently_recovered_by_category(self):
        self.insert_ready_task()
        self.db.execute("UPDATE tasks SET project_category='纯前端' WHERE id='task-1'")
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='recording_failed',error='no browser entry' WHERE id=?",
            (pair["id"],),
        )

        current = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "failed", "stage": "recording_failed"})

    def test_single_arm_repair_preserves_passed_peer_and_resumes_only_failed_arm(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """UPDATE pairs SET status='repair_pending',stage='single_arm_repair_pending',
               development_failure_count=1 WHERE id=?""",
            (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,attempt_no,error_retry_count,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,3,2,?,?)""",
            ("arm-single-A", pair["id"], "A", "A", str(self.root / "A"), "container-A",
             "screen-A", "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,attempt_no,error_retry_count,error,finished_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'failed',3,3,'首轮超时且无代码产出',?,?,?)""",
            ("arm-single-B", pair["id"], "B", "B", str(self.root / "B"), "container-B",
             "screen-B", "auto_model/urm", "image", stamp, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?, 'passed',?,?)""",
            ("check-single-A", pair["id"], "A", "a" * 40, stamp, stamp),
        )

        submitted = []
        with patch.object(
            self.service, "_submit_monitor",
            side_effect=lambda operation, fn, *args: submitted.append((operation, args)) or True,
        ):
            self.assertTrue(self.service._resume_one_single_arm_repair())

        current = self.db.one(
            "SELECT status,stage,development_failure_count FROM pairs WHERE id=?", (pair["id"],),
        )
        self.assertEqual(current, {
            "status": "running", "stage": "development", "development_failure_count": 1,
        })
        arms = self.db.all(
            "SELECT arm,status,commit_sha,attempt_no,error_retry_count,api_retry_count,last_api_error FROM arm_runs WHERE pair_id=? ORDER BY arm",
            (pair["id"],),
        )
        self.assertEqual(arms[0], {
            "arm": "A", "status": "completed", "commit_sha": "a" * 40,
            "attempt_no": 3, "error_retry_count": 2, "api_retry_count": 0,
            "last_api_error": "",
        })
        self.assertEqual(arms[1], {
            "arm": "B", "status": "queued", "commit_sha": "",
            "attempt_no": 1, "error_retry_count": 0, "api_retry_count": 0,
            "last_api_error": "",
        })
        self.assertEqual(submitted[0][0], "retry-recover-arm-single-B")
        self.assertEqual(submitted[0][1][0:2], (pair["id"], "arm-single-B"))

    def test_single_arm_repair_can_preserve_peer_with_terminal_failure_evidence(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='repair_pending',stage='single_arm_repair_pending' WHERE id=?",
            (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-observed-B", pair["id"], "B", "B", str(self.root / "B"), "container-B",
             "screen-B", "auto_model/urm", "image", "b" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'failed','API 504',?,?)""",
            ("arm-retry-A", pair["id"], "A", "A", str(self.root / "A"), "container-A",
             "screen-A", "auto_model/urm", "image", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?, 'observed_failed',?,?)""",
            ("check-observed-B", pair["id"], "B", "b" * 40, stamp, stamp),
        )

        submitted = []
        with patch.object(
            self.service, "_submit_monitor",
            side_effect=lambda operation, fn, *args: submitted.append((operation, args)) or True,
        ):
            self.assertTrue(self.service._resume_one_single_arm_repair())

        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "running", "stage": "development"},
        )
        self.assertEqual(
            self.db.one("SELECT status,commit_sha FROM arm_runs WHERE id='arm-observed-B'"),
            {"status": "completed", "commit_sha": "b" * 40},
        )
        self.assertEqual(
            self.db.one("SELECT status,attempt_no FROM arm_runs WHERE id='arm-retry-A'"),
            {"status": "queued", "attempt_no": 1},
        )
        self.assertEqual(submitted[0][0], "retry-recover-arm-retry-A")

    def test_recording_stage_waits_for_every_passed_arm_without_name_error(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='recording',updated_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-record-stage-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-record-stage-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
        self.service.refresh_recording_stage(pair["id"])
        current = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "running", "stage": "recording"})

    def test_deterministic_recording_failure_does_not_relaunch_same_project(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='recording',updated_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-record-loop-A", pair["id"], "A", "A", str(self.root / "A"), "container-A",
             "screen-A", "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?, 'passed',?,?)""",
            ("check-record-loop-A", pair["id"], "A", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,interaction_mode,status,error,
               created_at,updated_at) VALUES(?,?,?,?,?,'auto','failed',?,?,?)""",
            ("attempt-deterministic", pair["id"], "A", "a" * 40, str(self.root / "failed.mp4"),
             "Error: 没有完成可见的真实功能操作", stamp, stamp),
        )

        with patch.object(self.service, "start_recording") as start:
            self.service._schedule_next_automatic_recording([
                self.db.one("SELECT * FROM pairs WHERE id=?", (pair["id"],)),
            ])

        start.assert_not_called()
        current = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["stage"], "recording_failed")
        self.assertIn("确定性演示错误", current["error"])

    def test_unsuccessful_business_api_recording_is_deterministic(self):
        self.assertTrue(self.service._recording_failure_is_deterministic(
            "Error: 没有成功的业务接口请求"
        ))

    def test_recording_interaction_rejects_destructive_click_and_malformed_counts(self):
        evidence = {
            "event": "interaction", "workflow": "browser-ui", "ok": True,
            "clicks": ["载入示例", "开始排程", "删"], "featureCount": 3,
            "resultControlCount": 0, "controlsComplete": True,
            "finalResultVisible": True,
        }
        self.assertIn("破坏性控件", recording_interaction_issue([evidence]))
        evidence["clicks"] = ["载入示例", "开始排程"]
        evidence["visibleChange"] = True
        self.assertEqual(recording_interaction_issue([evidence]), "")
        evidence["controlsComplete"] = False
        self.assertEqual(recording_interaction_issue([evidence]), "")
        evidence["resultControlCount"] = 4
        self.assertIn("控件计数", recording_interaction_issue([evidence]))
        evidence["resultControlCount"] = 0
        evidence["failedPageAssets"] = True
        self.assertIn("资源加载失败", recording_interaction_issue([evidence]))
        evidence["failedPageAssets"] = False
        evidence["visibleChange"] = False
        self.assertIn("没有产生可见结果", recording_interaction_issue([evidence]))
        evidence["interactionMode"] = "manual"
        self.assertEqual("", recording_interaction_issue([evidence], "manual"))

    def test_recording_interaction_accepts_successful_backend_api_evidence(self):
        event = {
            "event": "interaction", "required": True, "ok": True,
            "status": 200, "method": "post", "path": "/api/v1/evidence-sets",
            "stepCount": 1, "compatibilityRequest": True,
        }
        self.assertEqual(recording_interaction_issue([event]), "")
        event.update(workflow="bare-json-api-operations", clicks=1)
        self.assertEqual(recording_interaction_issue([event]), "")
        event.update(workflow="browser-ui")
        self.assertIn("点击清单", recording_interaction_issue([event]))

    def test_recording_integrity_rejects_unsafe_click_even_if_recorder_reports_ok(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-unsafe-click", pair["id"], "B", "B", str(self.root), "container-b",
             "screen-b", "model", "image", "b" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,created_at,updated_at)
               VALUES(?,?,?,?,'passed','[]',?,?)""",
            ("check-unsafe-click", pair["id"], "B", "b" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,interaction_mode,
               status,created_at,updated_at) VALUES(?,?,?,?,?,'auto','recording',?,?)""",
            ("attempt-unsafe-click", pair["id"], "B", "b" * 40,
             str(self.root / "unsafe.mp4"), stamp, stamp),
        )
        events = [{"event": "interaction", "ok": True, "workflow": "browser-ui",
                   "clicks": ["开始排程", "删"], "featureCount": 2,
                   "resultControlCount": 0, "controlsComplete": True,
                   "finalResultVisible": True}, {"event": "finished"}]
        issue = self.service.recordings._recording_integrity_error(
            "attempt-unsafe-click", {"sha256": "f" * 64}, events,
        )
        self.assertIn("破坏性控件", issue)
        self.assertIn("业务接口", self.service.recordings._recording_integrity_error(
            "attempt-unsafe-click", {"sha256": "f" * 64}, [
                {"event": "interaction", "ok": True, "method": "get", "path": "/", "status": 200},
                {"event": "interaction", "ok": False, "method": "post", "path": "/api/v1/deconvolve", "status": 422},
                {"event": "finished", "reason": "automatic_workflow_complete"},
            ],
        ))
        self.assertIn("达到时限", self.service.recordings._recording_integrity_error(
            "attempt-unsafe-click", {"sha256": "f" * 64}, [
                {"event": "interaction", "ok": True, "method": "post", "path": "/api/v1/deconvolve", "status": 200},
                {"event": "finished", "reason": "maximum_duration"},
            ],
        ))

    def test_automatic_recording_rejects_incomplete_control_traversal(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-incomplete-recording", pair["id"], "B", "B", str(self.root),
             "container-b", "screen-b", "model", "image", "b" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,created_at,updated_at)
               VALUES(?,?,?,?,'passed','[]',?,?)""",
            ("check-incomplete-recording", pair["id"], "B", "b" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,interaction_mode,
               status,created_at,updated_at) VALUES(?,?,?,?,?,'auto','recording',?,?)""",
            ("attempt-incomplete-recording", pair["id"], "B", "b" * 40,
             str(self.root / "incomplete.mp4"), stamp, stamp),
        )
        events = [
            {"event": "interaction", "ok": True, "workflow": "browser-ui",
             "clicks": ["开始"], "featureCount": 1, "resultControlCount": 0,
             "controlsComplete": False, "finalResultVisible": True},
            {"event": "finished", "reason": "maximum_duration"},
        ]
        issue = self.service.recordings._recording_integrity_error(
            "attempt-incomplete-recording", {"sha256": "f" * 64}, events,
        )
        self.assertIn("未遍历完", issue)

    def test_manual_recording_does_not_require_every_transient_control(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-manual", pair["id"], "A", "A", str(self.root), "container-a",
             "screen-a", "model", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,created_at,updated_at)
               VALUES(?,?,?,?,'passed','[]',?,?)""",
            ("check-manual-recording", pair["id"], "A", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,interaction_mode,
               status,created_at,updated_at) VALUES(?,?,?,?,?,'manual','recording',?,?)""",
            ("attempt-manual", pair["id"], "A", "a" * 40,
             str(self.root / "manual.mp4"), stamp, stamp),
        )
        check = self.service.recordings._recording_integrity_error
        self.assertIn("可核验", check("attempt-manual", {"sha256": "f" * 64}, [
            {"event": "finished", "demonstration": {"interactionMode": "manual", "clicks": 10}},
        ]))
        self.assertEqual("", check("attempt-manual", {"sha256": "f" * 64}, [
            {"event": "interaction", "ok": True, "workflow": "browser-ui", "clicks": ["开始"],
             "featureCount": 1, "resultControlCount": 0,
             "controlsComplete": False, "finalResultVisible": True},
            {"event": "finished"},
        ]))
        self.assertEqual("", check("attempt-manual", {"sha256": "f" * 64}, [
            {"event": "interaction", "ok": True, "workflow": "browser-ui", "clicks": ["开始"],
             "featureCount": 1, "resultControlCount": 0,
             "controlsComplete": True, "finalResultVisible": True},
            {"event": "finished"},
        ]))
        self.assertIn("失败证据画面", check("attempt-manual", {"sha256": "f" * 64}, [
            {"event": "finished", "demonstration": {"interactionMode": "failure"}},
        ], require_interaction=False))

    def test_recording_prefers_frontend_published_port(self):
        compose_ps = json.dumps([
            {"Service": "api", "Publishers": [{"PublishedPort": 51001}]},
            {"Service": "web", "Publishers": [{"PublishedPort": 51002}]},
        ])
        with patch("pairwise_console.recording.run_command", return_value=MagicMock(stdout=compose_ps)):
            port = RecordingManager._published_port(["docker", "compose"], self.root, {})
        self.assertEqual(port, 51002)

    def test_recording_prefers_published_api_port_over_dns_publishers(self):
        compose_ps = json.dumps([
            {"Service": "edge", "Publishers": [
                {"TargetPort": 53, "PublishedPort": 53101, "Protocol": "tcp"},
                {"TargetPort": 53, "PublishedPort": 53101, "Protocol": "udp"},
                {"TargetPort": 8080, "PublishedPort": 53102, "Protocol": "tcp"},
            ]},
        ])
        with patch("pairwise_console.recording.run_command", return_value=MagicMock(stdout=compose_ps)):
            port = RecordingManager._published_port(
                ["docker", "compose"], self.root, {}, preferred_ports=[53102, 53101],
            )
        self.assertEqual(port, 53102)

    def test_artifact_checker_uses_documented_project_verifier(self):
        workspace = self.root / "documented-verifier"
        script = workspace / "scripts" / "verify.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('ok')\n", encoding="utf-8")
        (workspace / "README.md").write_text(
            "```bash\npython3 scripts/verify.py examples/request.json http://localhost:8080\n```\n",
            encoding="utf-8",
        )

        command = self.service.artifacts._documented_verifier(
            workspace, {"API_PORT": "54321"},
        )

        self.assertEqual(command, [
            "python3", "scripts/verify.py", "examples/request.json", "http://localhost:54321",
        ])

    def test_project_verifier_has_no_fixed_timeout(self):
        completed = CommandResult(
            ["docker", "compose", "run", "verify"], str(self.root), 0,
            "passed", "",
        )
        with patch("pairwise_console.artifact.run_command", return_value=completed) as mocked:
            result = self.service.artifacts._run_project_verifier(
                ["docker", "compose", "run", "verify"], self.root, {},
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "passed")
        self.assertIsNone(mocked.call_args.kwargs["timeout"])

    def test_artifact_clean_start_has_no_fixed_timeout(self):
        workspace = self.root / "slow-build"
        workspace.mkdir()
        (workspace / "docker-compose.yml").write_text(
            "services:\n  web:\n    build: .\n", encoding="utf-8",
        )
        (workspace / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        calls = []

        def command(args, **kwargs):
            calls.append((list(args), kwargs.get("timeout")))
            if args[-4:] == ["--profile", "*", "config", "--services"]:
                return CommandResult(list(args), str(workspace), 0, "web\n", "")
            if args[-3:] == ["ps", "--format", "json"]:
                return CommandResult(list(args), str(workspace), 0, '[{"Service":"api","State":"running","Health":"healthy"},{"Service":"web","State":"running","Health":"healthy"}]', "")
            return CommandResult(list(args), str(workspace), 0, "ok", "")

        with patch("pairwise_console.artifact.run_command", side_effect=command):
            result = self.service.artifacts._probe(workspace, "slow-build")

        up_calls = [item for item in calls if "up" in item[0]]
        self.assertEqual(up_calls, [(["docker", "compose", "-p", "slow-build", "-f",
                                     str(workspace / "docker-compose.yml"), "up", "-d", "--build", "web"], None)])
        # The fixture intentionally has no project-owned verifier, so the
        # overall probe still fails for that independent reason.
        self.assertEqual(result["status"], "failed")

    def test_artifact_starts_apps_once_and_runs_verify_once(self):
        workspace = self.root / "one-shot-verify"
        workspace.mkdir()
        (workspace / "docker-compose.yml").write_text(
            "services:\n  api:\n    build: .\n  web:\n    build: .\n  verify:\n    build: .\n",
            encoding="utf-8",
        )
        (workspace / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        calls = []

        def command(args, **kwargs):
            calls.append(list(args))
            if args[-4:] == ["--profile", "*", "config", "--services"]:
                return CommandResult(list(args), str(workspace), 0, "api\nweb\nverify\n", "")
            if args[-3:] == ["ps", "--format", "json"]:
                return CommandResult(list(args), str(workspace), 0, '[{"Service":"api","State":"running","Health":"healthy"},{"Service":"web","State":"running","Health":"healthy"}]', "")
            return CommandResult(list(args), str(workspace), 0, "ok", "")

        with patch("pairwise_console.artifact.run_command", side_effect=command), \
             patch("pairwise_console.artifact.time.sleep"):
            result = self.service.artifacts._probe(workspace, "one-shot-verify")

        up_calls = [args for args in calls if "up" in args]
        verify_calls = [args for args in calls if "run" in args]
        verify_build_calls = [args for args in calls if "build" in args and "run" not in args and "up" not in args]
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(up_calls), 1)
        self.assertEqual(up_calls[0][-5:], ["up", "-d", "--build", "api", "web"])
        self.assertEqual(len(verify_build_calls), 1)
        self.assertEqual(verify_build_calls[0][-4:], ["--profile", "*", "build", "verify"])
        self.assertEqual(len(verify_calls), 1)
        self.assertEqual(
            verify_calls[0][-6:],
            ["--profile", "*", "run", "--rm", "--no-deps", "verify"],
        )

    def test_artifact_verify_detail_keeps_stdout_failure_after_stderr_tests(self):
        result = CommandResult(
            ["docker", "compose", "run", "verify"], str(self.root), 1,
            "setup\n" + ("output\n" * 300) + "[FAIL] HTTP smoke could not reach service\n",
            "test passed\n" * 100,
        )
        detail = self.service.artifacts._verifier_detail(result)
        self.assertLessEqual(len(detail), 1200)
        self.assertIn("[FAIL] HTTP smoke could not reach service", detail)
        self.assertIn("test passed", detail)

    def test_fullstack_recording_rejects_backend_docs_and_health_pages(self):
        self.assertFalse(RecordingManager._entry_matches_project_category("全栈", "http://127.0.0.1:8000/docs"))
        self.assertFalse(RecordingManager._entry_matches_project_category("纯前端", "http://127.0.0.1:8000/healthz"))
        self.assertTrue(RecordingManager._entry_matches_project_category("全栈", "http://127.0.0.1:8080/"))
        self.assertTrue(RecordingManager._entry_matches_project_category("纯后端", "http://127.0.0.1:8000/docs"))

    def test_recording_prefers_api_published_port_over_database(self):
        compose_ps = json.dumps([
            {"Service": "db", "Publishers": [{"PublishedPort": 52001}]},
            {"Service": "api1", "Publishers": [{"PublishedPort": 52002}]},
            {"Service": "api2", "Publishers": [{"PublishedPort": 52003}]},
        ])
        with patch("pairwise_console.recording.run_command", return_value=MagicMock(stdout=compose_ps)):
            port = RecordingManager._published_port(["docker", "compose"], self.root, {})
        self.assertEqual(port, 52002)

    def test_artifact_failure_pair_reopens_for_commit_based_repair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-artifact-reuse-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='artifact_failed',error='missing Dockerfile' WHERE id=?",
            (pair["id"],),
        )
        self.assertTrue(self.service._resume_one_reusable_pair())
        current = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "running", "stage": "artifact_validation", "error": ""})
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id=? ORDER BY id DESC LIMIT 1", (pair["id"],),
        )
        self.assertEqual(event["event_type"], "artifact.revalidation_started")

    def test_reusable_pair_waits_when_all_pair_slots_are_occupied(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-capacity-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 arm.lower() * 40, stamp, stamp),
            )
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='artifact_failed',error='missing Dockerfile' WHERE id=?",
            (pair["id"],),
        )
        for index in range(4):
            self.db.execute(
                """INSERT INTO pairs(id,task_id,chain_id,status,stage,created_at,updated_at)
                   VALUES(?,?,?,'running','development',?,?)""",
                ("pair-active-%d" % index, "task-1", pair["chain_id"], stamp, stamp),
            )

        self.assertFalse(self.service._resume_one_reusable_pair())
        current = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {
            "status": "failed", "stage": "artifact_failed", "error": "missing Dockerfile",
        })

    def test_reusable_pair_queues_remote_cross_arm_prompt_repair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,trace_path,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?)""",
                ("arm-remote-prompt-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 str(self.root / "traces" / arm), arm.lower() * 40, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,remote_id,qc_summary,created_at,updated_at)
               VALUES(?,?,'needs_fix','387','两份轨迹里的 prompt 不一致（相似度 78.5%）',?,?)""",
            ("delivery-remote-prompt", pair["id"], stamp, stamp),
        )
        with patch.object(self.service, "_inspect_trace", side_effect=[
                (Path("A.jsonl"), "2.1.269", []), (Path("B.jsonl"), "2.1.269", []),
             ]), \
             patch.object(self.service, "_trace_first_user_prompt", side_effect=["prompt A", "prompt B"]), \
             patch.object(self.service, "_restart_trace_invalid_arms", return_value={}) as restart:
            self.assertTrue(self.service._resume_one_reusable_pair())
        restart.assert_called_once()
        self.assertEqual(restart.call_args.args[0], pair["id"])
        self.assertEqual({row["arm"] for row in restart.call_args.args[1]}, {"A", "B"})

    def test_completed_trace_prompt_mismatch_restarts_only_invalid_arm(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,session_id,prompt_id,trace_path,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?,?)""",
                ("arm-trace-" + arm, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", "session-" + arm, "prompt-" + arm,
                 str(self.root / "traces" / arm), arm.lower() * 40, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed',?,?)""",
            ("gsb-trace", pair["id"], "Same", "old", "old A", "old B", stamp, stamp),
        )
        restarted = {"id": "arm-trace-B", "arm": "B", "status": "developing"}
        with patch.object(self.service, "_inspect_trace", side_effect=[
                (Path("A.jsonl"), "2.1.269", []),
                (Path("B.jsonl"), "2.1.269", ["B 轨迹中没有与题面逐字一致的首轮 User Prompt"]),
             ]), \
             patch.object(self.service, "_restart_arm_from_baseline", return_value=restarted) as restart, \
             patch.object(self.service, "_submit_monitor") as submit, \
             patch.object(self.service, "_submit") as submit_operation:
            self.service._refresh_pair_after_arm(pair["id"])
        restart.assert_called_once()
        restart_args, restart_kwargs = restart.call_args
        self.assertEqual(restart_args[0], pair["id"])
        self.assertEqual(restart_args[1]["id"], "arm-trace-B")
        self.assertEqual(restart_args[2:4], (
            "Build a hard project with Docker Compose",
            "轨迹首轮题面不一致，按数据库原题面重新运行",
        ))
        self.assertEqual(restart_kwargs, {
            "count_development_failure": False,
            "count_error_retry": False,
        })
        submit.assert_called_once_with(
            "monitor-arm-trace-B", self.service._monitor_arm,
            pair["id"], "arm-trace-B", "Build a hard project with Docker Compose",
        )
        submit_operation.assert_not_called()
        states = {row["arm"]: row["status"] for row in self.db.all(
            "SELECT arm,status FROM arm_runs WHERE pair_id=?", (pair["id"],)
        )}
        self.assertEqual(states, {"A": "completed", "B": "waiting_retry"})
        self.assertEqual(self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))["stage"], "development")
        self.assertEqual(self.db.one("SELECT status FROM gsb_reviews WHERE pair_id=?", (pair["id"],))["status"], "draft")

    def test_artifact_repair_suffixes_require_clean_pair_rerun(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,session_id,prompt_id,trace_path,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?,?)""",
                ("arm-pair-prompt-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 "session-" + arm, "prompt-" + arm, str(self.root / "traces" / arm),
                 arm.lower() * 40, stamp, stamp),
            )

        def restarted(_pair_id, run, _prompt, _error, **_kwargs):
            return {**run, "status": "developing"}

        with patch.object(self.service, "_inspect_trace", side_effect=[
                (Path("A.jsonl"), "2.1.269", []),
                (Path("B.jsonl"), "2.1.269", []),
             ]), \
             patch.object(self.service, "_trace_first_user_prompt", side_effect=[
                 "Build a hard project with Docker Compose\n[PAIRWISE_ARTIFACT_REPAIR]\nrepair A",
                 "Build a hard project with Docker Compose\n[PAIRWISE_ARTIFACT_REPAIR]\nrepair B",
             ]), \
             patch.object(self.service, "_restart_arm_from_baseline", side_effect=restarted) as restart, \
             patch.object(self.service, "_submit_monitor") as submit, \
             patch.object(self.service, "_submit_auto") as submit_operation:
            self.service._refresh_pair_after_arm(pair["id"])

        self.assertEqual(restart.call_count, 2)
        self.assertEqual(
            {call.args[1]["arm"] for call in restart.call_args_list},
            {"A", "B"},
        )
        for call in restart.call_args_list:
            self.assertEqual(call.args[2:4], (
                "Build a hard project with Docker Compose",
                "轨迹首轮题面不一致，按数据库原题面重新运行",
            ))
            self.assertEqual(call.kwargs, {
                "count_development_failure": False,
                "count_error_retry": False,
            })
        self.assertEqual(submit.call_count, 2)
        submit_operation.assert_not_called()
        self.assertEqual(
            {row["arm"]: row["status"] for row in self.db.all(
                "SELECT arm,status FROM arm_runs WHERE pair_id=?", (pair["id"],)
            )},
            {"A": "waiting_retry", "B": "waiting_retry"},
        )
        self.assertEqual(
            self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))["stage"],
            "development",
        )

    def test_paired_trace_prompt_match_only_normalizes_newline_encoding(self):
        prompt = "first line\n\nsecond line"
        self.assertTrue(self.service._paired_trace_prompts_match(
            prompt, "first line\r\n\r\nsecond line\r\n", "first line\r\rsecond line\r",
        ))
        self.assertFalse(self.service._paired_trace_prompts_match(
            prompt,
            prompt + "\n[PAIRWISE_ARTIFACT_REPAIR]\nrepair A",
            prompt + "\n[PAIRWISE_ARTIFACT_REPAIR]\nrepair B",
        ))
        self.assertTrue(self.service._paired_trace_prompts_match(
            prompt, "first line\nsecond line", "first line\nsecond line",
        ))

    def test_discarded_pair_with_matching_arm_prompts_is_restored_without_rerun(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """UPDATE pairs SET status='repair_pending',stage='prompt_mismatch_retry_pending',
               winner='',completed_at=NULL,error='false mismatch' WHERE id=?""",
            (pair["id"],),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,session_id,prompt_id,trace_path,commit_sha,
                   created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?,?)""",
                ("arm-discarded-match-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 "session-" + arm, "prompt-" + arm, str(self.root / (arm + ".jsonl")),
                 arm.lower() * 40, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,status,created_at,updated_at)
               VALUES(?,?,'A better','old result','draft',?,?)""",
            ("gsb-discarded-match", pair["id"], stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,remote_status,qc_summary,
               error,created_at,updated_at) VALUES(?,?,'blocked','DISCARDED',?,'false mismatch',?,?)""",
            ("delivery-discarded-match", pair["id"], "duplicate task", stamp, stamp),
        )
        self.db.audit("gsb.confirmed", "pair", pair["id"], {
            "verdict": "A better", "confirmed_by": "reviewer",
        })

        with patch.object(self.service, "_inspect_trace", side_effect=[
                (Path("A.jsonl"), "2.1.269", []),
                (Path("B.jsonl"), "2.1.269", []),
             ]), patch.object(
                 self.service, "_trace_first_user_prompt",
                 side_effect=["same canonical prompt", "same canonical prompt"],
             ):
            self.assertEqual(self.service._restore_discarded_false_prompt_mismatches(), 1)

        restored = self.db.one(
            "SELECT status,stage,winner,error,completed_at FROM pairs WHERE id=?", (pair["id"],),
        )
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["stage"], "completed")
        self.assertEqual(restored["winner"], "A better")
        self.assertFalse(restored["error"])
        self.assertTrue(restored["completed_at"])
        self.assertEqual(
            self.db.one("SELECT status,confirmed_by FROM gsb_reviews WHERE pair_id=?", (pair["id"],)),
            {"status": "confirmed", "confirmed_by": "reviewer"},
        )
        self.assertEqual(
            self.db.one("SELECT status,error FROM delivery_submissions WHERE pair_id=?", (pair["id"],)),
            {"status": "discarded", "error": "duplicate task"},
        )

    def test_completed_prompt_mismatch_is_queued_for_clean_pair_rerun(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """UPDATE pairs SET status='completed',stage='completed',winner='Same',completed_at=?
                 WHERE id=?""",
            (stamp, pair["id"]),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,session_id,prompt_id,trace_path,commit_sha,
                   created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?,?)""",
                ("arm-completed-mismatch-" + arm, pair["id"], arm, arm,
                 str(self.root / arm), "container-" + arm, "screen-" + arm,
                 "auto_model/urm", "image", "session-" + arm, "prompt-" + arm,
                 str(self.root / (arm + ".jsonl")), arm.lower() * 40, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,status,created_at,updated_at)
               VALUES(?,?, 'Same','old result','confirmed',?,?)""",
            ("gsb-completed-mismatch", pair["id"], stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,payload_sha256,created_at,updated_at)
               VALUES(?,?,'ready_to_submit','old-payload',?,?)""",
            ("delivery-completed-mismatch", pair["id"], stamp, stamp),
        )

        with patch.object(self.service, "_inspect_trace", side_effect=[
                (Path("A.jsonl"), "2.1.269", []),
                (Path("B.jsonl"), "2.1.269", []),
             ]), patch.object(self.service, "_trace_first_user_prompt", side_effect=[
                 "Build a hard project with Docker Compose",
                 "Build a hard project with Docker Compose\n[PAIRWISE_ARTIFACT_REPAIR]",
             ]):
            self.assertEqual(self.service._queue_completed_prompt_mismatch_pairs(), 1)

        self.assertEqual(
            self.db.one("SELECT status,stage,winner,completed_at FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "repair_pending", "stage": "prompt_mismatch_retry_pending",
             "winner": "", "completed_at": None},
        )
        self.assertEqual(
            self.db.one("SELECT status FROM gsb_reviews WHERE pair_id=?", (pair["id"],))["status"],
            "draft",
        )
        delivery = self.db.one(
            "SELECT status,payload_sha256 FROM delivery_submissions WHERE pair_id=?", (pair["id"],),
        )
        self.assertEqual(delivery, {"status": "blocked", "payload_sha256": ""})

    def test_user_cancelled_prompt_mismatch_is_not_queued_again(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """UPDATE pairs SET status='completed',stage='completed',winner='Same',completed_at=?
                 WHERE id=?""",
            (stamp, pair["id"]),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,session_id,prompt_id,trace_path,commit_sha,
                   created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?,?)""",
                ("arm-cancelled-mismatch-" + arm, pair["id"], arm, arm,
                 str(self.root / arm), "container-" + arm, "screen-" + arm,
                 "auto_model/urm", "image", "session-" + arm, "prompt-" + arm,
                 str(self.root / (arm + ".jsonl")), arm.lower() * 40, stamp, stamp),
            )
        self.db.audit(
            "pair.prompt_mismatch_retry_cancelled_by_user", "pair", pair["id"],
            {"reason": "user_requested_no_rerun"},
        )

        with patch.object(self.service, "_inspect_trace") as inspect:
            self.assertEqual(self.service._queue_completed_prompt_mismatch_pairs(), 0)

        inspect.assert_not_called()
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "completed", "stage": "completed"},
        )

    def test_user_cancelled_prompt_mismatch_is_not_requarantined_for_cleared_checks(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """UPDATE pairs SET status='completed',stage='completed',winner='A better',completed_at=?
                 WHERE id=?""",
            (stamp, pair["id"]),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-cancelled-check-" + arm, pair["id"], arm, arm,
                 str(self.root / arm), "container-" + arm, "screen-" + arm,
                 "auto_model/urm", "image", arm.lower() * 40, stamp, stamp),
            )
        self.db.audit(
            "pair.prompt_mismatch_retry_cancelled_by_user", "pair", pair["id"],
            {"reason": "user_requested_no_rerun"},
        )

        self.service._quarantine_invalid_completed_pairs()

        self.assertEqual(
            self.db.one("SELECT status,stage,winner FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "completed", "stage": "completed", "winner": "A better"},
        )

    def test_archived_completed_trace_is_restored_before_prompt_validation(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        arm_id = pair["id"] + "-a"
        session_id = "session-archived-A"
        missing = self.config.data_dir / "claude-runs" / arm_id / "traces"
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,session_id,prompt_id,trace_path,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?,?)""",
            (arm_id, pair["id"], "A", "A", str(self.root / "A"), "container-A", "screen-A",
             "auto_model/urm", "image", session_id, "prompt-A", str(missing), "a" * 40,
             stamp, stamp),
        )
        archived = (
            self.config.data_dir / "claude-attempts" /
            (arm_id + "-attempt-1-archive") / "traces" / "-workspace"
        )
        archived.mkdir(parents=True)
        (archived / (session_id + ".jsonl")).write_text(
            json.dumps({
                "type": "user", "version": "2.1.269", "sessionId": session_id,
                "message": {"role": "user", "content": "Build a hard project with Docker Compose"},
            }) + "\n",
            encoding="utf-8",
        )
        arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,))

        trace, version, issues = self.service._inspect_trace(
            arm, "Build a hard project with Docker Compose",
        )

        self.assertEqual(issues, [])
        self.assertEqual(version, "2.1.269")
        self.assertTrue(trace.is_file())
        self.assertTrue(str(trace).startswith(str(missing.resolve())))
        self.assertEqual(
            self.db.one("SELECT trace_path FROM arm_runs WHERE id=?", (arm_id,))["trace_path"],
            str(missing),
        )
        self.assertIsNotNone(self.db.one(
            "SELECT id FROM audit_events WHERE entity_id=? AND event_type='claude.archived_trace_restored'",
            (arm_id,),
        ))

    def test_original_trace_prompt_remains_valid_with_stale_artifact_repair_file(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        arm_id = pair["id"] + "-a"
        session_id = "session-original-before-repair"
        trace_root = self.config.data_dir / "claude-runs" / arm_id / "traces"
        trace_file = trace_root / "-workspace" / (session_id + ".jsonl")
        trace_file.parent.mkdir(parents=True)
        trace_file.write_text(
            json.dumps({
                "type": "user", "version": "2.1.269", "sessionId": session_id,
                "message": {"role": "user", "content": "Build a hard project with Docker Compose"},
            }) + "\n",
            encoding="utf-8",
        )
        runtime = self.service.claude.runtime_dir / arm_id
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "prompt.txt").write_text(
            "Build a hard project with Docker Compose\n\n"
            "[PAIRWISE_ARTIFACT_REPAIR]\nrepair failed Docker verification",
            encoding="utf-8",
        )
        arm = {
            "id": arm_id, "arm": "A", "session_id": session_id,
            "trace_path": str(trace_root), "updated_at": stamp,
        }

        trace, version, issues = self.service._inspect_trace(
            arm, "Build a hard project with Docker Compose",
        )

        self.assertEqual(trace, trace_file.resolve())
        self.assertEqual(version, "2.1.269")
        self.assertEqual(issues, [])

    def test_missing_trace_is_not_reported_as_prompt_mismatch(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        arm = {
            "id": pair["id"] + "-a", "pair_id": pair["id"], "arm": "A",
            "status": "completed", "attempt_no": 1,
        }
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?)""",
            (arm["id"], pair["id"], "A", "A", str(self.root / "A"), "container-A", "screen-A",
             "auto_model/urm", "image", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,created_at,updated_at)
               VALUES(?,?,'needs_review',?,?)""",
            ("delivery-trace-label", pair["id"], stamp, stamp),
        )
        restarted = {**arm, "status": "developing"}
        with patch.object(self.service, "_restart_arm_from_baseline", return_value=restarted), \
             patch.object(self.service, "_submit_monitor"):
            self.service._restart_trace_invalid_arms(
                pair["id"], [arm], "Build a hard project with Docker Compose", ["A 轨迹目录无效"],
            )

        delivery = self.db.one("SELECT status,error FROM delivery_submissions WHERE pair_id=?", (pair["id"],))
        self.assertEqual(delivery["status"], "needs_review")
        self.assertIn("轨迹文件校验未通过", delivery["error"])
        self.assertNotIn("题面不一致", delivery["error"])

    def test_trace_repair_does_not_restart_a_replaced_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        arm = {
            "id": pair["id"] + "-a", "pair_id": pair["id"], "arm": "A",
            "status": "completed", "attempt_no": 3,
        }
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?)""",
            (arm["id"], pair["id"], "A", "A", str(self.root / "A"), "container-A", "screen-A",
             "auto_model/urm", "image", stamp, stamp),
        )
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='replaced',error='已自动换题' WHERE id=?",
            (pair["id"],),
        )
        with patch.object(self.service, "_restart_arm_from_baseline") as restart, \
             patch.object(self.service, "_submit_monitor") as monitor:
            result = self.service._restart_trace_invalid_arms(
                pair["id"], [arm], "Build a hard project with Docker Compose", ["A 轨迹目录无效"],
            )

        self.assertEqual(result["skipped"], "terminal_pair")
        restart.assert_not_called()
        monitor.assert_not_called()
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "failed", "stage": "replaced"},
        )

    def test_trace_repair_does_not_restart_user_cancelled_prompt_retry(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        arm = {
            "id": pair["id"] + "-a", "pair_id": pair["id"], "arm": "A",
            "status": "completed", "attempt_no": 2,
        }
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?)""",
            (arm["id"], pair["id"], "A", "A", str(self.root / "A"), "container-A", "screen-A",
             "auto_model/urm", "image", stamp, stamp),
        )
        self.db.execute(
            "UPDATE pairs SET status='repair_pending',stage='prompt_mismatch_retry_pending' WHERE id=?",
            (pair["id"],),
        )
        self.db.audit(
            "pair.prompt_mismatch_retry_cancelled_by_user", "pair", pair["id"],
            {"reason": "user_requested_no_rerun"},
        )
        with patch.object(self.service, "_restart_arm_from_baseline") as restart, \
             patch.object(self.service, "_submit_monitor") as monitor:
            result = self.service._restart_trace_invalid_arms(
                pair["id"], [arm], "Build a hard project", ["A/B 轨迹里的完整首轮 User Prompt 不一致"],
            )
            repeated = self.service._restart_trace_invalid_arms(
                pair["id"], [arm], "Build a hard project", ["A/B 轨迹里的完整首轮 User Prompt 不一致"],
            )

        self.assertEqual(result["skipped"], "terminal_pair")
        self.assertEqual(repeated["skipped"], "terminal_pair")
        restart.assert_not_called()
        monitor.assert_not_called()
        self.assertEqual(
            self.db.one(
                """SELECT COUNT(*) count FROM audit_events
                     WHERE event_type='claude.trace_repair_skipped_terminal_pair'
                       AND entity_type='pair' AND entity_id=?""",
                (pair["id"],),
            )["count"],
            1,
        )

    def test_trace_repair_waits_silently_for_configured_terminal_capacity(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        arm = {
            "id": pair["id"] + "-a", "pair_id": pair["id"], "arm": "A",
            "status": "completed", "attempt_no": 1,
        }
        with patch.object(self.service, "_development_arm_limit", return_value=4), \
             patch.object(self.service, "_active_development_arm_count", return_value=4), \
             patch.object(self.service, "_restart_arm_from_baseline") as restart, \
             patch.object(self.service, "_submit_monitor") as monitor:
            result = self.service._restart_trace_invalid_arms(
                pair["id"], [arm], "Build a hard project", ["A 轨迹目录无效"],
            )

        self.assertEqual(result["deferred"], "terminal_capacity")
        self.assertEqual(result["terminalLimit"], 4)
        restart.assert_not_called()
        monitor.assert_not_called()

    def test_retired_pair_delivery_is_discarded_instead_of_waiting_for_repair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,error,created_at,updated_at)
               VALUES(?,?,'needs_review','轨迹题面不一致，等待单侧重跑和重新验收',?,?)""",
            ("delivery-retired", pair["id"], stamp, stamp),
        )
        with patch.object(self.service, "_submit") as submit:
            self.service._retire_pair_and_schedule_replacement(
                pair["id"], pair["id"] + "-b", "首轮超时且无代码产出",
            )

        delivery = self.db.one("SELECT status,error FROM delivery_submissions WHERE pair_id=?", (pair["id"],))
        self.assertEqual(delivery["status"], "discarded")
        self.assertIn("已停止交付", delivery["error"])
        submit.assert_called_once_with(
            "replace-task-" + pair["id"], self.service._start_replacement_pair, pair["id"],
        )

    def test_retirement_queues_failed_side_when_peer_artifact_passed(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-retire-A", pair["id"], "A", "A", str(self.root / "A"), "container-A",
             "screen-A", "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,attempt_no,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'failed',3,'开发失败',?,?)""",
            ("arm-retire-B", pair["id"], "B", "B", str(self.root / "B"), "container-B",
             "screen-B", "auto_model/urm", "image", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?, 'passed',?,?)""",
            ("check-retire-A", pair["id"], "A", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,error,created_at,updated_at)
               VALUES(?,?,'not_submitted','',?,?)""",
            ("delivery-retire-pass", pair["id"], stamp, stamp),
        )

        with patch.object(self.service, "_submit") as submit:
            self.service._retire_pair_and_schedule_replacement(
                pair["id"], "arm-retire-B", "开发连续三次没有完成",
            )

        current = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current["status"], "repair_pending")
        self.assertEqual(current["stage"], "single_arm_repair_pending")
        self.assertIn("保留该侧", current["error"])
        preserved = self.db.one(
            "SELECT status,commit_sha FROM arm_runs WHERE id='arm-retire-A'"
        )
        self.assertEqual(preserved, {"status": "completed", "commit_sha": "a" * 40})
        delivery = self.db.one(
            "SELECT status,error FROM delivery_submissions WHERE pair_id=?", (pair["id"],),
        )
        self.assertEqual(delivery["status"], "needs_review")
        self.assertIn("等待空位仅返修 B", delivery["error"])
        submit.assert_not_called()
        event = self.db.one(
            """SELECT detail_json FROM audit_events
               WHERE event_type='claude.single_arm_repair_queued' AND entity_id='arm-retire-B'"""
        )
        self.assertEqual(json.loads(event["detail_json"])["preservedArm"], "A")

    def test_retirement_replaces_pair_after_single_arm_repair_window_is_exhausted(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-exhausted-A", pair["id"], "A", "A", str(self.root / "A"), "container-A",
             "screen-A", "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,attempt_no,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'failed',3,'返修仍失败',?,?)""",
            ("arm-exhausted-B", pair["id"], "B", "B", str(self.root / "B"), "container-B",
             "screen-B", "auto_model/urm", "image", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?, 'passed',?,?)""",
            ("check-exhausted-A", pair["id"], "A", "a" * 40, stamp, stamp),
        )
        self.db.audit("claude.single_arm_repair_resumed", "arm_run", "arm-exhausted-B", {
            "pair_id": pair["id"], "arm": "B",
        })

        with patch.object(self.service, "_submit") as submit:
            self.service._retire_pair_and_schedule_replacement(
                pair["id"], "arm-exhausted-B", "返修窗口连续三次仍未完成",
            )

        current = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "failed", "stage": "task_replacement"})
        submit.assert_called_once_with(
            "replace-task-" + pair["id"], self.service._start_replacement_pair, pair["id"],
        )

    def test_retirement_does_not_grant_fresh_window_after_shared_budget_exhausted(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET development_failure_count=2 WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-budget-A", pair["id"], "A", "A", str(self.root / "A"), "container-A",
             "screen-A", "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'failed','开发失败',?,?)""",
            ("arm-budget-B", pair["id"], "B", "B", str(self.root / "B"), "container-B",
             "screen-B", "auto_model/urm", "image", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?, 'passed',?,?)""",
            ("check-budget-A", pair["id"], "A", "a" * 40, stamp, stamp),
        )

        with patch.object(self.service, "_submit") as submit:
            self.service._retire_pair_and_schedule_replacement(
                pair["id"], "arm-budget-B", "项目累计两次开发失败",
            )

        current = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "failed", "stage": "task_replacement"})
        submit.assert_called_once_with(
            "replace-task-" + pair["id"], self.service._start_replacement_pair, pair["id"],
        )

    def test_gsb_process_evidence_excludes_discarded_arm_sessions(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm, sent_at in (("A", "2026-09-17T10:00:00+00:00"), ("B", "2026-09-17T12:00:00+00:00")):
            arm_id = pair["id"] + "-" + arm.lower()
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,prompt_sent_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
                (arm_id, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sent_at, stamp, stamp),
            )
        events = (
            (pair["id"] + "-a", "A-old", "2026-09-17T09:00:00+00:00"),
            (pair["id"] + "-a", "A-current", "2026-09-17T10:30:00+00:00"),
            (pair["id"] + "-b", "B-old", "2026-09-17T11:00:00+00:00"),
            (pair["id"] + "-b", "B-current", "2026-09-17T12:30:00+00:00"),
            (pair["id"], "pair-old", "2026-09-17T11:30:00+00:00"),
            (pair["id"], "pair-current", "2026-09-17T12:30:00+00:00"),
        )
        for entity_id, marker, created_at in events:
            self.db.execute(
                """INSERT INTO audit_events(event_type,entity_type,entity_id,detail_json,created_at)
                   VALUES('claude.test','arm_run',?,?,?)""",
                (entity_id, json.dumps({"marker": marker}), created_at),
            )
        markers = [json.loads(row["detail_json"])["marker"] for row in self.service._current_process_events(pair["id"])]
        self.assertEqual(markers, ["A-current", "B-current", "pair-current"])

    def test_gsb_process_events_hide_prior_verdict(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO audit_events(event_type,entity_type,entity_id,detail_json,created_at)
               VALUES('recording.unsafe_control_invalidated','pair',?,?,?)""",
            (pair["id"], json.dumps({
                "arm": "B", "oldVerdict": "B better", "oldReason": "old judgment",
                "nested": {"winner": "B", "safe": "new recording needed"},
            }), stamp),
        )
        events = self.service._current_process_events(pair["id"])
        detail = json.loads(events[-1]["detail_json"])
        self.assertEqual(detail, {"arm": "B", "nested": {"safe": "new recording needed"}})

    def test_gsb_trace_evidence_uses_real_jsonl_steps_and_visible_tool_results(self):
        trace_dir = self.root / "trace-evidence"
        trace_dir.mkdir()
        rows = [
            {"type": "system", "message": {"content": "start"}},
            {"message": {"content": [{
                "type": "tool_use", "name": "Write",
                "input": {"file_path": "/workspace/app/dating.go", "content": "package app"},
            }]}},
            {"message": {"content": [{
                "type": "tool_result", "content": "File written successfully",
            }]}},
            {"message": {"content": [{
                "type": "tool_use", "name": "Bash",
                "input": {"command": "go test ./..."},
            }]}},
            {"message": {"content": [{
                "type": "tool_result", "is_error": True,
                "content": "use of internal package not allowed",
            }]}},
        ]
        (trace_dir / "session.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8",
        )
        evidence = self.service._trace_action_evidence({"trace_path": str(trace_dir)})
        self.assertTrue(evidence["available"])
        self.assertEqual([event["step"] for event in evidence["events"]], [2, 3, 4, 5])
        self.assertEqual(evidence["events"][0]["tool"], "Write")
        self.assertIn("dating.go", evidence["events"][0]["detail"])
        self.assertEqual(evidence["events"][2]["detail"], "go test ./...")
        self.assertTrue(evidence["events"][3]["isError"])

    def test_review_and_evidence_pages_show_and_filter_submission_status(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                (pair["id"] + "-" + arm.lower(), pair["id"], arm, arm,
                 str(self.root / arm), "container-" + arm, "screen-" + arm,
                 "auto_model/urm", "image", arm.lower() * 40, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,status,confirmed_by,created_at,updated_at)
               VALUES(?,?,'A better','confirmed','刘昱',?,?)""",
            ("gsb-submission-filter", pair["id"], stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,remote_id,remote_status,created_at,updated_at)
               VALUES(?,?,'qc_passed','solo-qa-42','QC_PASSED',?,?)""",
            ("delivery-submission-filter", pair["id"], stamp, stamp),
        )
        self.db.execute(
            "UPDATE pairs SET model_scheme='cross_model',model_a=?,model_b=? WHERE id=?",
            ("auto_model/urm", "ark/urm-03", pair["id"]),
        )
        handler = object.__new__(Handler)
        handler.server = MagicMock(db=self.db)

        pair_row = handler._pairs_page({"q": [pair["id"]]})["items"][0]
        self.assertEqual(pair_row["model_scheme"], "cross_model")
        self.assertEqual(pair_row["model_b"], "ark/urm-03")

        evidence_page = handler._evidence_page({
            "submission_status": ["qc_passed"], "q": ["solo-qa-42"],
        })
        self.assertEqual(evidence_page["total"], 2)
        self.assertEqual({row["submission_status"] for row in evidence_page["items"]}, {"qc_passed"})
        self.assertEqual({row["remote_id"] for row in evidence_page["items"]}, {"solo-qa-42"})
        self.assertEqual({row["model_scheme"] for row in evidence_page["items"]}, {"cross_model"})

        reviews_page = handler._reviews_page({
            "submission_status": ["qc_passed"], "q": ["solo-qa-42"],
        })
        self.assertEqual(reviews_page["total"], 1)
        self.assertEqual(reviews_page["items"][0]["submission_status"], "qc_passed")
        self.assertEqual(reviews_page["items"][0]["model_b"], "ark/urm-03")
        self.assertEqual(handler._deliveries_page({"q": [pair["id"]]})["items"][0]["model_scheme"], "cross_model")
        self.assertEqual(
            handler._reviews_page({"submission_status": ["needs_fix"]})["total"], 0,
        )

    def test_evidence_page_filters_arms_that_have_ever_been_manually_rerecorded(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-manual-filter-" + arm.lower(), pair["id"], arm, arm,
                 str(self.root / arm), "container-" + arm, "screen-" + arm,
                 "auto_model/urm", "image", arm.lower() * 40, stamp, stamp),
            )
        for attempt_id, arm, mode, status in (
            ("attempt-manual-a-1", "A", "manual", "failed"),
            ("attempt-manual-a-2", "A", "manual", "passed"),
            ("attempt-auto-b", "B", "auto", "passed"),
        ):
            self.db.execute(
                """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,interaction_mode,
                   status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (attempt_id, pair["id"], arm, arm.lower() * 40,
                 str(self.root / (attempt_id + ".mp4")), mode, status, stamp, stamp),
            )
        handler = object.__new__(Handler)
        handler.server = MagicMock(db=self.db)

        manual = handler._evidence_page({"manual_rerecorded": ["yes"]})
        self.assertEqual(manual["total"], 1)
        self.assertEqual(manual["items"][0]["arm"], "A")
        self.assertEqual(manual["items"][0]["manual_rerecorded"], 1)

        never_manual = handler._evidence_page({"manual_rerecorded": ["no"]})
        self.assertEqual(never_manual["total"], 1)
        self.assertEqual(never_manual["items"][0]["arm"], "B")
        self.assertEqual(never_manual["items"][0]["manual_rerecorded"], 0)

        for arm, attempt_id in (("A", "attempt-manual-a-2"), ("B", "attempt-auto-b")):
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,sha256,duration_seconds,
                   commit_sha,commit_match,attempt_id,steps_json,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,5.12,?,1,?,?,'passed',?,?)""",
                ("recording-similar-" + arm, pair["id"], arm,
                 str(self.root / (arm + ".mp4")), arm.lower() * 64, arm.lower() * 40,
                 attempt_id, json.dumps([{"method": "get", "path": "/v1/profile", "body": None}]),
                 stamp, stamp),
            )
        similar = handler._evidence_page({"high_similarity": ["yes"]})
        self.assertEqual(similar["total"], 2)
        self.assertTrue(all(row["high_similarity"] for row in similar["items"]))
        self.assertEqual(handler._evidence_page({"high_similarity": ["yes"], "arm": ["B"]})["total"], 1)

    def test_evidence_page_filters_recordings_strictly_over_50_seconds(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm, duration in (("A", 50.0), ("B", 50.1)):
            commit = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-duration-" + arm.lower(), pair["id"], arm, arm,
                 str(self.root / arm), "container-" + arm, "screen-" + arm,
                 "auto_model/urm", "image", commit, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,sha256,duration_seconds,
                   commit_sha,commit_match,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,1,'passed',?,?)""",
                ("recording-duration-" + arm.lower(), pair["id"], arm,
                 str(self.root / (arm + ".mp4")), arm.lower() * 64, duration,
                 commit, stamp, stamp),
            )
        handler = object.__new__(Handler)
        handler.server = MagicMock(db=self.db)

        query = {"recording_duration": ["over_50"]}
        result = handler._evidence_page(query)
        self.assertTrue(result["recording_duration_filter_applied"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["arm"], "B")
        self.assertEqual(handler._evidence_page({**query, "arm": ["A"]})["total"], 0)
        self.assertEqual(handler._evidence_page({"recording_duration": ["unknown"]})["total"], 2)

    def test_gsb_prompt_requires_plain_language_trace_and_reproduced_bug_evidence(self):
        prompt = gsb_prompt("开发送检单", "A evidence", "B evidence", "process events")
        self.assertIn("traceEvidence", prompt)
        self.assertIn("不要为了显得简短而删掉能支撑结论的证据", prompt)
        self.assertIn("公开理由不得出现“第175步”", prompt)
        self.assertIn("step 只供内部找到证据", prompt)
        self.assertIn("不额外追求最短", prompt)
        self.assertIn("没有实际跑接口流程", prompt)
        self.assertIn("不必列出每项的原始数字", prompt)
        self.assertIn("process events", prompt)

    def test_gsb_recheck_prioritizes_logic_and_preserves_useful_evidence(self):
        independent_prompt = gsb_independent_recheck_prompt("开发送检单", "evidence")
        self.assertNotIn("A better", independent_prompt)
        self.assertIn("你看不到原来的 GSB 结论和理由", independent_prompt)
        self.assertIn("绝不能当成已经通过", independent_prompt)
        self.assertIn("不能仅因为暂时没有发现差异就判 Same", independent_prompt)
        prompt = gsb_recheck_prompt(
            "开发送检单", "A better", "A reason", "B reason",
            "B better", "blind A", "blind B", "evidence",
        )
        self.assertIn("复检首先检查首次生成的 GSB 逻辑是否正确", prompt)
        self.assertIn("独立盲评结论：B better", prompt)
        self.assertIn("不要因为理由较长或证据较多就要求精简", prompt)
        self.assertIn("必须保留原评价中所有会影响结论的有效证据", prompt)
        self.assertIn("建议理由中不得出现第几步", prompt)
        self.assertIn("不得仅因篇幅、数字数量或代码细节较多判为需要修改", prompt)
        self.assertIn("精确值仍在内部证据", prompt)

    def test_gsb_style_check_targets_mechanical_numbers_without_rejecting_real_evidence(self):
        issues = self.service._gsb_conversational_issues(
            "rehearsal.spec.ts 第15、16、21、22项通过，83个单测、23个端到端测试和38秒录像也通过，未见已发生的功能缺陷。",
            "B 在第43步运行接口测试，先因测试库冲突失败，换成独立数据库后通过。",
        )
        self.assertTrue(any("机械罗列测试编号" in issue for issue in issues))
        self.assertTrue(any("堆叠测试数量" in issue for issue in issues))
        self.assertTrue(any("不得引用录像" in issue for issue in issues))
        self.assertTrue(any("生硬的无缺陷套话" in issue for issue in issues))
        self.assertTrue(any(issue.startswith("B ") and "轨迹步骤号" in issue for issue in issues))

    def test_gsb_style_summarizes_successful_metric_dump_but_keeps_decisive_numbers(self):
        verbose = "A 实际调用 API 返回高扇出 10 条正边、总量 160，裁决向量为 (3,3,0,0)，范围与到达值也正确。"
        natural = "A 实际调用 API 后，高扇出场景返回了预期的正边，边的总量、裁决向量、范围和到达值也都核对正确。"
        decisive = "B 调用 API 后得到 9007199254740992，但正确值应为 9007199254740993，差一导致唯一性判错。"
        self.assertTrue(any("原始数字" in issue for issue in self.service._gsb_conversational_issues(verbose, natural)))
        self.assertFalse(self.service._gsb_conversational_issues(natural, decisive))

    def test_gsb_cleanup_removes_trace_step_numbers_without_dropping_evidence(self):
        source = (
            "B 中途第439步的回溯属性错误已在第448步修正，"
            "第569步及 Docker 又跑通重复码分叉，第646步测试全过。"
        )
        cleaned = self.service._clean_gsb_part(source, 300)
        self.assertNotRegex(cleaned, r"第\d+步")
        self.assertIn("回溯属性错误后来已修正", cleaned)
        self.assertIn("Docker 又跑通重复码分叉", cleaned)
        self.assertIn("测试全过", cleaned)

    def test_completed_pair_with_recorded_artifact_failure_is_preserved(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm, status in (("A", "observed_failed"), ("B", "passed")):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-quarantine-" + arm, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                ("check-quarantine-" + arm, pair["id"], arm, sha, status, stamp, stamp),
            )
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        self.service._quarantine_invalid_completed_pairs()
        current = self.db.one("SELECT status,stage,completed_at FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "completed", "stage": "completed", "completed_at": stamp})

    def test_qc_passed_pair_is_not_retroactively_quarantined(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm, status in (("A", "failed"), ("B", "passed")):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-qc-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                ("check-qc-" + arm, pair["id"], arm, sha, status, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,status,commit_sha,commit_match,
                   created_at,updated_at) VALUES(?,?,?,?, 'passed',?,1,?,?)""",
                ("recording-qc-" + arm, pair["id"], arm, str(self.root / (arm + ".mp4")),
                 sha, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,remote_id,remote_status,
               created_at,updated_at) VALUES(?,?,'qc_passed','1009','QC_PASSED',?,?)""",
            ("delivery-qc", pair["id"], stamp, stamp),
        )
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=? WHERE id=?",
            (stamp, pair["id"]),
        )

        self.service._quarantine_invalid_completed_pairs()

        current = self.db.one("SELECT status,stage,completed_at FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "completed", "stage": "completed", "completed_at": stamp})
        self.assertEqual(
            self.db.one("SELECT COUNT(*) count FROM recordings WHERE pair_id=?", (pair["id"],))["count"],
            2,
        )

    def test_completed_pair_missing_current_artifact_check_is_quarantined(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-missing-check-" + arm, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            if arm == "B":
                self.db.execute(
                    """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    ("check-missing-" + arm, pair["id"], arm, sha, "passed", stamp, stamp),
                )
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        self.service._quarantine_invalid_completed_pairs()
        current = self.db.one("SELECT status,stage,completed_at FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "failed", "stage": "artifact_failed", "completed_at": None})

    def test_completed_pair_with_recorded_artifact_failure_survives_restart_check(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm, status in (("A", "observed_failed"), ("B", "passed")):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-observed-complete-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                ("check-observed-complete-" + arm, pair["id"], arm, sha, status, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,commit_match,review_status,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,1,'confirmed','passed',?,?)""",
                ("rec-observed-complete-" + arm, pair["id"], arm,
                 str(self.root / (arm + ".mp4")), sha, stamp, stamp),
            )
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        self.service._quarantine_invalid_completed_pairs()
        current = self.db.one("SELECT status,stage,completed_at FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "completed", "stage": "completed", "completed_at": stamp})
        self.assertEqual(
            int((self.db.one("SELECT COUNT(*) count FROM recordings WHERE pair_id=?", (pair["id"],)) or {}).get("count") or 0),
            2,
        )

    def test_api_error_is_kept_as_evidence_and_later_completion_is_accepted(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-api-error", "container_name": "container-api-error"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "isApiErrorMessage": True,
             "message": {"content": [{"type": "text", "text": "API Error: 504 Gateway Timeout"}]}},
            {"type": "assistant", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "Finished after internal retry"}],
            }},
            {"type": "system", "subtype": "turn_duration"},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertTrue(state["complete"])
        self.assertTrue(state["prompt_matches"])
        self.assertEqual(state["result"], "Finished after internal retry")
        self.assertIn("504", state["api_error"])
        self.assertFalse(hasattr(self.service.claude, "send_continue"))

    def test_native_turn_end_accepts_visible_progress_without_stop_reason(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-native-turn-end", "container_name": "container-native-turn-end"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "Implemented the requested workflow and ran verification."}],
            }},
            {"type": "system", "subtype": "turn_duration"},
            {"type": "last-prompt"},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertTrue(state["complete"])
        self.assertEqual(state["completion_mode"], "native_turn_end")
        self.assertIn("Implemented", state["result"])

    def test_tool_use_progress_text_is_not_treated_as_completion(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-tool-progress", "container_name": "container-tool-progress"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "message": {
                "stop_reason": "tool_use",
                "content": [{"type": "text", "text": "Now let me inspect the remaining files."}],
            }},
            {"type": "assistant", "message": {
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "app.py"}}],
            }},
            {"type": "last-prompt"},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertFalse(state["complete"])
        self.assertEqual(state["result"], "Now let me inspect the remaining files.")

    def test_task_retired_by_false_completion_is_restored_to_pool(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        baseline = "b" * 40
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='replaced',baseline_sha=? WHERE id=?",
            (baseline, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'failed',?,?)""",
            ("arm-false-complete", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", stamp, stamp),
        )
        self.db.audit("claude.arm_completed", "arm_run", "arm-false-complete", {
            "arm": "A", "commit_sha": baseline, "checkpointedDelivery": True,
        })
        self.service._restore_false_completed_tasks()
        task = self.db.one("SELECT status,used_at FROM tasks WHERE id='task-1'")
        self.assertEqual(task, {"status": "ready", "used_at": None})

    def test_completed_mismatched_prompt_is_visible_to_monitor_for_targeted_rerun(self):
        prompt = "Build the requested project\n\nKeep every boundary condition."
        observed = "Keep every boundary condition."
        arm = {"id": "arm-mismatched-prompt", "container_name": "container-mismatched-prompt"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": observed}},
            {"type": "assistant", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "Finished"}],
            }},
            {"type": "system", "subtype": "turn_duration"},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertTrue(state["complete"])
        self.assertFalse(state["prompt_matches"])
        self.assertEqual(state["observed_prompt"], observed)

    def test_prompt_is_sent_as_one_bracketed_paste(self):
        arm = {"id": "arm-paste", "screen_name": "screen-paste"}
        (self.service.claude.runtime_dir / arm["id"]).mkdir(parents=True)
        result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch.object(self.service.claude, "_screen_running", return_value=True), \
             patch("pairwise_console.claude_runner.run_command", return_value=result) as command, \
             patch.object(self.service.claude, "trace_state", return_value={
                 "path": "trace.jsonl", "prompt_matches": True,
                 "session_id": "session-1", "prompt_id": "prompt-1",
             }), \
             patch("pairwise_console.claude_runner.time.sleep"):
            self.service.claude.send_prompt(arm, "第一段\n\n第二段")
        calls = [call.args[0] for call in command.call_args_list]
        self.assertEqual(calls[0][-1], "\x1b[200~")
        self.assertEqual(calls[-2][-1], "\x1b[201~")
        self.assertEqual(calls[-1][-1], "\r")
        self.assertEqual("".join(call[-1] for call in calls[1:-2]), "第一段\n\n第二段")
        self.assertTrue(all(call[1] == "-U" for call in calls))

    def test_incomplete_prompt_mismatch_is_restarted_without_counting_as_followup(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
            ("arm-live-fragment", pair["id"], "A", "A", str(self.root / "A"),
             "container-live-fragment", "screen-live-fragment", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        state = {
            "complete": False, "result": "", "api_error": "", "path": "trace.jsonl",
            "prompt_matches": False, "observed_prompt": "从空仓库�",
            "followup_detected": True, "followup_text": "剩余题面",
            "session_id": "session-fragment", "prompt_id": "prompt-fragment",
        }
        repaired = {"pairId": pair["id"], "restarted": ["A"]}
        with patch.object(self.service.claude, "trace_state", return_value=state), \
             patch.object(self.service, "_restart_trace_invalid_arms", return_value=repaired) as restart, \
             patch.object(self.service, "_handle_attempt_failure") as failure:
            result = self.service._monitor_arm(
                pair["id"], "arm-live-fragment", "Build a hard project with Docker Compose",
            )
        self.assertEqual(result, repaired)
        restart.assert_called_once()
        failure.assert_not_called()

    def test_monitor_routes_completed_prompt_mismatch_to_non_counting_trace_repair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
            ("arm-live-mismatch", pair["id"], "A", "A", str(self.root / "A"),
             "container-live-mismatch", "screen-live-mismatch", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        state = {
            "complete": True, "result": "Finished", "api_error": "",
            "prompt_matches": False, "observed_prompt": "truncated prompt",
            "session_id": "session-mismatch", "prompt_id": "prompt-mismatch",
        }
        repaired = {"pairId": pair["id"], "restarted": ["A"]}
        with patch.object(self.service.claude, "trace_state", return_value=state), \
             patch.object(self.service, "_restart_trace_invalid_arms", return_value=repaired) as restart, \
             patch.object(self.service.claude, "export_and_stop") as export:
            result = self.service._monitor_arm(
                pair["id"], "arm-live-mismatch", "Build a hard project with Docker Compose",
            )
        self.assertEqual(result, repaired)
        restart.assert_called_once()
        export.assert_not_called()
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id='arm-live-mismatch' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(event["event_type"], "claude.live_prompt_mismatch")

    def test_terminal_api_error_after_visible_progress_keeps_the_session_deliverable(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-api-turn-end", "container_name": "container-api-turn-end"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "Implemented the core flow and verified the main path."}],
            }},
            {"type": "assistant", "isApiErrorMessage": True, "message": {
                "stop_reason": "stop_sequence",
                "content": [{"type": "text", "text": "API Error: 504 Gateway Timeout"}],
            }},
            {"type": "system", "subtype": "turn_duration"},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertTrue(state["complete"])
        self.assertEqual(state["completion_mode"], "native_turn_end")
        self.assertIn("504", state["api_error"])
        self.assertIn("Implemented", state["result"])

    def test_api_error_bookkeeping_does_not_advance_effective_progress(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-api-progress", "container_name": "container-api-progress"}
        base = datetime.now(timezone.utc) - timedelta(minutes=25)
        events = [
            {"type": "user", "timestamp": base.isoformat(), "promptId": "prompt-1",
             "message": {"content": prompt}},
            {"type": "assistant", "timestamp": (base + timedelta(minutes=1)).isoformat(),
             "message": {"stop_reason": "tool_use", "content": [
                 {"type": "tool_use", "name": "Write", "input": {"file_path": "src/app.ts"}},
             ]}},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            initial = self.service.claude.trace_state(arm, prompt)
            events.extend([
                {"type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
                 "isApiErrorMessage": True, "message": {
                     "stop_reason": "stop_sequence",
                     "content": [{"type": "text", "text": "API Error: 504 Gateway Timeout"}],
                 }},
                {"type": "system", "subtype": "turn_duration",
                 "timestamp": datetime.now(timezone.utc).isoformat()},
            ])
            after_error = self.service.claude.trace_state(arm, prompt)
            events.append({
                "type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {"stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}},
                ]},
            })
            after_real_work = self.service.claude.trace_state(arm, prompt)

        self.assertEqual(initial["progress_token"], after_error["progress_token"])
        self.assertEqual(initial["last_tool_activity_at"], after_error["last_tool_activity_at"])
        self.assertGreater(after_error["effective_progress_age_seconds"], 20 * 60)
        self.assertNotEqual(after_error["progress_token"], after_real_work["progress_token"])
        self.assertNotEqual(
            after_error["last_tool_activity_at"], after_real_work["last_tool_activity_at"],
        )

    def test_terminal_429_without_final_answer_is_marked_as_ended(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-terminal-429", "container_name": "container-terminal-429"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "message": {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "I will inspect the repository."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
            }},
            {"type": "assistant", "isApiErrorMessage": True,
             "timestamp": "2026-09-27T12:01:00.000Z", "message": {
                "stop_reason": "stop_sequence",
                "content": [{"type": "text", "text": "API Error: Request rejected (429) · Rate limit"}],
            }},
            {"type": "system", "subtype": "turn_duration"},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertFalse(state["complete"])
        self.assertTrue(state["api_error_turn_ended"])
        self.assertIn("429", state["api_error"])
        self.assertEqual(state["api_error_at"], "2026-09-27T12:01:00.000Z")

    def test_native_turn_ending_after_tools_without_final_answer_is_terminal(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-ended-without-final", "container_name": "container-ended-without-final"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "message": {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "I will write the implementation."},
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "main.go"}},
                ],
            }},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}},
            {"type": "system", "subtype": "turn_duration"},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertFalse(state["complete"])
        self.assertFalse(state["api_error_turn_ended"])
        self.assertTrue(state["turn_ended_without_final"])

    def test_automatic_companion_after_turn_end_is_not_marked_terminal_while_running(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-companion-running", "container_name": "container-companion-running"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "message": {
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
            }},
            {"type": "system", "subtype": "turn_duration"},
            {"type": "user", "isMeta": True, "message": {"content": "请继续输出"}},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertFalse(state["turn_ended_without_final"])

    def test_monitor_restarts_native_turn_that_ended_without_final_answer(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?",
            (pair["id"],),
        )
        arm_id = "arm-ended-without-final"
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
            (arm_id, pair["id"], "A", "A", str(self.root / "A"),
             "container-ended", "screen-ended", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        state = {
            "complete": False, "api_error": "", "api_error_turn_ended": False,
            "turn_ended_without_final": True, "prompt_matches": True,
            "followup_detected": False,
        }
        failed = {"id": arm_id, "status": "failed"}
        with patch.object(self.service.claude, "trace_state", return_value=state), \
             patch.object(self.service, "_handle_attempt_failure", return_value=failed) as failure:
            result = self.service._monitor_arm(pair["id"], arm_id, "same prompt")
        self.assertEqual(result, failed)
        self.assertIn("没有输出最终答复", failure.call_args.args[3])

    def test_monitor_releases_terminal_when_504_turn_has_ended(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,attempt_no,error_retry_count,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,2,1,?,?)""",
            ("arm-ended-504", pair["id"], "A", "A", str(self.root / "A"),
             "container-ended-504", "screen-ended-504", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        error_state = {
            "complete": False,
            "api_error": "API Error: 504 Gateway Timeout",
            "api_error_turn_ended": True,
            "prompt_matches": True,
            "followup_detected": False,
        }
        queued = {"id": "arm-ended-504", "status": "waiting_api_retry"}
        with patch.object(self.service.claude, "trace_state", return_value=error_state), \
             patch.object(self.service.claude, "business_progress", return_value={
                 "has_code": False, "last_modified": 0.0, "paths": [],
             }), \
             patch.object(self.service, "_handle_attempt_failure", return_value=queued) as failure:
            result = self.service._monitor_arm(
                pair["id"], "arm-ended-504", "Build a hard project with Docker Compose",
            )
        self.assertEqual(result, queued)
        failure.assert_called_once()
        self.assertIn("504", failure.call_args.args[3])
        released = self.db.one(
            """SELECT detail_json FROM audit_events
               WHERE event_type='claude.gateway_timeout_turn_ended'
                 AND entity_id='arm-ended-504'"""
        )
        self.assertTrue(json.loads(released["detail_json"])["session_interrupted_immediately"])

    def test_monitor_deduplicates_repainting_504_audit_events(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,attempt_no,error_retry_count,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,1,0,?,?)""",
            ("arm-repainting-504", pair["id"], "A", "A", str(self.root / "A"),
             "container-repainting-504", "screen-repainting-504", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        states = [
            {
                "complete": False,
                "monitor_error": "API Error: 504 Gateway Timeout（Claude 内部重试界面超过 800 秒无新轨迹）",
                "prompt_matches": True,
            },
            {
                "complete": False,
                "monitor_error": "API Error: 504 Gateway Timeout（Claude 内部重试界面超过 805 秒无新轨迹）",
                "prompt_matches": True,
            },
            {"complete": True, "result": "Finished after the gateway recovered"},
        ]
        completed = {"id": "arm-repainting-504", "status": "completed"}
        with patch.object(self.service.claude, "trace_state", side_effect=states), \
             patch.object(self.service.claude, "business_progress", return_value={
                 "has_code": False, "last_modified": 0.0, "paths": [],
             }), \
             patch.object(self.service.claude, "export_and_stop", return_value=self.root / "trace"), \
             patch.object(self.service, "_finish_checkpointed_arm", return_value=completed), \
             patch("pairwise_console.service.time.sleep"):
            result = self.service._monitor_arm(
                pair["id"], "arm-repainting-504", "Build a hard project with Docker Compose",
            )
        self.assertEqual(result, completed)
        self.assertEqual(
            self.db.one(
                """SELECT COUNT(*) count FROM audit_events
                   WHERE event_type='claude.api_error_deferred_to_no_code_deadline'
                     AND entity_id='arm-repainting-504'"""
            )["count"],
            1,
        )

    def test_repeated_429_retries_use_exponential_cooldown_without_failure_count(self):
        self.db.set_setting("rate_limit_retry_base_seconds", 60)
        self.db.set_setting("rate_limit_retry_max_seconds", 900)
        arm_id = "arm-rate-limit-backoff"
        self.assertEqual(self.service._rate_limit_restart_cooldown(arm_id), (1, 60))
        for index in range(3):
            self.db.audit("claude.rate_limit_turn_ended", "arm_run", arm_id, {
                "index": index,
                "counts_toward_development_attempts": False,
                "counts_toward_error_retries": False,
            })
        self.assertEqual(self.service._rate_limit_restart_cooldown(arm_id), (4, 480))
        for index in range(3, 8):
            self.db.audit("claude.rate_limit_turn_ended", "arm_run", arm_id, {"index": index})
        self.assertEqual(self.service._rate_limit_restart_cooldown(arm_id), (9, 900))

    def test_monitor_does_not_restart_a_completed_session_that_contains_api_error(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
            ("arm-api-recovered", pair["id"], "A", "A", str(self.root / "A"),
             "container-api-recovered", "screen-api-recovered", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        state = {
            "complete": True, "result": "Finished after internal retry",
            "api_error": "API Error: 504 Gateway Timeout",
            "session_id": "session-recovered", "prompt_id": "prompt-recovered",
        }
        completed = {"id": "arm-api-recovered", "status": "completed"}
        with patch.object(self.service.claude, "trace_state", return_value=state), \
             patch.object(self.service.claude, "export_and_stop", return_value=self.root / "trace"), \
             patch.object(self.service, "_finish_checkpointed_arm", return_value=completed) as finish, \
             patch.object(self.service, "_handle_attempt_failure") as retry:
            result = self.service._monitor_arm(pair["id"], "arm-api-recovered", "Build a hard project with Docker Compose")
        self.assertEqual(result, completed)
        finish.assert_called_once_with(pair["id"], "arm-api-recovered")
        retry.assert_not_called()
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id='arm-api-recovered' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(event["event_type"], "claude.api_error_recovered")

    def test_429_is_counted_and_retried_only_after_no_code_deadline(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.set_setting("first_prompt_stop_minutes", 0)
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        workspace = self.root / "api-error-wait"
        workspace.mkdir()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,attempt_no,error_retry_count,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,2,1,?,?)""",
            ("arm-api-error-wait", pair["id"], "A", "A", str(workspace),
             "container-api-wait", "screen-api-wait", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        state = {
            "complete": False,
            "api_error": "API Error: Request rejected (429) · litellm.RateLimitError",
            "api_error_turn_ended": True,
            "activity_signature": "api-error-signature", "activity_summary": ["API Error: 429"],
        }

        with patch.object(self.service.claude, "trace_state", return_value=state), \
             patch.object(self.service.claude, "runtime_alive", return_value=True), \
             patch.object(self.service.claude, "business_progress", return_value={
                 "has_code": False, "last_modified": 0.0, "paths": [],
             }):
            self.service._monitor_arm(
                pair["id"], "arm-api-error-wait", "Build a hard project with Docker Compose",
            )
        arm = self.db.one(
            """SELECT status,attempt_no,error_retry_count,api_retry_count,api_retry_after
                 FROM arm_runs WHERE id='arm-api-error-wait'"""
        )
        self.assertEqual(arm["status"], "waiting_api_retry")
        self.assertEqual(arm["attempt_no"], 2)
        self.assertEqual(arm["error_retry_count"], 1)
        self.assertEqual(arm["api_retry_count"], 1)
        self.assertTrue(arm["api_retry_after"])
        self.assertEqual(self.db.one("SELECT status FROM pairs WHERE id=?", (pair["id"],))["status"],
                         "waiting_api_retry")
        event = self.db.one(
            """SELECT event_type,detail_json FROM audit_events
               WHERE entity_id='arm-api-error-wait' ORDER BY id DESC LIMIT 1"""
        )
        self.assertEqual(event["event_type"], "claude.api_retry_queued")
        detail = json.loads(event["detail_json"])
        self.assertFalse(detail["counts_toward_development_attempts"])
        self.assertFalse(detail["counts_toward_error_retries"])
        self.assertTrue(detail["counts_toward_pair_failure_limit"])
        self.assertEqual(
            self.db.one("SELECT development_failure_count FROM pairs WHERE id=?", (pair["id"],))["development_failure_count"],
            1,
        )

    def test_new_user_rpm_error_ends_arm_without_waiting_for_turn_or_deadline(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.set_setting("claude_user_rpm_fast_fail_after", "2026-09-27T12:00:00+00:00")
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
            ("arm-user-rpm", pair["id"], "A", "A", str(self.root / "A"),
             "container-user-rpm", "screen-user-rpm", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        message = (
            'API Error: Request rejected (429) · {"error":{"code":"user_rpm_exceeded",'
            '"message":"Rate limit exceeded for current user. RPM limit=600"}}. '
            'Available Model Group Fallbacks=None'
        )
        state = {
            "complete": False, "api_error": message,
            "api_error_at": "2026-09-27T12:01:00.000Z",
            "api_error_turn_ended": False,
        }
        with patch.object(self.service.claude, "trace_state", return_value=state), \
             patch.object(self.service.claude, "archive_failed_attempt", return_value={
                 "id": "arm-user-rpm",
             }) as archive:
            result = self.service._monitor_arm(pair["id"], "arm-user-rpm", "Build a hard project")
        self.assertEqual(result["status"], "waiting_api_retry")
        archive.assert_called_once()
        self.assertTrue(archive.call_args.kwargs["prepare_retry"])
        self.assertEqual(self.db.one(
            "SELECT development_failure_count FROM pairs WHERE id=?", (pair["id"],)
        )["development_failure_count"], 1)
        event = self.db.one(
            "SELECT detail_json FROM audit_events WHERE event_type='claude.user_rpm_limit_immediate_end'"
        )
        self.assertFalse(json.loads(event["detail_json"])["waited_for_no_code_deadline"])
        self.assertFalse(self.service._user_rpm_fast_fail_active("2026-09-27T11:59:00Z"))
        self.assertFalse(self.service._is_claude_user_rpm_error("API Error: 429 max_parallel_requests"))

    def test_monitor_stops_when_failed_attempt_retry_is_deferred_for_capacity(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.set_setting("first_prompt_stop_minutes", 0)
        workspace = self.root / "capacity-deferred-retry"
        workspace.mkdir()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,attempt_no,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,1,?,?)""",
            ("arm-capacity-deferred", pair["id"], "A", "A", str(workspace),
             "container-capacity-deferred", "screen-capacity-deferred", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        state = {
            "complete": False, "api_error": "", "activity_signature": "no-code",
            "activity_summary": ["read-only inspection"],
        }
        deferred = {"id": "arm-capacity-deferred", "status": "waiting_retry"}
        with patch.object(self.service.claude, "trace_state", return_value=state) as trace, \
             patch.object(self.service.claude, "runtime_alive", return_value=True), \
             patch.object(self.service.claude, "business_progress", return_value={
                 "has_code": False, "last_modified": 0.0, "paths": [],
             }), \
             patch.object(self.service, "_handle_attempt_failure", return_value=deferred) as failure:
            result = self.service._monitor_arm(
                pair["id"], "arm-capacity-deferred", "Build a hard project with Docker Compose",
            )
        self.assertEqual(result, deferred)
        failure.assert_called_once()
        trace.assert_called_once()

    def test_monitor_replaces_task_after_second_identical_no_code_signature(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.set_setting("first_prompt_stop_minutes", 60)
        self.db.set_setting("repeated_no_code_trace_minutes", 0)
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,attempt_no,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,2,?,?)""",
            ("arm-repeat-no-code", pair["id"], "A", "A", str(self.root / "A"),
             "container-repeat", "screen-repeat", "auto_model/urm", "image", stamp, stamp, stamp),
        )
        signature = "same-signature"
        self.db.audit("claude.no_code_timeout_signature", "arm_run", "arm-repeat-no-code", {
            "attempt": 1, "signature": signature, "summary": ["no-assistant-activity"],
        })
        state = {
            "complete": False, "api_error": "", "activity_signature": signature,
            "activity_summary": ["no-assistant-activity"],
        }
        with patch.object(self.service.claude, "trace_state", return_value=state), \
             patch.object(self.service.claude, "runtime_alive", return_value=True), \
             patch.object(self.service.claude, "business_progress", return_value={
                 "has_code": False, "last_modified": 0.0, "paths": [],
             }), \
             patch.object(self.service, "_handle_attempt_failure", return_value={"status": "failed"}) as failure:
            self.service._monitor_arm(
                pair["id"], "arm-repeat-no-code", "Build a hard project with Docker Compose",
            )
        self.assertTrue(failure.call_args.kwargs["early_replace"])
        self.assertIn("连续 2 次", failure.call_args.args[3])
        latest = self.db.one(
            "SELECT detail_json FROM audit_events WHERE entity_id='arm-repeat-no-code' ORDER BY id DESC LIMIT 1"
        )
        detail = json.loads(latest["detail_json"])
        self.assertTrue(detail["matches_previous_attempt"])
        self.assertEqual(detail["rule_trigger"], "repeated_trace_early")

    def test_second_no_code_attempt_with_new_trace_keeps_sixty_minute_window(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.set_setting("first_prompt_stop_minutes", 60)
        self.db.set_setting("repeated_no_code_trace_minutes", 0)
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,attempt_no,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,2,?,?)""",
            ("arm-new-no-code-trace", pair["id"], "A", "A", str(self.root / "new-trace"),
             "container-new-trace", "screen-new-trace", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        self.db.audit("claude.no_code_timeout_signature", "arm_run", "arm-new-no-code-trace", {
            "attempt": 1, "signature": "old-signature", "summary": ["old"],
        })
        states = [
            {"complete": False, "api_error": "", "activity_signature": "new-signature",
             "activity_summary": ["new"]},
            {"complete": True, "result": "Finished before the 60-minute deadline",
             "api_error": ""},
        ]
        completed = {"id": "arm-new-no-code-trace", "status": "completed"}
        with patch.object(self.service.claude, "trace_state", side_effect=states), \
             patch.object(self.service.claude, "runtime_alive", return_value=True), \
             patch.object(self.service.claude, "business_progress", return_value={
                 "has_code": False, "last_modified": 0.0, "paths": [],
             }), \
             patch.object(self.service.claude, "export_and_stop", return_value=self.root / "trace"), \
             patch.object(self.service, "_finish_checkpointed_arm", return_value=completed), \
             patch.object(self.service, "_handle_attempt_failure") as failure, \
             patch("pairwise_console.service.time.sleep"):
            result = self.service._monitor_arm(
                pair["id"], "arm-new-no-code-trace", "Build a hard project with Docker Compose",
            )
        self.assertEqual(result, completed)
        failure.assert_not_called()

    def test_system_turn_companion_is_not_treated_as_manual_followup(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-companion", "container_name": "container-companion"}
        companion = "[Your previous response had no visible output. Please continue and produce a user-visible response.]"
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "user", "isMeta": True, "turnCompanion": True,
             "message": {"content": companion}},
            {"type": "assistant", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "Finished"}],
            }},
            {"type": "system", "subtype": "turn_duration"},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertTrue(state["complete"])
        self.assertFalse(state["followup_detected"])
        self.assertEqual(state["automatic_companion_count"], 1)
        self.assertEqual(state["automatic_companion_messages"], [companion])

    def test_real_user_followup_still_invalidates_session(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-followup", "container_name": "container-followup"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "user", "message": {"content": "Please also change the database schema"}},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertTrue(state["followup_detected"])
        self.assertEqual(state["followup_text"], "Please also change the database schema")

    def test_failed_trace_copy_retains_old_container_and_prepares_fresh_session(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        workspace = self.root / "projects" / pair["id"] / "workspaces" / "A"
        workspace.mkdir(parents=True)
        (workspace / "partial.py").write_text("print('partial')\n", encoding="utf-8")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,session_id,prompt_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?,?,?)""",
            ("arm-copy-fail", pair["id"], "A", "A", str(workspace), "old-container", "old-screen",
             "auto_model/urm", "image", stamp, "session-1", "prompt-1", stamp, stamp),
        )
        failed_copy = type("Result", (), {"returncode": 1, "stdout": "", "stderr": "copy failed"})()
        with patch.object(self.service.claude, "_graceful_stop"), \
             patch.object(self.service.claude, "_container_exists", return_value=True), \
             patch.object(self.service.claude, "_copy_traces", return_value=failed_copy), \
             patch.object(self.service.claude, "_screen_running", return_value=False), \
             patch.object(self.service.claude, "_close_terminal_window"), \
             patch("pairwise_console.claude_runner.run_command") as command:
            updated = self.service.claude.archive_failed_attempt(
                self.db.one("SELECT * FROM arm_runs WHERE id='arm-copy-fail'"), "API Error: 429", True,
                count_development_failure=False,
                count_error_retry=False,
            )
        self.assertEqual(updated["attempt_no"], 1)
        self.assertEqual(updated["error_retry_count"], 0)
        self.assertEqual(updated["status"], "queued")
        self.assertNotEqual(updated["container_name"], "old-container")
        self.assertNotEqual(updated["workspace_path"], str(workspace))
        self.assertTrue(Path(updated["workspace_path"]).is_dir())
        self.assertFalse(any(call.args[0][:2] == ["docker", "rm"] for call in command.call_args_list))

    def test_completed_trace_is_verified_before_container_removal(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        workspace = self.root / "verified-workspace"
        workspace.mkdir()
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,session_id,prompt_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'checkpointing',?,?,?,?,?)""",
            ("arm-verified", pair["id"], "A", "A", str(workspace), "verified-container", "verified-screen",
             "auto_model/urm", "image", stamp, "session-verified", "prompt-verified", stamp, stamp),
        )
        root = self.service.claude.runtime_dir / "arm-verified"
        root.mkdir(parents=True)
        (root / "prompt.txt").write_text("Build verified output", encoding="utf-8")

        def copy_trace(_container, destination):
            events = [
                {"type": "user", "promptId": "prompt-verified", "message": {"content": "Build verified output"}},
                {"type": "system", "subtype": "turn_duration"},
            ]
            (destination / "session-verified.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        removed = type("Result", (), {"returncode": 0, "stdout": "verified-container", "stderr": ""})()
        with patch.object(self.service.claude, "_graceful_stop"), \
             patch.object(self.service.claude, "_copy_traces", side_effect=copy_trace), \
             patch.object(self.service.claude, "_screen_running", return_value=False), \
             patch.object(self.service.claude, "_close_terminal_window"), \
             patch("pairwise_console.claude_runner.run_command", return_value=removed) as command:
            trace_dir = self.service.claude.export_and_stop(
                self.db.one("SELECT * FROM arm_runs WHERE id='arm-verified'"),
            )
        self.assertTrue((trace_dir / "session-verified.jsonl").is_file())
        command.assert_called_once_with(["docker", "rm", "verified-container"], check=False, timeout=60)

    def test_completed_trace_survives_concurrent_container_removal(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        workspace = self.root / "removal-race-workspace"
        workspace.mkdir()
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,session_id,prompt_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'checkpointing',?,?,?,?,?)""",
            ("arm-removal-race", pair["id"], "A", "A", str(workspace),
             "removing-container", "removing-screen", "auto_model/urm", "image", stamp,
             "session-removal-race", "prompt-removal-race", stamp, stamp),
        )
        root = self.service.claude.runtime_dir / "arm-removal-race"
        root.mkdir(parents=True)
        (root / "prompt.txt").write_text("Build verified output", encoding="utf-8")

        def copy_trace(_container, destination):
            events = [
                {"type": "user", "promptId": "prompt-removal-race",
                 "message": {"content": "Build verified output"}},
                {"type": "system", "subtype": "turn_duration"},
            ]
            (destination / "session-removal-race.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        racing_remove = type("Result", (), {
            "returncode": 1, "stdout": "",
            "stderr": "removal of container removing-container is already in progress",
        })()
        with patch.object(self.service.claude, "_graceful_stop"), \
             patch.object(self.service.claude, "_copy_traces", side_effect=copy_trace), \
             patch.object(self.service.claude, "_container_exists", return_value=False), \
             patch.object(self.service.claude, "_screen_running", return_value=False), \
             patch.object(self.service.claude, "_close_terminal_window"), \
             patch("pairwise_console.claude_runner.run_command", return_value=racing_remove):
            trace_dir = self.service.claude.export_and_stop(
                self.db.one("SELECT * FROM arm_runs WHERE id='arm-removal-race'"),
            )
        self.assertTrue((trace_dir / "session-removal-race.jsonl").is_file())
        current = self.db.one("SELECT status,trace_path FROM arm_runs WHERE id='arm-removal-race'")
        self.assertEqual(current["status"], "exported")
        self.assertEqual(current["trace_path"], str(trace_dir))

    def test_second_pair_development_failure_retires_pair_and_schedules_new_task(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        self.db.execute(
            "UPDATE pairs SET development_failure_count=1 WHERE id=?", (pair["id"],),
        )
        arm = {"id": "arm-second-failure", "pair_id": pair["id"], "attempt_no": 1, "arm": "A"}
        with patch.object(self.service.claude, "archive_failed_attempt", return_value={**arm, "status": "failed"}) as archive, \
             patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace:
            result = self.service._handle_attempt_failure(pair["id"], arm, "same prompt", "container exited")
        self.assertEqual(result["status"], "failed")
        archive.assert_called_once()
        replace.assert_called_once_with(
            pair["id"], arm["id"], "container exited", "项目累计 2 次开发失败",
        )

    def test_running_peer_failure_does_not_raise_shared_count_above_maximum(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            """UPDATE pairs SET status='running',stage='development',
               development_failure_count=2 WHERE id=?""",
            (pair["id"],),
        )

        count, maximum, exhausted = self.service._record_pair_development_failure(
            pair["id"], "arm-running-peer", "运行中的另一侧自然失败", "development_error",
        )

        self.assertEqual((count, maximum, exhausted), (2, 2, True))
        self.assertEqual(
            self.db.one(
                "SELECT development_failure_count FROM pairs WHERE id=?", (pair["id"],),
            )["development_failure_count"],
            2,
        )
        event = self.db.one(
            "SELECT detail_json FROM audit_events WHERE entity_id='arm-running-peer' ORDER BY id DESC LIMIT 1"
        )
        self.assertTrue(json.loads(event["detail_json"])["pair_failure_budget_already_exhausted"])

    def test_second_failure_keeps_the_other_arm_running_to_its_own_timeout(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',development_failure_count=1 WHERE id=?",
            (pair["id"],),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,prompt_sent_at,attempt_no,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'developing',?,1,?,?)""",
                ("arm-drain-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-drain-" + arm, "screen-drain-" + arm,
                 "auto_model/urm", "image", stamp, stamp, stamp),
            )
        failed_arm = self.db.one("SELECT * FROM arm_runs WHERE id='arm-drain-A'")

        def archive(arm, error, prepare_retry=False, **_kwargs):
            self.db.execute(
                "UPDATE arm_runs SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?",
                (error, stamp, stamp, arm["id"]),
            )
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],))

        with patch.object(self.service.claude, "archive_failed_attempt", side_effect=archive), \
             patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace:
            result = self.service._handle_attempt_failure(
                pair["id"], failed_arm, "same prompt", "container exited",
            )
        self.assertEqual(result["status"], "failed")
        replace.assert_not_called()
        current = self.db.one(
            "SELECT status,stage,development_failure_count,error FROM pairs WHERE id=?",
            (pair["id"],),
        )
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["stage"], "development")
        self.assertEqual(current["development_failure_count"], 2)
        self.assertIn("B 侧继续运行", current["error"])
        self.assertEqual(
            self.db.one("SELECT status FROM arm_runs WHERE id='arm-drain-B'")["status"],
            "developing",
        )

    def test_pair_validates_completed_peer_before_retirement(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',development_failure_count=2 WHERE id=?",
            (pair["id"],),
        )
        for arm, status, error, commit_sha in (
            ("A", "failed", "container exited", ""),
            ("B", "completed", "", "b" * 40),
        ):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,error,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-finish-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-finish-" + arm, "screen-finish-" + arm,
                 "auto_model/urm", "image", status, error, commit_sha, stamp, stamp),
            )
        with patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace, \
             patch.object(self.service, "_schedule_completed_arm_validations") as validate:
            self.service._refresh_pair_after_arm(pair["id"])
        replace.assert_not_called()
        validate.assert_called_once_with(pair["id"])
        current = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["stage"], "development")
        self.assertIn("先执行 Docker 产物验收", current["error"])

        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?, 'observed_failed',?,?)""",
            ("check-finish-B", pair["id"], "B", "b" * 40, stamp, stamp),
        )
        with patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace:
            self.service._refresh_pair_after_arm(pair["id"])
        replace.assert_called_once_with(
            pair["id"], "arm-finish-A", "container exited",
            "项目累计 2 次失败，另一侧已结束",
        )

    def test_failed_pair_with_reset_budget_still_validates_completed_peer(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',development_failure_count=0 WHERE id=?",
            (pair["id"],),
        )
        for arm, status, commit_sha in (("A", "completed", "a" * 40), ("B", "failed", "")):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-reset-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-reset-" + arm, "screen-reset-" + arm,
                 "auto_model/urm", "image", status, commit_sha, stamp, stamp),
            )
        with patch.object(self.service, "_schedule_completed_arm_validations") as validate:
            self.service._refresh_pair_after_arm(pair["id"])
        validate.assert_called_once_with(pair["id"])
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "running", "stage": "development"},
        )

        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?, 'passed',?,?)""",
            ("check-reset-A", pair["id"], "A", "a" * 40, stamp, stamp),
        )
        self.service._refresh_pair_after_arm(pair["id"])
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "failed", "stage": "development_failed"},
        )

    def test_api_failure_after_no_code_deadline_uses_retry_queue(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        arm = {"id": "arm-transient-api", "pair_id": pair["id"], "attempt_no": 2, "arm": "A"}
        error = "API Error: Request rejected (429) · litellm.RateLimitError: max_parallel_requests"
        queued = {**arm, "status": "waiting_api_retry"}
        with patch.object(self.service, "_queue_api_retry", return_value=queued) as queue, \
             patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace:
            result = self.service._handle_attempt_failure(pair["id"], arm, "same prompt", error)
        self.assertEqual(result, queued)
        queue.assert_called_once_with(pair["id"], arm, error)
        replace.assert_not_called()

    def test_second_504_retires_the_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        arm = {"id": "arm-second-504", "pair_id": pair["id"], "attempt_no": 1, "arm": "A"}
        error = "API Error: 504 Gateway Timeout"
        self.db.execute("UPDATE pairs SET development_failure_count=1 WHERE id=?", (pair["id"],))
        with patch.object(
            self.service.claude, "archive_failed_attempt", return_value={**arm, "status": "failed"},
        ) as archive, patch.object(
            self.service, "_retire_pair_and_schedule_replacement",
        ) as replace:
            result = self.service._queue_api_retry(pair["id"], arm, error)
        self.assertEqual(result["status"], "failed")
        archive.assert_called_once()
        replace.assert_called_once_with(
            pair["id"], arm["id"], error, "项目累计 2 次失败（包含 API 错误）",
        )
        self.assertEqual(
            self.db.one("SELECT development_failure_count FROM pairs WHERE id=?", (pair["id"],))["development_failure_count"],
            2,
        )

    def test_second_504_does_not_stop_an_active_peer(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',development_failure_count=1 WHERE id=?",
            (pair["id"],),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,prompt_sent_at,attempt_no,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'developing',?,1,?,?)""",
                ("arm-api-drain-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-api-drain-" + arm, "screen-api-drain-" + arm,
                 "auto_model/urm", "image", stamp, stamp, stamp),
            )
        failed_arm = self.db.one("SELECT * FROM arm_runs WHERE id='arm-api-drain-A'")
        error = "API Error: 504 Gateway Timeout"

        def archive(arm, failure, prepare_retry=False, **_kwargs):
            self.db.execute(
                "UPDATE arm_runs SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?",
                (failure, stamp, stamp, arm["id"]),
            )
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],))

        with patch.object(self.service.claude, "archive_failed_attempt", side_effect=archive), \
             patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace:
            result = self.service._queue_api_retry(pair["id"], failed_arm, error)
        self.assertEqual(result["status"], "failed")
        replace.assert_not_called()
        self.assertEqual(
            self.db.one("SELECT status FROM arm_runs WHERE id='arm-api-drain-B'")["status"],
            "developing",
        )
        current = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["stage"], "development")
        self.assertIn("B 侧继续运行", current["error"])

    def test_second_failure_does_not_restart_a_peer_waiting_for_api_retry(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """UPDATE pairs SET status='running',stage='development',
               development_failure_count=2 WHERE id=?""",
            (pair["id"],),
        )
        for arm, status, error in (
            ("A", "waiting_api_retry", "API Error: 504 Gateway Timeout"),
            ("B", "failed", "API Error: 429 Rate limit"),
        ):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,error,api_retry_after,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-budget-race-" + arm, pair["id"], arm, arm,
                 str(self.root / ("budget-race-" + arm)), "container-budget-race-" + arm,
                 "screen-budget-race-" + arm, "auto_model/urm", "image", status, error,
                 "2000-01-01T00:00:00+00:00", stamp, stamp),
            )

        with patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace, \
             patch.object(self.service.git, "reset_arm_to_baseline") as reset, \
             patch.object(self.service.claude, "launch") as launch:
            result = self.service._recover_api_retry(
                pair["id"], "arm-budget-race-A", "same prompt",
            )

        self.assertEqual(result["status"], "waiting_api_retry")
        reset.assert_not_called()
        launch.assert_not_called()
        replace.assert_called_once()

    def test_due_api_retry_query_excludes_exhausted_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """UPDATE pairs SET status='running',stage='development',
               development_failure_count=2 WHERE id=?""",
            (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,status,api_retry_after,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'waiting_api_retry',?,?,?)""",
            ("arm-exhausted-api-due", pair["id"], "A", "A", str(self.root / "api-due"),
             "container-api-due", "screen-api-due", "auto_model/urm", "image",
             "2000-01-01T00:00:00+00:00", stamp, stamp),
        )
        submitted = []
        with patch.object(self.service, "_available_development_arm_slots", return_value=1), \
             patch.object(
                 self.service, "_submit_monitor",
                 side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
             ):
            resumed = self.service._schedule_due_api_retries(1)
        self.assertEqual(resumed, 0)
        self.assertEqual(submitted, [])

    def test_restart_recovery_does_not_launch_unsent_arm_after_pair_budget_exhausted(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """UPDATE pairs SET status='running',stage='development',
               development_failure_count=2 WHERE id=?""",
            (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,status,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'waiting_retry','等待 Claude 开发终端空位',?,?)""",
            ("arm-exhausted-restart", pair["id"], "A", "A",
             str(self.root / "exhausted-restart"), "container-exhausted-restart",
             "screen-exhausted-restart", "auto_model/urm", "image", stamp, stamp),
        )

        with patch.object(self.service, "_finish_exhausted_pair_after_peer") as finish, \
             patch.object(self.service.claude, "reset_unsent_arm") as reset, \
             patch.object(self.service.claude, "launch") as launch:
            result = self.service._recover_pending_retry(
                pair["id"], "arm-exhausted-restart", "same prompt",
            )

        self.assertEqual(result["status"], "waiting_retry")
        finish.assert_called_once_with(pair["id"])
        reset.assert_not_called()
        launch.assert_not_called()

    def test_api_retry_cooldown_uses_provider_reset_and_bounded_backoff(self):
        current = datetime(2026, 9, 18, 19, 0, 0, tzinfo=timezone.utc)
        error = "API Error: 429 Rate limit. Limit resets at: 2026-09-18 19:00:20 UTC"
        self.assertEqual(self.service._api_retry_delay_seconds(error, 0, current), 20)
        self.assertEqual(self.service._api_retry_delay_seconds(error, 2, current), 20)
        self.assertEqual(
            self.service._api_retry_delay_seconds("API Error: 504 Gateway Timeout", 0, current), 120,
        )
        self.assertEqual(
            self.service._api_retry_delay_seconds("API Error: 504 Gateway Timeout", 4, current), 900,
        )

    def test_waiting_api_pair_is_retried_before_consuming_a_ready_task(self):
        self.db.set_setting("max_pairs_parallel", 1)
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='waiting_api_retry',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,api_retry_count,api_retry_after,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'waiting_api_retry',NULL,1,?,?,?)""",
            ("arm-api-due", pair["id"], "A", "A", str(self.root / "api-due"),
             "container-api-due", "screen-api-due", "auto_model/urm", "image",
             "2000-01-01T00:00:00+00:00", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("task-spare", "test", "zero_to_one", "spare hard task", "Build another hard project",
             "困难", '["跨模块状态"]', "spare-fingerprint", "ready", stamp, stamp),
        )
        submitted = []
        with patch.object(
            self.service, "_submit_monitor",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ), patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_refill_once") as refill:
            status = self.service._schedule_auto_pipeline_once()
        self.assertIn("api-retry-arm-api-due", submitted)
        self.assertEqual(status["activePairs"], 1)
        self.assertEqual(status["waitingApiPairs"], 0)
        self.assertEqual(status["waitingApiArms"], 1)
        self.assertEqual(len(self.db.all("SELECT id FROM pairs")), 1)
        self.assertEqual(self.db.one("SELECT status FROM tasks WHERE id='task-spare'")["status"], "ready")
        refill.assert_not_called()

    def test_waiting_arm_of_active_pair_resumes_when_pair_limit_is_full(self):
        self.db.set_setting("max_pairs_parallel", 1)
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,api_retry_count,api_retry_after,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,0,NULL,?,?)""",
            ("arm-active-a", pair["id"], "A", "A", str(self.root / "active-a"),
             "container-active-a", "screen-active-a", "auto_model/urm", "image", stamp, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,api_retry_count,api_retry_after,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'waiting_api_retry',NULL,1,?,?,?)""",
            ("arm-active-b", pair["id"], "B", "B", str(self.root / "active-b"),
             "container-active-b", "screen-active-b", "auto_model/urm", "image",
             "2000-01-01T00:00:00+00:00", stamp, stamp),
        )
        submitted = []
        with patch.object(
            self.service, "_submit_monitor",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ), patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_refill_once"):
            status = self.service._schedule_auto_pipeline_once()
        self.assertIn("monitor-arm-active-a", submitted)
        self.assertIn("api-retry-arm-active-b", submitted)
        self.assertEqual(status["activePairs"], 1)
        self.assertEqual(
            self.db.one("SELECT status FROM pairs WHERE id=?", (pair["id"],))["status"], "running",
        )

    def test_disabled_api_auto_retry_keeps_waiting_arm_queued(self):
        self.db.set_setting("max_pairs_parallel", 1)
        self.db.set_setting("claude_api_auto_retry_enabled", False)
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='waiting_api_retry',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,api_retry_count,api_retry_after,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'waiting_api_retry',NULL,1,?,?,?)""",
            ("arm-api-disabled", pair["id"], "A", "A", str(self.root / "api-disabled"),
             "container-api-disabled", "screen-api-disabled", "auto_model/urm", "image",
             "2000-01-01T00:00:00+00:00", stamp, stamp),
        )
        submitted = []
        with patch.object(
            self.service, "_submit_monitor",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ), patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_refill_once"):
            status = self.service._schedule_auto_pipeline_once()
        self.assertEqual(submitted, [])
        self.assertFalse(status["apiAutoRetryEnabled"])
        self.assertEqual(status["waitingApiArms"], 1)
        self.assertEqual(
            self.db.one("SELECT status FROM pairs WHERE id=?", (pair["id"],))["status"],
            "waiting_api_retry",
        )

    def test_global_429_cooldown_blocks_retries_and_new_pairs(self):
        self.db.set_setting("max_pairs_parallel", 1)
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='waiting_api_retry',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,api_retry_count,api_retry_after,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'waiting_api_retry',NULL,1,?,?,?)""",
            ("arm-api-cooling", pair["id"], "A", "A", str(self.root / "api-cooling"),
             "container-api-cooling", "screen-api-cooling", "auto_model/urm", "image",
             "2000-01-01T00:00:00+00:00", stamp, stamp),
        )
        self.db.set_setting(
            "claude_api_cooldown_until",
            (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds"),
        )
        submitted = []
        with patch.object(
            self.service, "_submit_monitor",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ), patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_refill_once") as refill:
            status = self.service._schedule_auto_pipeline_once()
        self.assertEqual(submitted, [])
        self.assertEqual(status["activePairs"], 0)
        self.assertEqual(status["waitingApiPairs"], 1)
        self.assertEqual(len(self.db.all("SELECT id FROM pairs")), 1)
        refill.assert_not_called()

    def test_due_api_pair_resumes_when_no_ready_work_uses_the_free_slot(self):
        self.db.set_setting("max_pairs_parallel", 1)
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='waiting_api_retry',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,api_retry_count,api_retry_after,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'waiting_api_retry',NULL,1,?,?,?)""",
            ("arm-api-only-due", pair["id"], "A", "A", str(self.root / "api-only-due"),
             "container-api-only", "screen-api-only", "auto_model/urm", "image",
             "2000-01-01T00:00:00+00:00", stamp, stamp),
        )
        submitted = []
        with patch.object(
            self.service, "_submit_monitor",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ), patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_refill_once") as refill:
            status = self.service._schedule_auto_pipeline_once()
        self.assertEqual(submitted, ["api-retry-arm-api-only-due"])
        self.assertEqual(status["activePairs"], 1)
        self.assertEqual(status["waitingApiPairs"], 0)
        self.assertEqual(self.db.one("SELECT status FROM pairs WHERE id=?", (pair["id"],))["status"], "running")
        refill.assert_not_called()

    def test_scheduler_reconnects_monitor_to_live_development_after_restart(self):
        self.db.set_setting("max_pairs_parallel", 1)
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
            ("arm-live-after-restart", pair["id"], "A", "A", str(self.root / "live"),
             "container-live", "screen-live", "auto_model/urm", "image", stamp, stamp, stamp),
        )
        submitted = []
        with patch.object(
            self.service, "_submit_monitor",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ), patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_refill_once"):
            self.service._schedule_auto_pipeline_once()
        self.assertIn("monitor-arm-live-after-restart", submitted)

    def test_second_prompt_infrastructure_failure_pauses_instead_of_looping(self):
        self.insert_ready_task()
        self.db.execute("UPDATE tasks SET stack='Python 3.13, FastAPI' WHERE id='task-1'")
        pair = self.service.create_pair("task-1")
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        arm = {"id": "arm-prompt-retry", "pair_id": pair["id"], "attempt_no": 3, "arm": "A"}
        paused = {**arm, "status": "failed"}
        with patch.object(
            self.service, "_restart_arm_from_baseline",
            side_effect=RuntimeError("launch failed"),
        ) as restart, patch.object(
            self.service, "_retire_pair_and_schedule_replacement",
        ) as replace, patch.object(
            self.service.claude, "archive_failed_attempt", return_value=paused,
        ) as archive:
            result = self.service._handle_attempt_failure(
                pair["id"], arm, "same prompt",
                "轨迹首轮 User Prompt 与数据库原题面不一致",
            )
        self.assertEqual(result["status"], "infrastructure_paused")
        self.assertEqual(restart.call_count, 1)
        archive.assert_called_once()
        replace.assert_not_called()
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id=? ORDER BY id DESC LIMIT 1",
            (arm["id"],),
        )
        self.assertEqual(event["event_type"], "claude.prompt_infrastructure_paused")

    def test_repository_materialization_retry_does_not_consume_development_budget(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        arm = {
            "id": "arm-materialize-retry", "pair_id": pair["id"],
            "attempt_no": 1, "arm": "B", "prompt_sent_at": None,
        }
        restarted = {**arm, "status": "waiting_retry"}
        error = "恢复全新 Session 失败：A/B 源仓库未保持指定分支的清洁基线"
        with patch.object(
            self.service, "_restart_arm_from_baseline", return_value=restarted,
        ) as restart:
            result = self.service._handle_attempt_failure(
                pair["id"], arm, "same prompt", error,
            )
        self.assertEqual(result, restarted)
        restart.assert_called_once_with(
            pair["id"], arm, "same prompt", error,
            count_development_failure=False, count_error_retry=False,
        )
        self.assertEqual(
            self.db.one(
                "SELECT development_failure_count FROM pairs WHERE id=?", (pair["id"],),
            )["development_failure_count"],
            0,
        )
        event = self.db.one(
            "SELECT detail_json FROM audit_events WHERE entity_id=? ORDER BY id DESC LIMIT 1",
            (arm["id"],),
        )
        self.assertFalse(json.loads(event["detail_json"])["counts_toward_pair_failure_limit"])

    def test_api_retry_screen_launch_failure_does_not_consume_second_pair_failure(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        arm_id = "arm-api-screen-retry"
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',development_failure_count=1 WHERE id=?",
            (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
               screen_name,model,image,status,api_retry_count,api_retry_after,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'waiting_api_retry',1,?,?,?)""",
            (arm_id, pair["id"], "B", "B", str(self.root / "api-screen-retry"),
             "container-api-screen-retry", "screen-api-screen-retry", "auto_model/urm",
             "image", "2000-01-01T00:00:00+00:00", stamp, stamp),
        )
        restarted = {"id": arm_id, "status": "waiting_retry"}
        with patch.object(self.service, "_available_development_arm_slots", return_value=1), \
             patch.object(self.service.git, "reset_arm_to_baseline", return_value="baseline"), \
             patch.object(self.service, "_launch_arm_if_capacity", return_value={"id": arm_id}), \
             patch.object(self.service.claude, "wait_until_ready"), \
             patch.object(self.service.claude, "materialize_repository"), \
             patch.object(self.service, "_send_prompt_with_pair_stagger",
                          side_effect=RuntimeError("Screen 分块题面输入或提交失败：No screen session found.")), \
             patch.object(self.service, "_restart_arm_from_baseline",
                          return_value=restarted) as restart, \
             patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace:
            result = self.service._recover_api_retry(pair["id"], arm_id, "same prompt")
        self.assertEqual(result, restarted)
        restart.assert_called_once()
        self.assertFalse(restart.call_args.kwargs["count_development_failure"])
        replace.assert_not_called()
        self.assertEqual(
            self.db.one("SELECT development_failure_count FROM pairs WHERE id=?", (pair["id"],))[
                "development_failure_count"
            ], 1,
        )
        event = self.db.one(
            "SELECT detail_json FROM audit_events WHERE entity_id=? ORDER BY id DESC LIMIT 1",
            (arm_id,),
        )
        self.assertFalse(json.loads(event["detail_json"])["counts_toward_pair_failure_limit"])

    def test_api_text_inside_restart_infrastructure_error_still_does_not_count(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',development_failure_count=1 WHERE id=?",
            (pair["id"],),
        )
        arm = {"id": "arm-api-text-in-infra", "pair_id": pair["id"], "arm": "B", "attempt_no": 1}
        with patch.object(self.service, "_restart_arm_from_baseline",
                          return_value={**arm, "status": "waiting_retry"}) as restart:
            self.service._handle_attempt_failure(
                pair["id"], arm, "same prompt",
                "全新 Session 基础设施重试失败：API Error: 504 Gateway Timeout",
            )
        self.assertFalse(restart.call_args.kwargs["count_development_failure"])
        self.assertEqual(
            self.db.one("SELECT development_failure_count FROM pairs WHERE id=?", (pair["id"],))[
                "development_failure_count"
            ], 1,
        )

    def test_code_stall_watchdog_uses_total_and_native_trace_idle_time(self):
        self.assertEqual(self.service._development_stall_reason(69 * 60, 30 * 60, True), "")
        self.assertEqual(self.service._development_stall_reason(75 * 60, 39 * 60, True), "")
        self.assertEqual(self.service._development_stall_reason(75 * 60, 30 * 60, False), "")
        reason = self.service._development_stall_reason(70 * 60, 40 * 60, True)
        self.assertIn("原生轨迹", reason)
        self.assertIn("终端动画刷新不计为进展", reason)

    def test_business_progress_timeout_does_not_wait_for_seventy_minutes(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        old_prompt = (datetime.now(timezone.utc) - timedelta(minutes=62)).isoformat()
        stamp = now_iso()
        workspace = self.root / "business-idle"
        workspace.mkdir()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
            ("arm-business-idle", pair["id"], "A", "A", str(workspace),
             "container-business-idle", "screen-business-idle", "auto_model/urm", "image",
             old_prompt, stamp, stamp),
        )
        stale = time.time() - 61 * 60
        state = {
            "complete": False, "api_error": "", "progress_token": "recent-text",
            "effective_progress_age_seconds": 0,
            "last_tool_activity_at": datetime.fromtimestamp(stale, timezone.utc).isoformat(),
        }
        progress = {"has_code": True, "last_modified": stale, "paths": ["src/app.ts"]}
        with patch.object(self.service.claude, "trace_state", return_value=state), \
             patch.object(self.service.claude, "runtime_alive", return_value=True), \
             patch.object(self.service.claude, "business_progress", return_value=progress), \
             patch.object(
                 self.service, "_handle_attempt_failure", return_value={"status": "failed"},
             ) as failure:
            result = self.service._monitor_arm(
                pair["id"], "arm-business-idle", "Build a hard project with Docker Compose",
            )
        self.assertEqual(result["status"], "failed")
        self.assertIn("连续 60 分钟", failure.call_args.args[3])
        self.assertNotIn("70 分钟", failure.call_args.args[3])
        event = self.db.one(
            "SELECT detail_json FROM audit_events "
            "WHERE event_type='claude.business_progress_timeout' "
            "AND entity_id='arm-business-idle' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(json.loads(event["detail_json"])["businessPaths"], ["src/app.ts"])

    def test_code_stall_watchdog_refreshes_on_native_jsonl_progress(self):
        token, progress_at, changed = self.service._advance_native_progress(
            {"progress_token": "session:10:2048"}, "", 100.0, 120.0,
        )
        self.assertEqual((token, progress_at, changed), ("session:10:2048", 120.0, True))

        unchanged = self.service._advance_native_progress(
            {"progress_token": "session:10:2048"}, token, progress_at, 180.0,
        )
        self.assertEqual(unchanged, ("session:10:2048", 120.0, False))

        advanced = self.service._advance_native_progress(
            {"progress_token": "session:11:4096"}, token, progress_at, 200.0,
        )
        self.assertEqual(advanced, ("session:11:4096", 200.0, True))

        restored = self.service._advance_native_progress(
            {"progress_token": "session:12:8192", "effective_progress_age_seconds": 600},
            "", 0.0, 1000.0,
        )
        self.assertEqual(restored, ("session:12:8192", 400.0, True))

    def test_old_unowned_git_index_lock_is_removed_safely(self):
        workspace = self.root / "locked-repo"
        (workspace / ".git").mkdir(parents=True)
        lock = workspace / ".git" / "index.lock"
        lock.write_text("stale", encoding="utf-8")
        old = time.time() - 120
        os.utime(lock, (old, old))
        no_owner = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with patch("pairwise_console.gitops.shutil.which", return_value="/usr/sbin/lsof"), \
             patch("pairwise_console.gitops.run_command", return_value=no_owner):
            self.assertTrue(self.service.git._remove_stale_index_lock(workspace))
        self.assertFalse(lock.exists())
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id=? ORDER BY id DESC LIMIT 1",
            (str(workspace),),
        )
        self.assertEqual(event["event_type"], "git.stale_index_lock_removed")

    def test_run_command_uses_spawn_safe_cwd_wrapper(self):
        from pairwise_console.commands import run_command

        workspace = self.root / "spawn-safe-cwd"
        workspace.mkdir()
        result = run_command(["pwd"], cwd=workspace)
        self.assertEqual(result.stdout.strip(), str(workspace))
        self.assertEqual(result.args, ["pwd"])
        self.assertEqual(result.cwd, str(workspace))

    def test_replaced_pair_cannot_restart_an_arm_from_baseline(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute("UPDATE pairs SET status='running',stage='replaced' WHERE id=?", (pair["id"],))
        arm = {"id": "arm-retired-race", "pair_id": pair["id"], "attempt_no": 3, "arm": "A"}
        with patch.object(self.service, "_invalidate_recordings") as invalidate, \
             patch.object(self.service.claude, "archive_failed_attempt") as archive, \
             patch.object(self.service.git, "reset_arm_to_baseline") as reset:
            result = self.service._restart_arm_from_baseline(
                pair["id"], arm, "same prompt", "stale monitor",
            )
        self.assertEqual(result, {"restart_skipped_terminal_pair": True})
        invalidate.assert_not_called()
        archive.assert_not_called()
        reset.assert_not_called()

    def test_pipeline_drain_archives_failure_without_resetting_baseline(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        self.db.set_setting("pipeline_drain", True)
        arm = {"id": "arm-paused-retry", "pair_id": pair["id"], "arm": "A", "attempt_no": 1}
        with patch.object(self.service, "_invalidate_recordings"), \
             patch.object(self.service.claude, "archive_failed_attempt", return_value=arm) as archive, \
             patch.object(self.service.git, "reset_arm_to_baseline") as reset, \
             patch.object(self.service, "_launch_arm_if_capacity") as launch:
            self.service._restart_arm_from_baseline(pair["id"], arm, "original prompt", "API Error: 504")
        archive.assert_called_once()
        reset.assert_not_called()
        launch.assert_not_called()
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id=? ORDER BY id DESC LIMIT 1",
            (arm["id"],),
        )
        self.assertEqual(event["event_type"], "claude.restart_paused_for_pipeline_drain")

    def test_pause_claude_sessions_preserves_completed_peer_and_failure_count(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',development_failure_count=1 WHERE id=?",
            (pair["id"],),
        )
        stamp = now_iso()
        for arm, status, commit in (("A", "developing", ""), ("B", "completed", "b" * 40)):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-pause-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 status, commit, stamp, stamp),
            )
        def archive(arm, reason, **kwargs):
            self.db.audit("claude.failed_attempt_archived", "arm_run", arm["id"], {"archive": str(self.root / "archive")})
            return arm
        with patch.object(self.service.claude, "archive_failed_attempt", side_effect=archive) as archived:
            result = self.service.pause_claude_sessions(pair["id"])
        self.assertEqual(len(result["stopped"]), 1)
        self.assertEqual(result["errors"], [])
        archived.assert_called_once()
        self.assertEqual(self.db.one("SELECT status FROM arm_runs WHERE id='arm-pause-A'")["status"], "paused")
        peer = self.db.one("SELECT status,commit_sha FROM arm_runs WHERE id='arm-pause-B'")
        self.assertEqual((peer["status"], peer["commit_sha"]), ("completed", "b" * 40))
        current = self.db.one("SELECT status,stage,development_failure_count FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual((current["status"], current["stage"], current["development_failure_count"]),
                         ("paused", "manual_pause", 1))

    def test_only_twice_reproduced_hard_bug_converts_to_task(self):
        self.insert_ready_task()
        self.db.execute("UPDATE tasks SET stack='Python 3.12, FastAPI, pytest, Docker' WHERE id='task-1'")
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,model,image,
               status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("arm-a", pair["id"], "A", "A", str(self.root), "container", "screen", "auto_model/urm",
             "image", "completed", "abc123", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,preconditions,
               reproduction_steps_json,actual_result,expected_result,reproduce_count,difficulty,
               difficulty_evidence_json,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("bug-1", pair["id"], "A", "abc123", "Concurrent commit loses update", "two clients",
             '["send two requests"]', "one update disappears", "both updates persist", 2, "困难",
             '["并发事务","异常恢复"]', "reproduced", stamp, stamp),
        )
        self.db.execute(
            """UPDATE bug_candidates SET estimated_module_count=3,
                 estimated_source_lines_min=80,estimated_source_lines_max=180,
                 estimated_minutes_min=45,estimated_minutes_max=90,
                 complexity_axes_json=? WHERE id='bug-1'""",
            ('["并发一致性","持久化"]',),
        )
        old_template = (
            "Concurrent commit loses update\n\n前置条件：two clients\n\n复现步骤：send two requests\n\n"
            "实际结果：one update disappears\n\n预期结果：both updates persist\n\n"
            "请修复该问题，保留现有 Docker Compose 启动与验收链路，并补充覆盖复现路径的自动化验收。"
        )
        natural_prompt = (
            "两个客户端从同一版本同时提交更新时，服务端目前会让后到的提交覆盖先到结果，"
            "最终只能看到一份修改。这个现象已经用 send two requests 在两次独立环境中重复确认，"
            "实际输出都是 one update disappears，而产品约定要求 both updates persist。\n\n"
            "请沿着并发提交时的读取、版本判断和写入链路修正一致性处理，使两个互不冲突的更新都能保存；"
            "发生真实字段冲突时仍需返回现有冲突响应，不能通过串行覆盖来掩盖问题。不要改变当前对外接口。"
            "仓库外验收会在清洁环境中让两个客户端基于同一版本同步提交，"
            "核对两次请求结果与最终持久化内容，并再次运行已有冲突场景，确认 both updates persist 且旧行为没有回退。"
        )
        with patch.object(self.service.codex, "run", side_effect=[
            {"prompt": old_template, "evidenceUsed": ["preconditions", "steps", "actual"]},
            {"prompt": natural_prompt, "evidenceUsed": ["preconditions", "steps", "actual", "expected"]},
            {"accepted": True, "difficulty": "困难", "difficultyEvidence": [
                "并发版本检查必须保持事务一致性", "准确基线仍会覆盖先到的更新",
            ], "banned": False, "duplicate": False, "baselineReady": True, "reason": "通过"},
        ]) as generated, patch.object(
            self.service, "_create_isolated_bug_baseline",
            return_value=(self.root, "d" * 40),
        ):
            task = self.service.convert_bug_to_task("bug-1")
        self.assertEqual(task["task_type"], "bugfix")
        self.assertEqual(task["difficulty"], "困难")
        self.assertEqual(task["estimated_module_count"], 3)
        self.assertEqual((task["estimated_source_lines_min"], task["estimated_source_lines_max"]), (80, 180))
        self.assertEqual((task["estimated_minutes_min"], task["estimated_minutes_max"]), (45, 90))
        self.assertEqual(json.loads(task["complexity_axes_json"]), ["并发一致性", "持久化"])
        self.assertEqual(task["parent_pair_id"], pair["id"])
        self.assertEqual(task["stack"], "Python 3.12, FastAPI")
        self.assertEqual(task["status"], "ready")
        self.assertNotIn("前置条件：", task["prompt"])
        self.assertNotIn("复现步骤：", task["prompt"])
        self.assertNotIn("请修复该问题，保留现有 Docker Compose", task["prompt"])
        self.assertIn("send two requests", task["prompt"])
        self.assertIn("both updates persist", task["prompt"])
        self.assertLessEqual(len(task["prompt"]), 1200)
        self.assertNotIn("```", task["prompt"])
        command_prompt = natural_prompt + "\n```sh\ndocker compose exec api python -c 'print(1)'\n```"
        self.assertIn("公开题面不得包含代码块或完整复现命令", self.service._bugfix_prompt_issues(command_prompt))
        self.assertEqual(generated.call_count, 3)
        self.assertIn("上一次草稿存在的问题", generated.call_args_list[1].args[1])
        self.assertNotIn("最终题面必须自然出现“自动化验收”或“自动化测试”", generated.call_args_list[0].args[1])
        self.assertFalse(self.service._retire_outdated_ready_bug_task(task))

        self.db.execute("UPDATE tasks SET prompt=? WHERE id=?", (old_template, task["id"]))
        self.assertEqual(self.service._retire_outdated_ready_bug_tasks(), 1)
        self.assertEqual(self.db.one("SELECT status FROM tasks WHERE id=?", (task["id"],))["status"], "rejected")
        self.assertEqual(self.db.one("SELECT status FROM bug_candidates WHERE id='bug-1'")["status"], "reproduced")

        with patch.object(self.service.codex, "run", side_effect=[{
            "prompt": natural_prompt,
            "evidenceUsed": ["preconditions", "steps", "actual", "expected"],
        }, {"accepted": True, "difficulty": "困难", "difficultyEvidence": [
            "响应必须与事务快照一致", "准确基线的并发读取会混合两个版本",
        ], "banned": False, "duplicate": False, "baselineReady": True, "reason": "通过"}]), patch.object(
            self.service, "_create_isolated_bug_baseline",
            return_value=(self.root, "d" * 40),
        ):
            regenerated = self.service.convert_bug_to_task("bug-1")
        self.assertEqual(regenerated["id"], task["id"])
        self.assertEqual(regenerated["status"], "ready")
        self.assertEqual(regenerated["prompt"], natural_prompt)

    def test_reworked_bug_review_excludes_its_previous_rejected_task(self):
        candidate = {"id": "bug-rework", "source_pair_id": "pair-source"}
        review = {
            "accepted": True, "difficulty": "困难",
            "difficultyEvidence": ["跨模块状态裁决", "基线缺少一致性保证"],
            "banned": False, "duplicate": False, "baselineReady": True, "reason": "通过",
        }
        with patch.object(self.service, "_task_duplicate_context", return_value=[]) as context, \
             patch.object(self.service, "_task_baseline_evidence", return_value="固定基线"), \
             patch.object(self.service.codex, "run", return_value=review):
            self.service._independent_bug_difficulty_review(
                candidate, "task-rework", "题目", "新题面", "纯后端", self.root, "a" * 40,
            )
        self.assertEqual(context.call_args.kwargs["exclude_task_id"], "task-rework")

    def test_bug_task_uses_concrete_artifact_stack_instead_of_source_task_metadata(self):
        self.insert_ready_task()
        self.db.execute("UPDATE tasks SET stack='Go 1.25, Gin' WHERE id='task-1'")
        pair = self.service.create_pair("task-1")
        workspace = self.root / "python-artifact"
        workspace.mkdir()
        (workspace / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
        (workspace / "requirements.txt").write_text("fastapi==0.116.1\n", encoding="utf-8")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,model,image,
               status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("arm-python", pair["id"], "A", "A", str(workspace), "container", "screen", "auto_model/urm",
             "image", "completed", "abc123", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,preconditions,
               reproduction_steps_json,reproduction_commands_json,reproduction_results_json,
               actual_result,expected_result,reproduce_count,difficulty,difficulty_evidence_json,
               estimated_module_count,estimated_source_lines_min,estimated_source_lines_max,
               estimated_minutes_min,estimated_minutes_max,complexity_axes_json,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("bug-python-stack", pair["id"], "A", "abc123", "Cross-module state race", "two workers",
             '["run requests"]', '[{"composeArgs":["exec","api","python","-c","pass"]}]',
             '[{"passed":true},{"passed":true}]', "mixed state", "one snapshot", 2, "困难",
             '["并发一致性","持久化"]', 3, 90, 150, 60, 90, '["并发一致性","持久化"]',
             "reproduced", stamp, stamp),
        )
        natural_prompt = (
            "两个工作进程并发读取时会返回来自不同事务快照的状态：响应中的版本来自轮换后的记录，"
            "明细却仍属于轮换前的列表。该现象已在两套清洁环境稳定复现，两次都能得到版本与明细"
            "互相矛盾的成功响应；单进程顺序调用则不会出现。修复后，每次响应中的版本和全部明细"
            "必须来自同一已提交事务快照，同时保留既有写入幂等、冲突响应和重启恢复语义。"
            "沿用仓库现有启动行为与对外接口。自动化验收会启动两个"
            "工作进程，先写入初始状态，再并发执行轮换与状态读取，循环核对每个成功响应的版本、键集合"
            "和审计摘要；任一字段跨越事务边界即以非零退出。验收还会重复正常顺序读写、重复请求、"
            "真实冲突及服务重启场景，确认既有接口字段、状态码和持久化数据均保持不变。"
        )
        with patch.object(self.service.codex, "run", side_effect=[{
            "prompt": natural_prompt,
            "evidenceUsed": ["preconditions", "steps", "actual", "expected"],
        }, {"accepted": True, "difficulty": "困难", "difficultyEvidence": [
            "并发读取须保持事务快照一致", "准确基线会混合轮换前后的版本",
        ], "banned": False, "duplicate": False, "baselineReady": True, "reason": "通过"}]), patch.object(
            self.service, "_create_isolated_bug_baseline",
            return_value=(self.root, "d" * 40),
        ):
            task = self.service.convert_bug_to_task("bug-python-stack")
        self.assertEqual(task["stack"], "Python 3.13, FastAPI")

    def test_arm_delivery_is_squashed_to_one_commit_on_baseline(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        workspace = self.root / "delivery-A"
        remote = self.root / "delivery.git"
        workspace.mkdir()
        run_command(["git", "init", "-b", "A"], cwd=workspace)
        run_command(["git", "config", "user.name", "Test"], cwd=workspace)
        run_command(["git", "config", "user.email", "test@example.com"], cwd=workspace)
        (workspace / "app.py").write_text("print('baseline')\n", encoding="utf-8")
        run_command(["git", "add", "app.py"], cwd=workspace)
        run_command(["git", "commit", "-m", "baseline"], cwd=workspace)
        baseline = run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()
        (workspace / "app.py").write_text("print('first')\n", encoding="utf-8")
        run_command(["git", "commit", "-am", "first local commit"], cwd=workspace)
        (workspace / "feature.py").write_text("ENABLED = True\n", encoding="utf-8")
        run_command(["git", "add", "feature.py"], cwd=workspace)
        run_command(["git", "commit", "-m", "second local commit"], cwd=workspace)
        dependency = workspace / ".venv" / "lib" / "python3.13" / "site-packages" / "helper.py"
        dependency.parent.mkdir(parents=True)
        dependency.write_text("GENERATED = True\n", encoding="utf-8")
        run_command(["git", "add", "-f", ".venv"], cwd=workspace)
        run_command(["git", "commit", "-m", "accidentally commit local environment"], cwd=workspace)
        run_command(["git", "init", "--bare", str(remote)])
        run_command(["git", "remote", "add", "origin", str(remote)], cwd=workspace)
        run_command(["git", "push", "origin", "%s:refs/heads/A" % baseline], cwd=workspace)
        stamp = now_iso()
        self.db.execute("UPDATE pairs SET baseline_sha=? WHERE id=?", (baseline, pair["id"]))
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,remote_url,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?, 'ready',?,?)""",
            ("repo-squash", pair["id"], "owner", "repo", "public", str(self.root), str(remote),
             baseline, baseline, baseline, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'checkpointing',?,?)""",
            ("arm-squash-A", pair["id"], "A", "A", str(workspace), "container", "screen",
             "auto_model/urm", "image", stamp, stamp),
        )

        def local_git(args, cwd=None, timeout=180, check=True):
            return run_command(["git"] + list(args), cwd=cwd, timeout=timeout, check=check)

        with patch.object(self.service.git, "_github_git", side_effect=local_git):
            delivered = self.service.git.push_arm(pair["id"], "A")
        parent = run_command(["git", "rev-parse", delivered + "^"], cwd=workspace).stdout.strip()
        self.assertEqual(parent, baseline)
        self.assertEqual(run_command(["git", "rev-list", "--count", baseline + ".." + delivered], cwd=workspace).stdout.strip(), "1")
        self.assertEqual((workspace / "app.py").read_text(encoding="utf-8"), "print('first')\n")
        self.assertTrue((workspace / "feature.py").is_file())
        self.assertTrue(dependency.is_file())
        delivered_files = run_command(
            ["git", "diff", "--name-only", baseline + ".." + delivered], cwd=workspace,
        ).stdout.splitlines()
        self.assertNotIn(".venv/lib/python3.13/site-packages/helper.py", delivered_files)
        self.assertIn(".venv/", (workspace / ".git" / "info" / "exclude").read_text(encoding="utf-8"))

    def test_solo_qa_blocks_generated_dependencies_and_under_ten_source_lines(self):
        workspace = self.root / "delivery-preflight"
        workspace.mkdir()
        run_command(["git", "init", "-b", "main"], cwd=workspace)
        run_command(["git", "config", "user.name", "Test"], cwd=workspace)
        run_command(["git", "config", "user.email", "test@example.com"], cwd=workspace)
        (workspace / "README.md").write_text("baseline\n", encoding="utf-8")
        run_command(["git", "add", "README.md"], cwd=workspace)
        run_command(["git", "commit", "-m", "baseline"], cwd=workspace)
        baseline = run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()

        app = workspace / "app.py"
        app.write_text("\n".join("VALUE_%d = %d" % (index, index) for index in range(12)) + "\n", encoding="utf-8")
        dependency = workspace / ".venv" / "lib" / "site-packages" / "dependency.py"
        dependency.parent.mkdir(parents=True)
        dependency.write_text("generated\n", encoding="utf-8")
        run_command(["git", "add", "-f", "app.py", ".venv"], cwd=workspace)
        run_command(["git", "commit", "-m", "polluted A"], cwd=workspace)
        a_sha = run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()

        run_command(["git", "reset", "--hard", baseline], cwd=workspace)
        app.write_text("VALUE = 1\n", encoding="utf-8")
        run_command(["git", "add", "app.py"], cwd=workspace)
        run_command(["git", "commit", "-m", "tiny B"], cwd=workspace)
        b_sha = run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()

        summary = self.service.git.delivery_diff_summary(workspace, baseline, a_sha)
        self.assertEqual(summary["source_line_changes"], 12)
        self.assertEqual(summary["generated_roots"], [".venv"])
        _, _, issues = self.service._solo_qa_material({
            "task": {
                "task_type": "zero_to_one", "difficulty": "困难",
                "prompt": "Build a hard project", "stack": "Python, FastAPI",
            },
            "repository": {
                "remote_url": "https://github.com/example/project.git", "main_sha": baseline,
            },
            "arms": [
                {"arm": "A", "commit_sha": a_sha, "workspace_path": str(workspace)},
                {"arm": "B", "commit_sha": b_sha, "workspace_path": str(workspace)},
            ],
            "checks": [], "recordings": [], "gsb": {},
        })
        self.assertTrue(any("A 产物快照包含生成依赖目录" in issue for issue in issues))
        self.assertTrue(any("B 相对初始环境的有效源码改动仅 1 行" in issue for issue in issues))

    def test_legacy_repair_chain_is_normalized_without_changing_the_tree(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        workspace = self.root / "legacy-repair-A"
        remote = self.root / "legacy-repair.git"
        workspace.mkdir()
        run_command(["git", "init", "-b", "A"], cwd=workspace)
        run_command(["git", "config", "user.name", "Test"], cwd=workspace)
        run_command(["git", "config", "user.email", "test@example.com"], cwd=workspace)
        (workspace / "app.py").write_text("print('baseline')\n", encoding="utf-8")
        run_command(["git", "add", "app.py"], cwd=workspace)
        run_command(["git", "commit", "-m", "baseline"], cwd=workspace)
        baseline = run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()
        (workspace / "app.py").write_text("print('delivered')\n", encoding="utf-8")
        run_command(["git", "commit", "-am", "deliver"], cwd=workspace)
        (workspace / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        run_command(["git", "add", "compose.yml"], cwd=workspace)
        run_command(["git", "commit", "-m", "repair docker"], cwd=workspace)
        old_sha = run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()
        old_tree = run_command(["git", "rev-parse", "HEAD^{tree}"], cwd=workspace).stdout.strip()
        run_command(["git", "init", "--bare", str(remote)])
        run_command(["git", "remote", "add", "origin", str(remote)], cwd=workspace)
        run_command(["git", "push", "origin", "A"], cwd=workspace)
        stamp = now_iso()
        self.db.execute("UPDATE pairs SET baseline_sha=? WHERE id=?", (baseline, pair["id"]))
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,remote_url,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?, 'ready',?,?)""",
            ("repo-normalize", pair["id"], "owner", "repo", "public", str(self.root), str(remote),
             baseline, old_sha, baseline, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-normalize-A", pair["id"], "A", "A", str(workspace), "container", "screen",
             "auto_model/urm", "image", old_sha, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?, 'passed',?,?)""",
            ("check-normalize-A", pair["id"], "A", old_sha, stamp, stamp),
        )

        def local_git(args, cwd=None, timeout=180, check=True):
            return run_command(["git"] + list(args), cwd=cwd, timeout=timeout, check=check)

        arm = self.db.one("SELECT arm,commit_sha,workspace_path FROM arm_runs WHERE id='arm-normalize-A'")
        with patch.object(self.service.git, "_github_git", side_effect=local_git):
            new_sha = self.service._normalize_delivery_arm_snapshot(pair["id"], arm)

        self.assertNotEqual(new_sha, old_sha)
        self.assertEqual(run_command(["git", "rev-parse", new_sha + "^"], cwd=workspace).stdout.strip(), baseline)
        self.assertEqual(run_command(["git", "rev-parse", new_sha + "^{tree}"], cwd=workspace).stdout.strip(), old_tree)
        self.assertEqual(self.db.one("SELECT commit_sha FROM arm_runs WHERE id='arm-normalize-A'")["commit_sha"], new_sha)
        self.assertEqual(self.db.one("SELECT commit_sha FROM artifact_checks WHERE id='check-normalize-A'")["commit_sha"], new_sha)
        self.assertEqual(self.db.one("SELECT a_sha FROM git_repositories WHERE id='repo-normalize'")["a_sha"], new_sha)

    def test_lineage_normalize_api_restores_pair_without_redevelopment(self):
        handler = Handler.__new__(Handler)
        service = MagicMock()
        result = {"pair_id": "pair-test", "normalized": {"A": "a" * 40}}
        service.normalize_delivery_lineage.return_value = result
        handler.server = MagicMock(service=service)
        handler._path_query = MagicMock(return_value=("/api/pairs/pair-test/lineage/normalize", {}))
        handler._body = MagicMock(return_value={})
        handler._json = MagicMock()

        handler.do_POST()

        service.normalize_delivery_lineage.assert_called_once_with("pair-test")
        handler._json.assert_called_once_with(200, result)

    def test_arm_repository_is_imported_only_after_empty_container_start(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        source = self.root / "source-A"
        destination = self.root / "runtime-A"
        source.mkdir()
        destination.mkdir()
        run_command(["git", "init", "-b", "A"], cwd=source)
        run_command(["git", "config", "user.name", "Test"], cwd=source)
        run_command(["git", "config", "user.email", "test@example.com"], cwd=source)
        (source / "README.md").write_text("baseline\n", encoding="utf-8")
        run_command(["git", "add", "README.md"], cwd=source)
        run_command(["git", "commit", "-m", "baseline"], cwd=source)
        expected_sha = run_command(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
        arm = self.service.claude.prepare_arm(pair, "A", destination)

        with patch.object(self.service.claude, "_container_running", return_value=True):
            self.service.claude.materialize_repository(arm, source, expected_sha)

        self.assertEqual((destination / "README.md").read_text(encoding="utf-8"), "baseline\n")
        self.assertEqual(run_command(["git", "branch", "--show-current"], cwd=destination).stdout.strip(), "A")
        self.assertEqual(run_command(["git", "status", "--porcelain"], cwd=destination).stdout.strip(), "")
        (destination / "app.py").write_text("print('ready')\n", encoding="utf-8")
        run_command(["git", "add", "app.py"], cwd=destination)
        run_command(["git", "commit", "-m", "implementation"], cwd=destination)
        self.assertTrue(self.service.claude.has_business_code(destination, expected_sha))
        run_command(["git", "reset", "--hard", expected_sha], cwd=destination)
        nested = destination / "untracked-package"
        nested.mkdir()
        (nested / "worker.py").write_text("print('work')\n", encoding="utf-8")
        self.assertTrue(self.service.claude.has_business_code(destination, expected_sha))

    def test_dependency_and_build_directories_do_not_count_as_business_code(self):
        workspace = self.root / "dependency-only"
        workspace.mkdir()
        run_command(["git", "init", "-b", "A"], cwd=workspace)
        run_command(["git", "config", "user.name", "Test"], cwd=workspace)
        run_command(["git", "config", "user.email", "test@example.com"], cwd=workspace)
        (workspace / "README.md").write_text("baseline\n", encoding="utf-8")
        run_command(["git", "add", "README.md"], cwd=workspace)
        run_command(["git", "commit", "-m", "baseline"], cwd=workspace)
        baseline = run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()

        for relative in (
            "backend/.venv/lib/python3.11/site-packages/helper.py",
            "frontend/node_modules/example/index.js",
            "backend/__pycache__/main.py",
            "frontend/dist/assets/app.js",
        ):
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("generated\n", encoding="utf-8")
        self.assertFalse(self.service.claude.has_business_code(workspace, baseline))
        self.assertEqual(
            self.service.claude.business_progress(workspace, baseline)["paths"], [],
        )

        source = workspace / "backend" / "app" / "main.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("print('business code')\n", encoding="utf-8")
        self.assertTrue(self.service.claude.has_business_code(workspace, baseline))
        progress = self.service.claude.business_progress(workspace, baseline)
        self.assertIn("backend/app/main.py", progress["paths"])
        self.assertGreater(progress["last_modified"], 0)

    def test_node_module_sources_count_as_business_code(self):
        workspace = self.root / "node-module-source"
        workspace.mkdir()
        run_command(["git", "init", "-b", "A"], cwd=workspace)
        run_command(["git", "config", "user.name", "Test"], cwd=workspace)
        run_command(["git", "config", "user.email", "test@example.com"], cwd=workspace)
        source = workspace / "src" / "parse.mjs"
        source.parent.mkdir()
        source.write_text("export const parse = () => 1;\n", encoding="utf-8")
        run_command(["git", "add", "src/parse.mjs"], cwd=workspace)
        run_command(["git", "commit", "-m", "baseline"], cwd=workspace)
        baseline = run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()
        source.write_text("export const parse = () => 2;\n", encoding="utf-8")

        progress = self.service.claude.business_progress(workspace, baseline)
        self.assertTrue(progress["has_code"])
        self.assertIn("src/parse.mjs", progress["paths"])

    def test_docstring_only_package_initializer_is_not_business_code(self):
        workspace = self.root / "package-scaffold-only"
        workspace.mkdir()
        run_command(["git", "init", "-b", "A"], cwd=workspace)
        run_command(["git", "config", "user.name", "Test"], cwd=workspace)
        run_command(["git", "config", "user.email", "test@example.com"], cwd=workspace)
        (workspace / "README.md").write_text("baseline\n", encoding="utf-8")
        run_command(["git", "add", "README.md"], cwd=workspace)
        run_command(["git", "commit", "-m", "baseline"], cwd=workspace)
        baseline = run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()
        package = workspace / "app"
        package.mkdir()
        (package / "__init__.py").write_text('"""Application package."""\n', encoding="utf-8")
        self.assertFalse(self.service.claude.has_business_code(workspace, baseline))
        (package / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        self.assertTrue(self.service.claude.has_business_code(workspace, baseline))

    def test_tests_and_reproduction_helpers_do_not_count_as_business_code(self):
        workspace = self.root / "diagnostic-only"
        workspace.mkdir()
        run_command(["git", "init", "-b", "A"], cwd=workspace)
        run_command(["git", "config", "user.name", "Test"], cwd=workspace)
        run_command(["git", "config", "user.email", "test@example.com"], cwd=workspace)
        (workspace / "README.md").write_text("baseline\n", encoding="utf-8")
        run_command(["git", "add", "README.md"], cwd=workspace)
        run_command(["git", "commit", "-m", "baseline"], cwd=workspace)
        baseline = run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()

        (workspace / "repro.py").write_text("print('diagnostic')\n", encoding="utf-8")
        (workspace / "repro_perf.py").write_text("print('benchmark')\n", encoding="utf-8")
        (workspace / "reproduce_case.sh").write_text("echo diagnostic\n", encoding="utf-8")
        (workspace / "proto_search.py").write_text("print('prototype')\n", encoding="utf-8")
        (workspace / "scratch_benchmark.py").write_text("print('experiment')\n", encoding="utf-8")
        test_source = workspace / "tests" / "test_regression.py"
        test_source.parent.mkdir()
        test_source.write_text("def test_regression(): pass\n", encoding="utf-8")

        progress = self.service.claude.business_progress(workspace, baseline)
        self.assertFalse(progress["has_code"])
        self.assertEqual(progress["paths"], [])

    def test_stalled_504_retry_spinner_is_detected_after_grace_period(self):
        arm = {"id": "arm-stalled-504"}
        trace = self.root / "stalled.jsonl"
        trace.write_text('{"type":"tool_result"}\n', encoding="utf-8")
        old = time.time() - 701
        os.utime(trace, (old, old))
        runtime = self.service.claude.runtime_dir / arm["id"]
        runtime.mkdir(parents=True)
        terminal = runtime / "terminal.log"
        terminal.write_text(
            "API error · Retrying in 3s · attempt 3/10\n504 Gateway Time-out\n",
            encoding="utf-8",
        )
        self.assertIn("504", self.service.claude.stalled_gateway_timeout(arm, str(trace), 120))
        os.utime(trace, None)
        self.assertEqual(self.service.claude.stalled_gateway_timeout(arm, str(trace), 120), "")

    def test_stalled_504_ignores_missing_trace_path(self):
        arm = {"id": "arm-stalled-504-without-trace"}
        runtime = self.service.claude.runtime_dir / arm["id"]
        runtime.mkdir(parents=True)
        terminal = runtime / "terminal.log"
        terminal.write_text(
            "API error · Retrying in 3s · attempt 3/10\n504 Gateway Time-out\n",
            encoding="utf-8",
        )
        self.assertEqual(self.service.claude.stalled_gateway_timeout(arm, "", 120), "")
        self.assertEqual(
            self.service.claude.stalled_gateway_timeout(arm, str(runtime / "missing.jsonl"), 120),
            "",
        )

    def test_internal_no_visible_output_companion_is_not_a_human_followup(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-meta-companion", "container_name": "container-meta-companion"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": ""}],
            }},
            {"type": "user", "isMeta": True, "turnCompanion": True, "message": {
                "content": "[Your previous response had no visible output. Please continue.]",
            }},
            {"type": "assistant", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "Completed"}],
            }},
            {"type": "system", "subtype": "turn_duration"},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertTrue(state["complete"])
        self.assertEqual(state["result"], "Completed")
        self.assertFalse(state["followup_detected"])

    def test_terminal_close_timeout_does_not_break_cleanup(self):
        metadata = self.root / "terminal-window.json"
        metadata.write_text(
            json.dumps({"window_id": "123", "tty": "/dev/ttys001", "title": "pair-screen"}),
            encoding="utf-8",
        )
        with patch(
            "pairwise_console.claude_runner.run_command",
            side_effect=subprocess.TimeoutExpired(["osascript"], 5),
        ):
            self.service.claude._close_terminal_window(metadata, "pair-screen")
        self.assertFalse(metadata.exists())

    def test_terminal_close_matches_owned_window_even_when_tab_is_busy(self):
        metadata = self.root / "terminal-window.json"
        metadata.write_text(
            json.dumps({"window_id": "123", "tty": "/dev/ttys001", "title": "pair-screen"}),
            encoding="utf-8",
        )
        with patch("pairwise_console.claude_runner.run_command") as command:
            self.service.claude._close_terminal_window(metadata, "pair-screen")
        script = command.call_args.args[0][-1]
        self.assertIn('name of w contains "pair-screen"', script)
        self.assertIn('tty of t as text) is "/dev/ttys001"', script)
        self.assertNotIn("not busy of t", script)
        self.assertFalse(metadata.exists())

    def test_manual_difficulty_edit_preserves_automatic_review_evidence(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        evidence = json.dumps(["后续产物检查显示跨模块状态恢复"], ensure_ascii=False)
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO difficulty_reviews(
                 id,pair_id,original_difficulty,a_difficulty,b_difficulty,assessed_difficulty,
                 reason,evidence_json,a_commit_sha,b_commit_sha,status,reviewed_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?, 'passed',?,?,?)""",
            ("difficulty-manual", pair["id"], "困难", "困难", "中等", "中等",
             "自动复评认为修改面较窄", evidence, "a" * 40, "b" * 40,
             stamp, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,payload_sha256,created_at,updated_at)
               VALUES(?,?,'ready_to_submit','old-payload',?,?)""",
            ("delivery-manual-difficulty", pair["id"], stamp, stamp),
        )

        result = self.service.edit_pair_difficulty(
            pair["id"], "地狱", "人工复核确认存在耦合状态机",
        )

        review = result["difficulty_review"]
        self.assertEqual(result["task"]["difficulty"], "地狱")
        self.assertEqual(review["assessed_difficulty"], "地狱")
        self.assertEqual(review["a_difficulty"], "困难")
        self.assertEqual(review["b_difficulty"], "中等")
        self.assertEqual(review["evidence_json"], evidence)
        self.assertIn("原自动复评：自动复评认为修改面较窄", review["reason"])
        self.assertEqual(
            self.db.one("SELECT payload_sha256 FROM delivery_submissions WHERE pair_id=?", (pair["id"],))["payload_sha256"],
            "",
        )
        self.assertIsNotNone(self.db.one(
            "SELECT id FROM audit_events WHERE event_type='difficulty.manually_edited' AND entity_id=?",
            (pair["id"],),
        ))

    def test_manual_difficulty_edit_rejects_platform_bound_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,remote_id,submitted_at,created_at,updated_at)
               VALUES(?,?,'qc_pending','remote-12',?,?,?)""",
            ("delivery-locked", pair["id"], stamp, stamp, stamp),
        )
        with self.assertRaisesRegex(ValueError, "已提交或已绑定"):
            self.service.edit_pair_difficulty(pair["id"], "地狱", "不应生效")
        self.assertEqual(self.db.one("SELECT difficulty FROM tasks WHERE id='task-1'")["difficulty"], "困难")

    def test_manual_arm_requeue_resets_only_selected_side_and_waits_for_capacity(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        baseline = "c" * 40
        root = self.root / "manual-arm-requeue"
        root.mkdir()
        self.db.execute(
            """UPDATE pairs SET status='completed',stage='completed',baseline_sha=?,
               development_failure_count=2,completed_at=?,updated_at=? WHERE id=?""",
            (baseline, stamp, stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-manual-arm", pair["id"], "owner", "repo", "public", str(root),
             baseline, baseline, baseline, stamp, stamp),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,session_id,prompt_id,trace_path,commit_sha,
                   error_retry_count,api_retry_count,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,2,3,?,?)""",
                ("arm-manual-" + arm, pair["id"], arm, arm, str(root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 "session-" + arm, "prompt-" + arm, "/trace/" + arm,
                 arm.lower() * 40, stamp, stamp),
            )

        def archive_selected(arm, _reason, **_kwargs):
            self.db.execute(
                """UPDATE arm_runs SET session_id='',prompt_id='',trace_path='',commit_sha='',
                   prompt_sent_at=NULL,finished_at=NULL,status='waiting_retry' WHERE id=?""",
                (arm["id"],),
            )
            return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],))

        with patch.object(self.service.claude, "archive_failed_attempt", side_effect=archive_selected) as archive, \
             patch.object(self.service.git, "reset_arm_to_baseline") as reset, \
             patch.object(self.service, "_resume_one_manual_arm_requeue", return_value=False):
            result = self.service.queue_arm_manually(pair["id"], "B")

        archive.assert_called_once()
        reset.assert_called_once_with(pair["id"], "B")
        states = {row["arm"]: row for row in self.db.all(
            "SELECT arm,status,session_id,commit_sha FROM arm_runs WHERE pair_id=? ORDER BY arm",
            (pair["id"],),
        )}
        self.assertEqual(states["A"]["status"], "completed")
        self.assertEqual(states["A"]["session_id"], "session-A")
        self.assertEqual(states["A"]["commit_sha"], "a" * 40)
        self.assertEqual(states["B"]["status"], "manual_waiting")
        self.assertFalse(states["B"]["session_id"])
        self.assertFalse(states["B"]["commit_sha"])
        self.assertEqual(result["status"], "repair_pending")
        self.assertEqual(result["stage"], "manual_arm_requeue_pending")

        with patch.object(self.service, "_submit_monitor") as submit:
            self.assertTrue(self.service._resume_one_manual_arm_requeue(pair["id"]))
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[3], "arm-manual-B")
        current = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "running", "stage": "development"})
        states = {row["arm"]: row["status"] for row in self.db.all(
            "SELECT arm,status FROM arm_runs WHERE pair_id=?", (pair["id"],),
        )}
        self.assertEqual(states, {"A": "completed", "B": "queued"})

    def test_manual_arm_requeue_recovers_paused_side_without_resetting_failure_budget(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        baseline = "c" * 40
        root = self.root / "paused-arm-requeue"
        root.mkdir()
        self.db.execute(
            """UPDATE pairs SET status='paused',stage='manual_pause',baseline_sha=?,
               development_failure_count=1,updated_at=? WHERE id=?""",
            (baseline, stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-paused-arm", pair["id"], "owner", "repo", "public", str(root),
             baseline, "a" * 40, baseline, stamp, stamp),
        )
        for arm, status, commit in (("A", "completed", "a" * 40),
                                    ("B", "infrastructure_paused", "")):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,session_id,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-paused-" + arm, pair["id"], arm, arm, str(root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 status, "session-" + arm if arm == "A" else "", commit, stamp, stamp),
            )

        def archive_selected(arm, _reason, **_kwargs):
            self.db.execute(
                """UPDATE arm_runs SET status='queued',session_id='',commit_sha='',
                   prompt_sent_at=NULL WHERE id=?""", (arm["id"],),
            )

        with patch.object(self.service.claude, "archive_failed_attempt", side_effect=archive_selected), \
             patch.object(self.service.git, "reset_arm_to_baseline") as reset, \
             patch.object(self.service, "_resume_one_manual_arm_requeue", return_value=False):
            result = self.service.queue_arm_manually(pair["id"], "B")
        reset.assert_called_once_with(pair["id"], "B")
        self.assertEqual((result["status"], result["stage"]),
                         ("repair_pending", "manual_arm_requeue_pending"))
        self.assertEqual(result["development_failure_count"], 1)
        states = {row["arm"]: row for row in self.db.all(
            "SELECT arm,status,session_id,commit_sha FROM arm_runs WHERE pair_id=?", (pair["id"],),
        )}
        self.assertEqual((states["A"]["status"], states["A"]["session_id"], states["A"]["commit_sha"]),
                         ("completed", "session-A", "a" * 40))
        self.assertEqual(states["B"]["status"], "manual_waiting")
        event = self.db.one(
            """SELECT detail_json FROM audit_events WHERE event_type='claude.manual_arm_requeue_queued'
               AND entity_id='arm-paused-B' ORDER BY id DESC LIMIT 1""",
        )
        self.assertFalse(json.loads(event["detail_json"])["failureBudgetReset"])

    def test_manual_arm_requeue_recovers_infrastructure_paused_side_in_running_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        baseline = "c" * 40
        root = self.root / "running-infrastructure-paused-arm"
        root.mkdir()
        self.db.execute(
            """UPDATE pairs SET status='running',stage='development',baseline_sha=?,
               development_failure_count=1,updated_at=? WHERE id=?""",
            (baseline, stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-infra-paused-arm", pair["id"], "owner", "repo", "public", str(root),
             baseline, baseline, "b" * 40, stamp, stamp),
        )
        for arm, status, commit in (("A", "infrastructure_paused", ""),
                                    ("B", "completed", "b" * 40)):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,session_id,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-infra-" + arm, pair["id"], arm, arm, str(root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 status, "session-B" if arm == "B" else "", commit, stamp, stamp),
            )

        def archive_selected(arm, _reason, **_kwargs):
            self.db.execute(
                "UPDATE arm_runs SET status='queued',session_id='',commit_sha='' WHERE id=?",
                (arm["id"],),
            )

        with patch.object(self.service.claude, "archive_failed_attempt", side_effect=archive_selected), \
             patch.object(self.service.git, "reset_arm_to_baseline") as reset, \
             patch.object(self.service, "_resume_one_manual_arm_requeue", return_value=False):
            result = self.service.queue_arm_manually(pair["id"], "A")
        reset.assert_called_once_with(pair["id"], "A")
        self.assertEqual((result["status"], result["stage"]),
                         ("repair_pending", "manual_arm_requeue_pending"))
        self.assertEqual(result["development_failure_count"], 1)
        peer = self.db.one("SELECT status,session_id,commit_sha FROM arm_runs WHERE id='arm-infra-B'")
        self.assertEqual(peer, {"status": "completed", "session_id": "session-B",
                                "commit_sha": "b" * 40})
        event = self.db.one(
            """SELECT detail_json FROM audit_events WHERE event_type='claude.manual_arm_requeue_queued'
               AND entity_id='arm-infra-A' ORDER BY id DESC LIMIT 1""",
        )
        self.assertFalse(json.loads(event["detail_json"])["failureBudgetReset"])

    def test_manual_arm_requeue_recovers_paused_peer_after_other_side_completes(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        baseline, delivered = "c" * 40, "a" * 40
        root = self.root / "running-manually-paused-arm"
        root.mkdir()
        self.db.execute(
            """UPDATE pairs SET status='running',stage='development',baseline_sha=?,
               development_failure_count=1,updated_at=? WHERE id=?""",
            (baseline, stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-manually-paused-arm", pair["id"], "owner", "repo", "public",
             str(root), baseline, delivered, baseline, stamp, stamp),
        )
        for arm, status, commit in (("A", "completed", delivered), ("B", "paused", "")):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,session_id,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-manual-pause-" + arm, pair["id"], arm, arm, str(root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 status, "session-A" if arm == "A" else "", commit, stamp, stamp),
            )

        def archive_selected(arm, _reason, **_kwargs):
            self.db.execute(
                "UPDATE arm_runs SET status='queued',session_id='',commit_sha='' WHERE id=?",
                (arm["id"],),
            )

        with patch.object(self.service.claude, "archive_failed_attempt", side_effect=archive_selected), \
             patch.object(self.service.git, "reset_arm_to_baseline") as reset, \
             patch.object(self.service, "_resume_one_manual_arm_requeue", return_value=False):
            result = self.service.queue_arm_manually(pair["id"], "B")
        reset.assert_called_once_with(pair["id"], "B")
        self.assertEqual((result["status"], result["stage"]),
                         ("repair_pending", "manual_arm_requeue_pending"))
        self.assertEqual(result["development_failure_count"], 1)
        peer = self.db.one(
            "SELECT status,session_id,commit_sha FROM arm_runs WHERE id='arm-manual-pause-A'"
        )
        self.assertEqual(peer, {"status": "completed", "session_id": "session-A",
                                "commit_sha": delivered})
        self.assertEqual(self.db.one(
            "SELECT status FROM arm_runs WHERE id='arm-manual-pause-B'"
        )["status"], "manual_waiting")

    def test_manual_single_arm_requeue_yields_unstarted_pair_reservation(self):
        self.db.set_setting("max_pairs_parallel", 4)
        self.insert_ready_task()
        priority = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """UPDATE pairs SET status='repair_pending',stage='manual_arm_requeue_pending'
                 WHERE id=?""",
            (priority["id"],),
        )
        for arm, status in (("A", "completed"), ("B", "manual_waiting")):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-priority-" + arm, priority["id"], arm, arm, str(self.root),
                 "container-priority-" + arm, "screen-priority-" + arm,
                 "auto_model/urm", "image", status,
                 "a" * 40 if arm == "A" else "", stamp, stamp),
            )
        prepared_id = ""
        for index in range(4):
            task_id = f"task-other-{index}"
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, "test", "zero_to_one", task_id, "Build a hard project", "困难",
                 '["跨模块状态"]', "fingerprint-" + task_id, "ready", stamp, stamp),
            )
            other = self.service.create_pair(task_id)
            if index == 3:
                prepared_id = other["id"]
                self.db.execute(
                    "UPDATE pairs SET stage='ready_to_start',created_at=? WHERE id=?",
                    ("2030-01-01T00:00:00+00:00", prepared_id),
                )
                for arm in ("A", "B"):
                    self.db.execute(
                        """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                           screen_name,model,image,status,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                        ("arm-prepared-" + arm, prepared_id, arm, arm, str(self.root),
                         "container-prepared-" + arm, "screen-prepared-" + arm,
                         "auto_model/urm", "image", stamp, stamp),
                    )
        with patch.object(self.service, "_submit_monitor") as submit:
            self.assertTrue(self.service._resume_one_manual_arm_requeue(priority["id"]))
        submit.assert_called_once()
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (priority["id"],)),
            {"status": "running", "stage": "development"},
        )
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (prepared_id,)),
            {"status": "deferred_priority", "stage": "ready_to_start"},
        )
        self.assertEqual(self.service.automation_status()["activePairs"], 4)
        self.db.execute(
            "UPDATE pairs SET stage='artifact_validation' WHERE id=?", (priority["id"],),
        )
        self.assertTrue(self.service._resume_one_deferred_prepared_pair())
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (prepared_id,)),
            {"status": "queued", "stage": "ready_to_start"},
        )

    def test_unstarted_repository_pair_can_yield_without_losing_task(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.assertTrue(self.service._defer_unstarted_pair_for_manual_requeue_locked("priority-pair"))
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "deferred_priority", "stage": "repository"},
        )
        self.assertEqual(
            self.db.one("SELECT status,locked_by FROM tasks WHERE id='task-1'"),
            {"status": "used", "locked_by": pair["id"]},
        )
        self.assertTrue(self.service._resume_one_deferred_prepared_pair())
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "queued", "stage": "repository"},
        )

    def test_manual_requeue_recovers_failed_side_after_replacement(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        baseline, delivered = "c" * 40, "a" * 40
        root = self.root / "replaced-manual-requeue"
        root.mkdir()
        self.db.execute(
            """UPDATE pairs SET status='failed',stage='replaced',baseline_sha=?,
               development_failure_count=2 WHERE id=?""", (baseline, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-replaced-manual", pair["id"], "owner", "repo", "public",
             str(root), baseline, delivered, baseline, stamp, stamp),
        )
        for arm, status, commit in (("A", "completed", delivered), ("B", "failed", "")):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-replaced-" + arm, pair["id"], arm, arm, str(root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 status, commit, stamp, stamp),
            )
        with self.assertRaisesRegex(ValueError, "另一侧须已完成 Docker 验收"):
            self.service.queue_arm_manually(pair["id"], "B")
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,'passed',?,?)""",
            ("check-replaced-A", pair["id"], "A", delivered, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO codex_jobs(id,pair_id,job_type,model,reasoning_effort,status,
               created_at,updated_at) VALUES(?,?,?,?,?,'running',?,?)""",
            ("job-replaced-discovery", pair["id"], "bug_discovery", "test", "low",
             stamp, stamp),
        )

        def archive_selected(arm, _reason, **_kwargs):
            self.db.execute(
                "UPDATE arm_runs SET status='queued',commit_sha='',session_id='',prompt_sent_at=NULL WHERE id=?",
                (arm["id"],),
            )

        with patch.object(self.service.claude, "archive_failed_attempt", side_effect=archive_selected), \
             patch.object(self.service.git, "reset_arm_to_baseline") as reset, \
             patch.object(self.service, "_resume_one_manual_arm_requeue", return_value=False):
            result = self.service.queue_arm_manually(pair["id"], "B")
        reset.assert_called_once_with(pair["id"], "B")
        self.assertEqual((result["status"], result["stage"]),
                         ("repair_pending", "manual_arm_requeue_pending"))
        arms = {row["arm"]: row for row in self.db.all(
            "SELECT arm,status,commit_sha FROM arm_runs WHERE pair_id=?", (pair["id"],)
        )}
        self.assertEqual((arms["A"]["status"], arms["A"]["commit_sha"]),
                         ("completed", delivered))
        self.assertEqual((arms["B"]["status"], arms["B"]["commit_sha"]),
                         ("manual_waiting", ""))
        self.assertEqual(self.db.one(
            "SELECT status FROM artifact_checks WHERE id='check-replaced-A'"
        )["status"], "passed")

    def test_manual_requeue_one_extra_attempt_preserves_failed_peer_check(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        baseline, delivered = "c" * 40, "a" * 40
        root = self.root / "replaced-approved-requeue"
        root.mkdir()
        self.db.execute(
            """UPDATE pairs SET status='failed',stage='replaced',baseline_sha=?,
               development_failure_count=2 WHERE id=?""", (baseline, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-approved-requeue", pair["id"], "owner", "repo", "public",
             str(root), baseline, delivered, baseline, stamp, stamp),
        )
        for arm, status, commit in (("A", "completed", delivered), ("B", "failed", "")):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,
                   screen_name,model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-approved-" + arm, pair["id"], arm, arm, str(root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 status, commit, stamp, stamp),
            )
        check_id = "check-approved-A"
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,'observed_failed',?,?)""",
            (check_id, pair["id"], "A", delivered, stamp, stamp),
        )
        self.db.audit("pair.manual_arm_requeue_exception_approved", "pair", pair["id"], {
            "arm": "B", "peerCommit": delivered, "peerCheckId": check_id,
            "grant": "one_extra_attempt", "reason": "独立复核发现产物自带验收选择器歧义",
        })
        approval_id = self.db.one(
            "SELECT id FROM audit_events WHERE event_type=? AND entity_id=? ORDER BY id DESC",
            ("pair.manual_arm_requeue_exception_approved", pair["id"]),
        )["id"]

        with self.assertRaisesRegex(ValueError, "另一侧须已完成 Docker 验收"):
            self.service.queue_arm_manually(pair["id"], "B")

        def archive_selected(arm, _reason, **_kwargs):
            self.db.execute(
                "UPDATE arm_runs SET status='queued',commit_sha='',session_id='',prompt_sent_at=NULL WHERE id=?",
                (arm["id"],),
            )

        with patch.object(self.service.claude, "archive_failed_attempt", side_effect=archive_selected), \
             patch.object(self.service.git, "reset_arm_to_baseline") as reset, \
             patch.object(self.service, "_resume_one_manual_arm_requeue", return_value=False):
            result = self.service.queue_arm_manually(pair["id"], "B", approval_id)
        reset.assert_called_once_with(pair["id"], "B")
        self.assertEqual((result["status"], result["stage"]),
                         ("repair_pending", "manual_arm_requeue_pending"))
        self.assertEqual(self.db.one(
            "SELECT development_failure_count FROM pairs WHERE id=?", (pair["id"],)
        )["development_failure_count"], 1)
        self.assertEqual(self.db.one(
            "SELECT status,commit_sha FROM arm_runs WHERE pair_id=? AND arm='A'", (pair["id"],)
        )["commit_sha"], delivered)
        self.assertEqual(self.db.one(
            "SELECT status FROM artifact_checks WHERE id=?", (check_id,)
        )["status"], "observed_failed")
        self.assertTrue(self.db.one(
            """SELECT id FROM audit_events WHERE event_type='pair.manual_arm_requeue_exception_used'
               AND entity_id=? AND json_extract(detail_json,'$.approvalId')=?""",
            (pair["id"], approval_id),
        ))
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='replaced',development_failure_count=2 WHERE id=?",
            (pair["id"],),
        )
        self.db.execute(
            "UPDATE arm_runs SET status='failed' WHERE pair_id=? AND arm='B'",
            (pair["id"],),
        )
        with self.assertRaisesRegex(ValueError, "另一侧须已完成 Docker 验收"):
            self.service.queue_arm_manually(pair["id"], "B", approval_id)

    def test_retry_budget_reset_preserves_sessions_and_commits(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='development_failed',development_failure_count=2 WHERE id=?",
            (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,session_id,commit_sha,error_retry_count,api_retry_count,
               api_retry_after,last_api_error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'failed',?,?,2,4,?,'429',?,?)""",
            ("arm-budget-A", pair["id"], "A", "A", str(self.root / "A"), "container-A",
             "screen-A", "auto_model/urm", "image", "session-kept", "d" * 40,
             stamp, stamp, stamp),
        )
        result = self.service.reset_pair_retries(pair["id"])
        arm = self.db.one("SELECT * FROM arm_runs WHERE id='arm-budget-A'")
        self.assertEqual(result["development_failure_count"], 0)
        self.assertEqual((arm["error_retry_count"], arm["api_retry_count"]), (0, 0))
        self.assertEqual(arm["session_id"], "session-kept")
        self.assertEqual(arm["commit_sha"], "d" * 40)

    def test_retry_budget_reset_rejects_pair_replacement_states(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        for stage in ("task_replacement", "replaced", "replacement_failed", "cancelled"):
            self.db.execute(
                "UPDATE pairs SET status='failed',stage=? WHERE id=?", (stage, pair["id"]),
            )
            with self.assertRaisesRegex(ValueError, "自动换题已启动或原 Pair 已退役"):
                self.service.reset_pair_retries(pair["id"])

    def test_wait_until_ready_allows_delayed_screen_and_container_start(self):
        arm = {"id": "delayed-arm", "container_name": "delayed-container",
               "screen_name": "delayed-screen"}
        root = self.service.claude.runtime_dir / arm["id"]
        root.mkdir(parents=True)
        (root / "terminal.log").write_text("BypassPermissions on", encoding="utf-8")
        with patch.object(self.service.claude, "_container_running",
                          side_effect=[False, False, True, True]), \
             patch.object(self.service.claude, "_screen_running",
                          side_effect=[False, False]), \
             patch("pairwise_console.claude_runner.time.sleep"):
            self.service.claude.wait_until_ready(arm, timeout=30)
        self.assertEqual((root / "permission-status").read_text(encoding="utf-8"),
                         "accepted\n")


if __name__ == "__main__":
    unittest.main()
