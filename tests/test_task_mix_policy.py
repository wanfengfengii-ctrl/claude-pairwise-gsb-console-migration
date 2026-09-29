import json
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch
from collections import Counter

from pairwise_console.db import Database, now_iso
from pairwise_console.service import PairwiseService


class TaskMixPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "policy.db")
        self.db.initialize()
        self.service = object.__new__(PairwiseService)
        self.service.db = self.db
        self.service._future_lock = threading.Lock()
        self.service._futures = {}
        self.db.set_setting("manual_bug_only_mode", True)
        self.db.set_setting("task_generation_zero_to_one_only", False)
        self.db.set_setting("task_mix_policy", {
            "enabled": True, "phase": "bug_top_up", "additionalBugTarget": 14,
            "excludedTaskIds": ["old"], "weights": {"zero_to_one": 7, "feature": 7, "bugfix": 10},
        })

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, task_id, kind="bugfix", status="ready", difficulty="困难", minutes=90):
        prompt = ("项目从空仓库起步，用 Dockerfile 和 Docker Compose 启动，并提供名为 verify、"
                  "执行完成后自行退出并用退出码报告结果的一次性验收服务。" + ("甲" * 60 + "。") * 4)
        self.db.execute(
            "INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,fingerprint,status,"
            "estimated_module_count,estimated_source_lines_min,estimated_source_lines_max,"
            "estimated_minutes_min,estimated_minutes_max,created_at,updated_at) "
            "VALUES(?,'test',?,?,?,?,?,?,3,80,180,60,?,?,?)",
            (task_id, kind, task_id, prompt, difficulty, task_id, status, minutes, now_iso(), now_iso()),
        )
        if kind == "bugfix":
            self.service._append_manual_bug_reservation(task_id)

    def phase(self, value):
        policy = self.db.setting("task_mix_policy")
        policy["phase"] = value
        self.db.set_setting("task_mix_policy", policy)
        self.db.set_setting("manual_bug_only_mode", value == "bug_top_up")

    def pair(self, pair_id, task_id):
        stamp = now_iso()
        self.db.execute("INSERT OR IGNORE INTO project_chains(id,root_task_id,created_at,updated_at) VALUES('c',?,?,?)",
                        (task_id, stamp, stamp))
        self.db.execute("INSERT INTO pairs(id,task_id,chain_id,created_at,updated_at) VALUES(?,?,'c',?,?)",
                        (pair_id, task_id, stamp, stamp))

    def test_ready_generated_task_with_old_development_only_estimate_is_blocked(self):
        self.phase("balanced")
        self.task("over-budget", kind="zero_to_one", minutes=160)
        items = [
            {"name": "实现", "phase": "development", "minMinutes": 80, "maxMinutes": 120, "basis": "核心逻辑"},
            {"name": "回归", "phase": "development", "minMinutes": 20, "maxMinutes": 40, "basis": "业务测试"},
            {"name": "交付", "phase": "docker_delivery", "minMinutes": 20, "maxMinutes": 35, "basis": "Compose 验证"},
        ]
        self.db.execute(
            "UPDATE tasks SET source='generated',estimate_work_items_json=? WHERE id='over-budget'",
            (json.dumps(items, ensure_ascii=False),),
        )
        issues = self.service._task_mix_task_issues(
            self.db.one("SELECT * FROM tasks WHERE id='over-budget'")
        )
        self.assertTrue(any("完整总工时超过 180 分钟" in issue for issue in issues))

    def test_only_new_qualified_admissions_count(self):
        self.task("old")
        self.task("used", status="used")
        self.task("rejected", status="rejected")
        self.task("medium", difficulty="中等")
        self.task("long", minutes=241)
        self.task("unlisted")
        self.db.set_setting("manual_priority_task_pause", {
            "active": True, "reservedTaskIds": ["old", "used", "rejected", "medium", "long"],
        })
        self.assertEqual(self.service._task_mix_progress()["qualified"], 1)
        self.assertEqual(self.service._task_mix_progress()["remaining"], 13)

    def test_transition_at_fourteen_and_restart_does_not_reset(self):
        self.task("old")
        self.pair("historical", "old")
        for i in range(13):
            self.task(str(i))
        self.service._advance_task_mix_policy()
        self.assertTrue(self.db.setting("manual_bug_only_mode"))
        self.task("13")
        self.service._advance_task_mix_policy()
        policy = self.db.setting("task_mix_policy")
        self.assertEqual(policy["phase"], "zero_to_one_first")
        self.assertEqual(policy["excludedPairIds"], ["historical"])
        self.assertFalse(self.db.setting("manual_bug_only_mode"))
        self.assertTrue(self.db.setting("auto_refill_enabled"))
        self.service.db = Database(self.db.path)
        self.service._advance_task_mix_policy()
        self.assertEqual(len(self.db.all("SELECT * FROM audit_events WHERE event_type='task.mix_transitioned'")), 1)
        self.assertEqual(self.service._task_mix_type_order(), ["zero_to_one"])

    def test_approved_credit_requires_two_more_real_bugs(self):
        for i in range(6):
            self.task(str(i))
        policy = self.db.setting("task_mix_policy")
        policy["approvedBugCredit"] = 6
        self.db.set_setting("task_mix_policy", policy)
        progress = self.service._task_mix_progress()
        self.assertEqual((progress["qualified"], progress["verifiedQualified"],
                          progress["approvedBugCredit"], progress["remaining"]), (12, 6, 6, 2))
        self.assertEqual(len(progress["qualifiedTaskIds"]), 6)
        self.task("six")
        self.service._advance_task_mix_policy()
        self.assertEqual(self.db.setting("task_mix_policy")["phase"], "bug_top_up")
        self.task("seven")
        self.service._advance_task_mix_policy()
        transitioned = self.db.setting("task_mix_policy")
        self.assertEqual(transitioned["phase"], "zero_to_one_first")
        self.assertEqual(len(transitioned["qualifiedTaskIds"]), 8)

    def test_actual_difficulty_review_does_not_recount_admitted_task(self):
        self.task("new")
        self.service._advance_task_mix_policy()
        self.db.execute("UPDATE tasks SET status='used',difficulty='中等' WHERE id='new'")
        self.assertEqual(self.service._task_mix_progress()["qualified"], 1)

    def test_rejected_admission_is_removed_before_transition(self):
        self.task("new")
        self.service._advance_task_mix_policy()
        self.db.execute("UPDATE tasks SET status='rejected' WHERE id='new'")
        self.assertEqual(self.service._task_mix_progress()["qualified"], 0)

    def test_rejected_admission_is_removed_after_transition(self):
        for i in range(8):
            self.task(str(i))
        policy = self.db.setting("task_mix_policy")
        policy["approvedBugCredit"] = 6
        self.db.set_setting("task_mix_policy", policy)
        self.service._advance_task_mix_policy()
        self.assertEqual(self.service._task_mix_progress()["qualified"], 14)
        self.db.execute("UPDATE tasks SET status='rejected' WHERE id='7'")
        progress = self.service._task_mix_progress()
        self.assertEqual((progress["qualified"], progress["verifiedQualified"],
                          progress["remaining"]), (13, 7, 1))
        self.assertNotIn("7", progress["qualifiedTaskIds"])

    def test_post_transition_deficit_recovers_without_recounting_history(self):
        for i in range(8):
            self.task(str(i))
        policy = self.db.setting("task_mix_policy")
        policy["approvedBugCredit"] = 6
        self.db.set_setting("task_mix_policy", policy)
        self.service._advance_task_mix_policy()
        self.phase("balanced")
        self.db.execute("UPDATE tasks SET status='rejected' WHERE id='7'")
        self.service._advance_task_mix_policy()
        self.assertTrue(self.service._task_mix_refill_deficit())
        self.assertEqual(self.service._task_mix_type_order(), ["bugfix"])
        self.assertFalse(self.service._schedule_task_source("zero_to_one"))
        self.task("replacement")
        self.service._advance_task_mix_policy()
        self.assertFalse(self.service._task_mix_refill_deficit())
        self.assertEqual(self.service._task_mix_progress()["qualified"], 14)
        admitted = self.db.setting("task_mix_policy")["qualifiedTaskIds"]
        self.assertIn("replacement", admitted)
        self.assertNotIn("7", admitted)
        self.task("later-mixed-bug")
        self.assertEqual(self.service._task_mix_progress()["verifiedQualified"], 8)

    def test_first_zero_pair_starts_balance_and_history_stays_excluded(self):
        self.phase("zero_to_one_first")
        self.task("old")
        self.pair("p-old", "old")
        policy = self.db.setting("task_mix_policy")
        policy["excludedPairIds"] = ["p-old"]
        self.db.set_setting("task_mix_policy", policy)
        self.task("new-zero", "zero_to_one")
        self.pair("p-zero", "new-zero")
        self.service._advance_task_mix_policy()
        self.assertEqual(self.db.setting("task_mix_policy")["phase"], "balanced")
        self.assertEqual(self.service._task_mix_counts(), {"zero_to_one": 1, "feature": 0, "bugfix": 0})

    def test_weighted_sequence_recovers_7_7_10_without_old_history(self):
        self.phase("balanced")
        counts = Counter(zero_to_one=1, feature=0, bugfix=0)
        for _ in range(23):
            with patch.object(self.service, "_task_mix_counts", return_value=dict(counts)):
                counts[self.service._task_mix_type_order()[0]] += 1
        self.assertEqual(dict(counts), {"zero_to_one": 7, "feature": 7, "bugfix": 10})

    def test_full_mixed_pool_refills_missing_ratio_types_with_two_slots(self):
        self.phase("balanced")
        for i in range(6):
            self.task(f"zero-{i}", "zero_to_one")
        with patch.object(self.service, "_task_mix_counts", return_value={
            "zero_to_one": 10, "feature": 4, "bugfix": 4,
        }), patch.object(self.service, "_schedule_task_source", return_value=True) as schedule:
            self.service._schedule_mixed_refill_once()
        self.assertEqual([call.args[0] for call in schedule.call_args_list],
                         ["bugfix", "feature"])

    def test_full_mixed_pool_does_not_overfill_types_already_ready(self):
        self.phase("balanced")
        for i in range(4):
            self.task(f"zero-{i}", "zero_to_one")
        self.task("feature", "feature")
        self.task("bug")
        with patch.object(self.service, "_schedule_task_source") as schedule:
            self.service._schedule_mixed_refill_once()
        schedule.assert_not_called()

    def test_full_pool_still_refills_understocked_feature_reserve(self):
        self.phase("balanced")
        self.db.set_setting("feature_ready_target", 8)
        self.db.set_setting("task_pool_target_ready", 6)
        self.db.set_setting("task_generation_max_parallel", 2)
        for i in range(5):
            self.task(f"feature-{i}", "feature")
        self.task("bug")
        self.service._futures["generate-zero-to-one"] = Future()
        with patch.object(self.service, "_task_mix_task_issues", return_value=[]), \
                patch.object(self.service, "_task_mix_type_order",
                             return_value=["bugfix", "zero_to_one", "feature"]), \
                patch.object(self.service, "_schedule_task_source", return_value=True) as schedule:
            self.service._schedule_mixed_refill_once()
        schedule.assert_called_once_with("feature")

    def test_balanced_mode_uses_ratio_when_multiple_types_are_ready(self):
        self.phase("balanced")
        self.task("feature-first", "feature")
        self.task("zero-later", "zero_to_one")
        self.db.execute("UPDATE tasks SET created_at=? WHERE id='feature-first'",
                        ("2026-01-01T00:00:00+00:00",))
        self.db.execute("UPDATE tasks SET created_at=? WHERE id='zero-later'",
                        ("2026-01-02T00:00:00+00:00",))
        with patch.object(self.service, "_task_mix_type_order",
                          return_value=["zero_to_one", "feature", "bugfix"]), \
                patch.object(self.service, "_deterministic_task_duplicate", return_value=""):
            self.assertEqual(self.service._next_ready_task()["id"], "zero-later")

    def test_balanced_mode_does_not_wait_for_missing_preferred_type(self):
        self.phase("balanced")
        self.task("feature-ready", "feature")
        with patch.object(self.service, "_task_mix_type_order",
                          return_value=["bugfix", "feature", "zero_to_one"]), \
                patch.object(self.service, "_deterministic_task_duplicate", return_value=""):
            self.assertEqual(self.service._next_ready_task()["id"], "feature-ready")

    def test_balanced_refill_also_maintains_bug_reserve(self):
        self.phase("balanced")
        with patch.object(self.service, "_advance_task_mix_policy"), \
                patch.object(self.service, "_schedule_manual_bug_refill_once") as bug_refill, \
                patch.object(self.service, "_schedule_mixed_refill_once") as mixed_refill:
            self.service._schedule_refill_once()
        bug_refill.assert_called_once_with()
        mixed_refill.assert_called_once_with()

    def test_balanced_refill_can_start_another_type_while_generation_runs(self):
        self.phase("balanced")
        self.db.set_setting("task_generation_max_parallel", 2)
        self.service._futures["generate-zero-to-one"] = Future()
        with patch.object(self.service, "_task_mix_type_order",
                          return_value=["zero_to_one", "feature", "bugfix"]), \
                patch.object(self.service, "_schedule_task_source", return_value=True) as schedule:
            self.service._schedule_mixed_refill_once()
        schedule.assert_called_once_with("feature")

    def test_balanced_missing_sources_do_not_fall_back_to_more_zero_tasks(self):
        self.phase("balanced")
        with patch.object(self.service, "_schedule_priority_bug_sources", return_value=False), \
                patch.object(self.service, "_failed_bug_sources", return_value=[]), \
                patch.object(self.service, "_eligible_feature_sources", return_value=[]), \
                patch.object(self.service, "_submit_auto") as submit:
            self.assertFalse(self.service._schedule_task_source("bugfix"))
            self.assertFalse(self.service._schedule_task_source("feature"))
        submit.assert_not_called()

    def test_feature_refill_skips_newest_project_at_iteration_limit(self):
        self.phase("balanced")
        sources = [
            {"id": "full", "baseline_repo_url": "https://example.test/full", "title": "full"},
            {"id": "open", "baseline_repo_url": "https://example.test/open", "title": "open"},
        ]
        def can_generate(seed):
            return seed["parent_pair_id"] == "open"
        with patch.object(self.service, "_eligible_feature_sources", return_value=sources), \
                patch.object(self.service, "_feature_project_can_generate", side_effect=can_generate), \
                patch.object(self.service, "_submit_auto", return_value=True) as submit:
            self.assertTrue(self.service._schedule_task_source("feature"))
        self.assertEqual(submit.call_args.args[0], "feature-open")

    def test_rejected_feature_proposals_do_not_consume_valid_iteration_slots(self):
        for i in range(2):
            self.task(f"rejected-{i}", "feature", status="rejected")
            self.db.execute("UPDATE tasks SET parent_pair_id='source' WHERE id=?",
                            (f"rejected-{i}",))
        seed = {"parent_pair_id": "source", "baseline_repo_url": "", "title": "source"}
        self.assertTrue(self.service._feature_project_can_generate(seed))
        self.task("candidate", "feature", status="candidate")
        self.db.execute("UPDATE tasks SET parent_pair_id='source' WHERE id='candidate'")
        candidate = self.db.one("SELECT * FROM tasks WHERE id='candidate'")
        self.assertEqual(self.service._feature_project_rank(candidate), 1)
        for i in range(2, 5):
            self.task(f"rejected-{i}", "feature", status="rejected")
            self.db.execute("UPDATE tasks SET parent_pair_id='source' WHERE id=?",
                            (f"rejected-{i}",))
        self.assertFalse(self.service._feature_project_can_generate(seed))

    def test_feature_refill_skips_source_with_existing_ready_followup(self):
        self.phase("balanced")
        self.task("already-ready", "feature")
        self.db.execute("UPDATE tasks SET parent_pair_id='full' WHERE id='already-ready'")
        sources = [
            {"id": "full", "baseline_repo_url": "", "title": "full"},
            {"id": "open", "baseline_repo_url": "", "title": "open"},
        ]
        with patch.object(self.service, "_eligible_feature_sources", return_value=sources), \
                patch.object(self.service, "_feature_project_rows", return_value=[]), \
                patch.object(self.service, "_submit_auto", return_value=True) as submit:
            self.assertTrue(self.service._schedule_task_source("feature"))
        self.assertEqual(submit.call_args.args[0], "feature-open")

    def test_new_generation_gate_rejects_over_limit_estimates_and_keeps_old_tasks_paused(self):
        self.phase("balanced")
        self.task("old", "zero_to_one", status="paused_manual_priority")
        self.task("missing", "zero_to_one", minutes=0)
        self.task("honest-over-target", "feature", minutes=181)
        self.task("invalid-estimate", "feature", minutes=481)
        self.task("valid", "zero_to_one", minutes=180)
        for task_id in ("old", "missing", "honest-over-target", "invalid-estimate"):
            task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
            self.assertTrue(self.service._task_mix_task_issues(task), task_id)
        self.assertEqual(self.service._task_mix_task_issues(self.db.one("SELECT * FROM tasks WHERE id='valid'")), [])
        self.assertEqual(self.db.one("SELECT status FROM tasks WHERE id='old'")["status"], "paused_manual_priority")

    def test_mixed_selection_allows_new_zero_outside_bug_reservations(self):
        self.phase("zero_to_one_first")
        self.task("bug")
        self.task("zero", "zero_to_one")
        with patch.object(self.service, "_preferred_zero_to_one_categories", return_value=[]), \
                patch.object(self.service, "_deterministic_task_duplicate", return_value=""):
            self.assertEqual(self.service._next_ready_task()["id"], "zero")

    def test_first_zero_gate_blocks_cached_bug_selection(self):
        self.task("cached-bug")
        self.phase("zero_to_one_first")
        task = self.db.one("SELECT * FROM tasks WHERE id='cached-bug'")
        self.assertTrue(self.service._task_mix_task_issues(task))

    def test_balanced_bug_gate_stays_hard_and_requires_reservation(self):
        self.phase("balanced")
        self.task("middle", difficulty="中等")
        self.task("hard")
        self.assertTrue(self.service._task_mix_task_issues(self.db.one("SELECT * FROM tasks WHERE id='middle'")))
        self.assertFalse(self.service._task_mix_task_issues(self.db.one("SELECT * FROM tasks WHERE id='hard'")))
        self.db.set_setting("manual_priority_task_pause", {"active": True, "reservedTaskIds": []})
        self.assertTrue(self.service._task_mix_task_issues(self.db.one("SELECT * FROM tasks WHERE id='hard'")))


if __name__ == "__main__":
    unittest.main()
