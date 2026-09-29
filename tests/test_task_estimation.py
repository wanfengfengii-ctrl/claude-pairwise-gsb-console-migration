import tempfile
import unittest
from pathlib import Path

from pairwise_console.db import Database
from pairwise_console.prompts import task_validation_prompt
from pairwise_console.service import ESTIMATED_VALIDATION_SCHEMA, VALIDATION_SCHEMA
from pairwise_console.task_estimation import summarize_reviewed_estimate


class TaskEstimateTests(unittest.TestCase):
    def test_independent_estimate_accepts_180_minute_boundary(self):
        review = summarize_reviewed_estimate([
            {"name": "实现", "phase": "development", "minMinutes": 70, "maxMinutes": 110, "basis": "跨模块状态处理"},
            {"name": "本地回归", "phase": "development", "minMinutes": 20, "maxMinutes": 35, "basis": "业务模块回归"},
            {"name": "后续交付", "phase": "docker_delivery", "minMinutes": 15, "maxMinutes": 35, "basis": "Compose 交付与清洁验收"},
        ], 90, 180)
        self.assertEqual(review["max"], 180)
        self.assertEqual(review["dockerDeliveryMax"], 35)
        self.assertNotIn("超过180分钟上限", review["risk"])

    def test_independent_items_expose_an_underestimated_overrun(self):
        review = summarize_reviewed_estimate([
            {"name": "理解基线", "phase": "development", "minMinutes": 20, "maxMinutes": 30, "basis": "核对既有状态与接口"},
            {"name": "核心实现", "phase": "development", "minMinutes": 90, "maxMinutes": 180, "basis": "新增持久化恢复路径"},
            {"name": "本地回归", "phase": "development", "minMinutes": 35, "maxMinutes": 55, "basis": "业务模块构建与回归"},
            {"name": "后续交付", "phase": "docker_delivery", "minMinutes": 10, "maxMinutes": 25, "basis": "Compose verify 与清洁验收"},
        ], 105, 120)
        self.assertEqual((review["min"], review["max"]), (155, 290))
        self.assertIn("超过180分钟上限", review["risk"])
        self.assertIn("生成方可能低估", review["risk"])

    def test_bad_work_item_cannot_be_silently_used(self):
        with self.assertRaises(ValueError):
            summarize_reviewed_estimate([
                {"name": "实现", "phase": "development", "minMinutes": 30, "maxMinutes": 20, "basis": "倒置区间"},
                {"name": "验证", "phase": "development", "minMinutes": 10, "maxMinutes": 20, "basis": "测试"},
                {"name": "后续交付", "phase": "docker_delivery", "minMinutes": 5, "maxMinutes": 10, "basis": "交付"},
            ], 40, 60)

    def test_docker_work_cannot_be_hidden_in_development_time(self):
        with self.assertRaisesRegex(ValueError, "Docker 交付"):
            summarize_reviewed_estimate([
                {"name": "实现", "phase": "development", "minMinutes": 60, "maxMinutes": 90, "basis": "业务实现"},
                {"name": "Compose verify", "phase": "development", "minMinutes": 10, "maxMinutes": 20, "basis": "镜像启动"},
                {"name": "后续交付", "phase": "docker_delivery", "minMinutes": 5, "maxMinutes": 10, "basis": "验收"},
            ], 70, 110)

    def test_generated_validation_requires_itemized_blind_estimate(self):
        self.assertIn("workItems", ESTIMATED_VALIDATION_SCHEMA["required"])
        self.assertIn("phase", ESTIMATED_VALIDATION_SCHEMA["properties"]["workItems"]["items"]["required"])
        for schema in (VALIDATION_SCHEMA, ESTIMATED_VALIDATION_SCHEMA):
            self.assertEqual(set(schema["properties"]), set(schema["required"]))
        prompt = task_validation_prompt("待评题面", "无", "基线", "无", "同类中位数 80 分钟", True)
        self.assertIn("workItems", prompt)
        self.assertIn("docker_delivery", prompt)
        self.assertIn("同类中位数 80 分钟", prompt)

    def test_task_table_has_review_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            db.initialize()
            columns = {row[1] for row in db.connect().execute("PRAGMA table_info(tasks)")}
            self.assertTrue({"reviewed_minutes_min", "reviewed_minutes_max",
                             "estimate_work_items_json", "estimate_risk"} <= columns)


if __name__ == "__main__":
    unittest.main()
