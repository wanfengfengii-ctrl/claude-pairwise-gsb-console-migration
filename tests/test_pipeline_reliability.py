import json
from pathlib import Path
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from pairwise_console.db import Database, now_iso
from pairwise_console.config import DEFAULT_CODEX_MODEL, load_config
from pairwise_console.service import PairwiseService
from pairwise_console.pipeline_state import operation_failed, operation_ready, cli_event_error
from pairwise_console.bug_verification import (check_isolation, private_container_name,
                                               private_image_tag, wait_for_services, validate_specs)
from pairwise_console.commands import CommandResult
from pairwise_console.analytics import dashboard


class PipelineReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = replace(load_config(Path(__file__).resolve().parents[1]),
                              data_dir=self.root, db_path=self.root / "test.db",
                              projects_dir=self.root / "projects", old_db_path=self.root / "old.db")
        self.db = Database(self.config.db_path)
        self.db.initialize()
        self.service = PairwiseService(self.config, self.db)

    def tearDown(self):
        self.service.executor.shutdown(wait=True, cancel_futures=True)
        self.service.monitor_executor.shutdown(wait=True, cancel_futures=True)
        self.tmp.cleanup()

    def test_codex_default_uses_a_model_in_local_api_catalog(self):
        self.assertEqual(DEFAULT_CODEX_MODEL, "gpt-5.6-terra")

    def task(self, task_id="t"):
        self.db.execute("INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,fingerprint,status,created_at,updated_at) "
                        "VALUES(?,'test','bugfix','业务问题','复核合法输入','困难',?,'ready',?,?)",
                        (task_id, task_id, now_iso(), now_iso()))

    def test_private_images_are_unique_per_service_and_clean_run(self):
        first = private_image_tag("run-a", "api1", "tree", "build")
        self.assertEqual(first, private_image_tag("run-a", "api1", "tree", "build"))
        self.assertNotEqual(first, private_image_tag("run-a", "api2", "tree", "build"))
        self.assertNotEqual(first, private_image_tag("run-b", "api1", "tree", "build"))

    def test_fixed_container_names_only_pass_after_private_remapping(self):
        private = private_container_name("clean-run-a", "verify")
        self.assertNotEqual(private, private_container_name("clean-run-b", "verify"))
        self.assertNotEqual(private, private_container_name("clean-run-a", "web"))
        config = {"services": {"verify": {"container_name": "shared-verify"}}}
        with self.assertRaisesRegex(ValueError, "共享名称"):
            check_isolation(config, self.root)
        config["services"]["verify"]["container_name"] = private
        check_isolation(config, self.root, {"verify": private})
        with self.assertRaisesRegex(ValueError, "共享名称"):
            check_isolation(config, self.root, {"verify": "different-private-name"})

    def test_bug_discovery_sees_ancestor_candidates(self):
        stamp = now_iso()
        self.task("root")
        self.db.execute("INSERT INTO project_chains(id,root_task_id,created_at,updated_at) VALUES(?,?,?,?)",
                        ("chain", "root", stamp, stamp))
        self.db.execute("INSERT INTO pairs(id,task_id,chain_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                        ("parent", "root", "chain", stamp, stamp))
        self.db.execute("""INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,
                        preconditions,reproduction_steps_json,actual_result,expected_result,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        ("bug-parent", "parent", "A", "a" * 40, "祖先缺陷", "条件", "[]",
                         "错误结果", "正确结果", stamp, stamp))
        self.db.execute("""INSERT INTO tasks(id,source,source_id,task_type,title,prompt,difficulty,
                        fingerprint,status,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        ("child", "bug_discovery", "bug-parent", "bugfix", "修复", "修复", "困难",
                         "child", "used", stamp, stamp))
        self.db.execute("INSERT INTO pairs(id,task_id,chain_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                        ("descendant", "child", "chain", stamp, stamp))
        rows = self.service._bug_source_candidate_rows("descendant", "A", "b" * 40)
        self.assertEqual([row["id"] for row in rows], ["bug-parent"])

    def test_sibling_arm_decimal_cost_duplicate_is_rejected_before_conversion(self):
        self.task("root")
        self.db.execute("INSERT INTO project_chains(id,root_task_id,created_at,updated_at) VALUES(?,?,?,?)",
                        ("chain", "root", now_iso(), now_iso()))
        self.db.execute("INSERT INTO pairs(id,task_id,chain_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                        ("parent", "root", "chain", now_iso(), now_iso()))
        cases = (
            ("bug-a", "A", "a" * 40, "先前的成本尾差", "converted",
             "parseDraft 用 Number 令文本成本 0.10000000000000001 精度丢失，较贵位置被稳定序号选中",
             "2026-09-26T00:00:00+00:00"),
            ("bug-b", "B", "b" * 40, "另一个录入成本标题", "reproduced",
             "parseDraft 的 Number 抹除 1.0000000000000001 的小数尾差，较高代价按稳定序号选中",
             "2026-09-26T00:01:00+00:00"),
        )
        for candidate_id, arm, sha, title, status, actual, created in cases:
            self.db.execute(
                """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,
                   preconditions,reproduction_steps_json,actual_result,expected_result,
                   source_paths_json,reproduce_count,difficulty,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id, "parent", arm, sha, title, "合法文本草稿", "[]", actual,
                 "应选更低成本的位置，仅同价时按序号裁决",
                 json.dumps(["src/draft.ts", "src/solver/adjudicate.ts"]),
                 2, "困难", status, created, created),
            )
        rows = self.service._bug_source_candidate_rows("parent", "B", "b" * 40)
        self.assertEqual([row["id"] for row in rows], ["bug-a", "bug-b"])
        with patch.object(self.service, "_strict_bug_admission", return_value=False):
            with self.assertRaisesRegex(ValueError, "十进制尾差"):
                self.service.convert_bug_to_task("bug-b")
        self.assertEqual(self.db.one("SELECT status FROM bug_candidates WHERE id='bug-b'")["status"],
                         "duplicate_rejected")

    def test_reservations_concurrent_append_preserves_both(self):
        start = threading.Barrier(2)
        errors = []
        def append(task):
            try:
                start.wait(timeout=5)
                self.service._append_manual_bug_reservation(task)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=append, args=(name,)) for name in ("a", "b")]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(set(self.db.setting("manual_priority_task_pause")["reservedTaskIds"]), {"a", "b"})

    def test_empty_admission_fails_closed(self):
        self.task()
        self.db.set_setting("manual_bug_only_mode", True)
        self.db.set_setting("manual_priority_task_pause", {"active": True, "reservedTaskIds": []})
        with self.assertRaisesRegex(ValueError, "尚未加入"):
            self.service.create_pair("t")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM pairs")["n"], 0)

    def test_retry_block_survives_new_database_object(self):
        result = operation_failed(self.db, "bugs-one", "flagged for possible cybersecurity risk")
        self.assertTrue(result["blocked"])
        self.assertFalse(operation_ready(Database(self.config.db_path), "bugs-one"))
        self.assertTrue(operation_ready(self.db, "bugs-two"))

    def test_unknown_error_has_finite_automatic_retries(self):
        for _ in range(3): result = operation_failed(self.db, "bug-convert-x", "invalid JSON")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["attempts"], 3)

    def test_cooling_conversion_does_not_block_next_candidate(self):
        operation_failed(self.db, "bug-convert-a", "504 timeout")
        candidates = [{"id": "a"}, {"id": "b"}]
        with patch.object(self.db, "all", return_value=candidates), patch.object(self.service, "_submit_auto", return_value=True) as submit:
            self.service._schedule_task_source("bugfix")
        self.assertEqual(submit.call_args.args[0], "bug-convert-b")

    def test_long_paragraph_does_not_hide_template_tail(self):
        text = "合法输入得到互相矛盾的业务结果。" * 20 + "现有 Docker Compose 启动方式与 verify 链路仍须正常运行。"
        self.assertTrue(any("固定结尾" in issue for issue in self.service._bugfix_prompt_issues(text)))

    def test_repeated_invalid_bug_prompt_rejects_candidate(self):
        stamp = now_iso()
        self.task("source")
        self.db.execute("INSERT INTO project_chains(id,root_task_id,created_at,updated_at) VALUES(?,?,?,?)",
                        ("chain", "source", stamp, stamp))
        self.db.execute("INSERT INTO pairs(id,task_id,chain_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                        ("parent", "source", "chain", stamp, stamp))
        self.db.execute("""INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,
                        preconditions,reproduction_steps_json,actual_result,expected_result,
                        reproduce_count,difficulty,status,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("bad-prompt", "parent", "A", "a" * 40, "业务缺陷", "合法输入", "[]",
                         "返回错误", "返回正确结果", 2, "困难", "reproduced", stamp, stamp))
        candidate = self.db.one("SELECT * FROM bug_candidates WHERE id='bad-prompt'")
        prompt = "合法输入产生不一致结果。" * 20 + "现有 Docker Compose 启动方式与 verify 链路仍须正常运行。"
        with patch.object(self.service.codex, "run", return_value={"prompt": prompt, "evidenceUsed": []}) as run:
            with self.assertRaisesRegex(ValueError, "固定结尾"):
                self.service._generate_bugfix_task_prompt(candidate, {"workspace_path": str(self.root)}, {})
        self.assertEqual(run.call_count, 2)
        self.assertEqual(self.db.one("SELECT status FROM bug_candidates WHERE id='bad-prompt'")["status"], "rejected")

    def test_bug_prompt_rejects_private_fallback_plan(self):
        text = (
            "合法证据在 Docker Compose 冷启动后被判为 VALID，但包内复核结果为 REVOKED。" * 4
            + "任何作用域条目用于裁决前，都应检测侧车内容并改由封存 DER 得出权威结论。"
            + "预期在线返回 REJECTED，离线复核一致；verify 仍可正常完成。"
        )
        self.assertTrue(any("指定修法" in issue for issue in self.service._bugfix_prompt_issues(text)))

    def test_bug_prompt_rejects_global_translation_solution_hint(self):
        text = (
            "合法锚点输入被误判为冲突。" * 10
            + "请求实际返回 422，预期应返回 200 并保留所有锚点一致性。"
            + "锚点给出的共同全局水深可以整体平移整条链，不应仅因其不是零而被拒绝。"
        )
        self.assertTrue(any("实现方法" in issue for issue in self.service._bugfix_prompt_issues(text)))

    def test_bug_prompt_rejects_unique_per_round_answer(self):
        text = (
            "合法的大整数输入返回了与实际行程不一致的最终偏差。" * 8
            + "每点四轮总行程应为 4，因此唯一正确的行程选择是每轮均取 1。"
            + "验收应通过业务 API 核对总行程与最终偏差。"
        )
        self.assertTrue(any("精确答案" in issue for issue in self.service._bugfix_prompt_issues(text)))

    def test_browser_documentation_not_dependency(self):
        (self.root / "README.md").write_text("Playwright 已移除，当前使用 HTTP", encoding="utf-8")
        self.assertEqual(self.service._bug_source_browser_automation_files(self.root), [])
        (self.root / "verify.mjs").write_text("import {chromium} from 'playwright'", encoding="utf-8")
        self.assertIn("verify.mjs", self.service._bug_source_browser_automation_files(self.root))

    def test_latest_nonexhausted_scan_wins(self):
        self.db.audit("bug.discovery_completed", "pair", "p", {"arm": "A", "exhausted": True})
        self.db.audit("bug.discovery_completed", "pair", "p", {"arm": "A", "exhausted": False})
        self.assertFalse(self.service._bug_source_arm_exhausted("p", "A"))

    def test_stats_count_first_confirmation_only(self):
        self.db.audit("gsb.confirmed", "pair", "p", {})
        self.db.audit("gsb.confirmed", "pair", "p", {})
        self.db.audit("gsb.confirmed", "pair", "q", {})
        self.assertEqual(dashboard(self.db)["summary"]["completedPairs24h"], 2)

    def test_structured_cli_error_is_not_lost(self):
        events = self.root / "events.jsonl"
        events.write_text(json.dumps({"type": "turn.failed", "error": {"message": "permission denied"}}))
        self.assertEqual(cli_event_error(events), "permission denied")

    def test_readiness_requires_every_application_healthy(self):
        response = CommandResult([], "", 0, json.dumps([
            {"Service": "api", "State": "running", "Health": "healthy"},
            {"Service": "web", "State": "running", "Health": "starting"},
        ]), "")
        with self.assertRaisesRegex(RuntimeError, "环境未就绪"):
            wait_for_services([], self.root, {}, ["api", "web"], timeout=0, runner=lambda *a, **k: response)

    def test_clean_reproduction_rejects_shared_host_state(self):
        config = {"services": {"api": {"volumes": [{"type": "bind", "source": "/var/run/docker.sock"}]}}}
        with self.assertRaisesRegex(ValueError, "快照外"):
            check_isolation(config, self.root)

    def test_fix_check_needs_distinct_business_markers(self):
        with self.assertRaises(ValueError):
            validate_specs([{"composeArgs": ["exec", "api", "check"], "expectedOutputContains": "ok", "failureOutputContains": "ok"}], repair=True)

    def test_invalid_bug_verification_is_rejected_without_retry(self):
        stamp = now_iso()
        self.task("source")
        self.db.execute("INSERT INTO project_chains(id,root_task_id,created_at,updated_at) VALUES(?,?,?,?)",
                        ("chain", "source", stamp, stamp))
        self.db.execute("INSERT INTO pairs(id,task_id,chain_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                        ("pair", "source", "chain", stamp, stamp))
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("arm", "pair", "A", "A", str(self.root), "container", "screen", "model", "image",
             "completed", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,preconditions,
               reproduction_steps_json,reproduction_commands_json,actual_result,expected_result,
               difficulty,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("bug", "pair", "A", "a" * 40, "restart-only candidate", "ready", "[]",
             json.dumps([{"composeArgs": ["restart", "api"], "expectedOutputContains": "bad"}]),
             "bad", "good", "困难", "awaiting_reproduction", stamp, stamp),
        )
        with patch.object(self.service, "_strict_bug_admission", return_value=False), \
                patch("pairwise_console.service.clean_commands") as reproduce:
            result = self.service.reproduce_bug("bug")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("Compose exec/run", result["error"])
        reproduce.assert_not_called()
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM audit_events "
                                     "WHERE event_type='bug.invalid_verification_rejected'")["n"], 1)

    def test_draining_does_not_start_new_work(self):
        self.db.set_setting("pipeline_drain", True)
        with patch.object(self.service.executor, "submit") as submit:
            self.assertFalse(self.service._submit_auto("bugs-x", lambda: None))
        submit.assert_not_called()
