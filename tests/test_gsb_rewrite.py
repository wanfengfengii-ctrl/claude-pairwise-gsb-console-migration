import unittest
from unittest.mock import MagicMock

from pairwise_console.gsb_rewrite import validate_source, rewrite_preview
from pairwise_console.service import PairwiseService


class RewriteTests(unittest.TestCase):
    def setUp(self):
        self.source = validate_source(
            "A better",
            "A 在 merge.py 增加旧草稿校验，接口测试通过，但浏览器测试没有执行，因此略优。",
            "B 在 rebase.ts 修好了正常页面流程，容器验收通过，但新增测试是否运行仍无法确认。",
        )
        self.result = dict(self.source,
            aReason="A 在 merge.py 多加了一道旧草稿检查，接口测过了，不过浏览器测试没跑，所以稍好一些。")
        self.runner = MagicMock()
        self.runner.run.return_value = self.result

    def rewrite(self):
        return rewrite_preview(self.runner, self.source, "pair-test", PairwiseService._gsb_locator_issues)

    def test_valid_preview_preserves_verdict_and_source(self):
        before = dict(self.source)
        self.assertEqual(self.rewrite(), self.result)
        self.assertEqual(self.source, before)
        prompt = self.runner.run.call_args.args[1]
        self.assertIn("没有证据", prompt)
        self.assertIn(self.source['bReason'], prompt)

    def test_rejects_changed_verdict(self):
        self.runner.run.return_value = dict(self.result, verdict="B better")
        with self.assertRaisesRegex(ValueError, "改变了 GSB"):
            self.rewrite()

    def test_rejects_recording_based_public_preview(self):
        self.runner.run.return_value = dict(
            self.result,
            aReason="A 在 merge.py 完成旧草稿检查，录像显示页面正常，所以略优。",
        )
        with self.assertRaisesRegex(ValueError, "仍引用录像"):
            self.rewrite()

    def test_rejects_overlong_result_without_truncating_caveat(self):
        self.runner.run.return_value = dict(self.result, aReason="A merge.py " + "测试通过" * 80 + "，但浏览器测试没有执行")
        with self.assertRaisesRegex(ValueError, "长度"):
            self.rewrite()

    def test_retries_lost_evidence_locator_and_accepts_corrected_preview(self):
        lost = dict(self.result, aReason="A 多加了一道旧草稿检查，实际测过了，不过页面流程没跑，所以稍好一些。")
        self.runner.run.side_effect = [lost, self.result]
        self.assertEqual(self.rewrite(), self.result)
        self.assertEqual(self.runner.run.call_count, 2)
        retry_prompt = self.runner.run.call_args_list[1].args[1]
        self.assertIn("上一版口语化结果没有通过证据定位校验", retry_prompt)
        self.assertIn("merge.py", retry_prompt)

    def test_rejects_lost_evidence_locator_after_one_automatic_retry(self):
        lost = dict(self.result, aReason="A 多加了一道旧草稿检查，实际测过了，不过页面流程没跑，所以稍好一些。")
        self.runner.run.return_value = lost
        with self.assertRaisesRegex(ValueError, "自动重试后仍遗漏"):
            self.rewrite()
        self.assertEqual(self.runner.run.call_count, 2)

    def test_module_name_is_a_reviewable_locator(self):
        self.runner.run.return_value = dict(
            self.result,
            aReason="A 用 app.verify 测过旧草稿保存和接口，不过浏览器测试没跑，所以稍好一些。",
        )
        rewritten = self.rewrite()
        self.assertIn("app.verify", rewritten["aReason"])
        self.runner.run.assert_called_once()

    def test_natural_verification_result_is_a_reviewable_locator(self):
        a_reason = "A 单独跑 Docker 验收通过，相同内容换名后不用重传，完整下载和 Range 范围读取都正确。"
        b_reason = "B 的真实接口返回 206，重启后下载内容一致，不过没有覆盖损坏候选回退。"
        self.assertEqual(PairwiseService._gsb_locator_issues(a_reason, b_reason), [])

    def test_preview_may_summarize_non_decisive_numbers_without_losing_the_api_result(self):
        self.source["aReason"] = (
            "A 调用 API 返回高扇出 10 条正边、总量 160，裁决向量为 (3,3,0,0)，范围与到达值正确。"
        )
        natural = "A 实际调用 API 后，高扇出场景返回了预期的正边，边的总量、裁决向量、范围和到达值也核对正确。"
        self.runner.run.return_value = dict(self.result, aReason=natural)
        self.assertEqual(self.rewrite()["aReason"], natural)
        prompt = self.runner.run.call_args.args[1]
        self.assertIn("数字不是一律照抄", prompt)
        self.assertIn("错误、边界、阈值、状态码或 A/B 差异", prompt)

    def test_invalid_source_never_calls_model(self):
        for verdict, a, b in [("invalid", "a"*20, "b"*20), ("Same", "", "b"*20), ("Same", None, "b"*20), ("Same", "a"*301, "b"*20)]:
            with self.assertRaises(ValueError):
                validate_source(verdict, a, b)
        self.runner.run.assert_not_called()

    def test_failure_propagates_without_replacement(self):
        self.runner.run.side_effect = RuntimeError("模型暂不可用")
        with self.assertRaisesRegex(RuntimeError, "模型暂不可用"):
            self.rewrite()
