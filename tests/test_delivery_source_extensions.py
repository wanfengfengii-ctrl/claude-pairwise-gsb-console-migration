import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pairwise_console.gitops import GitOps


class DeliverySourceExtensionTests(unittest.TestCase):
    def test_javascript_modules_are_source(self):
        for path in ("src/service.mjs", "src/service.cjs", "src/service.js",
                     "src/SERVICE.MJS", "src/SERVICE.CJS"):
            with self.subTest(path=path):
                self.assertTrue(GitOps._is_source_delivery_path(path))

    def test_dependencies_and_lockfiles_remain_excluded(self):
        for path in ("node_modules/pkg/index.mjs", "node_modules/pkg/index.cjs",
                     ".venv/helper.mjs", "package-lock.json", "yarn.lock",
                     "README.md"):
            with self.subTest(path=path):
                self.assertFalse(GitOps._is_source_delivery_path(path))

    def test_module_changes_are_added_to_other_source_changes(self):
        # This shape used to count only worker.js (9 lines), losing the module.
        output = "5\t4\tsrc/bin/worker.js\0" + "122\t0\tsrc/services/idempotency.mjs\0"
        with patch("pairwise_console.gitops.run_command",
                   return_value=SimpleNamespace(stdout=output)):
            result = GitOps(None, None).delivery_diff_summary(Path("/tmp"), "base", "head")
        self.assertEqual(result["source_line_changes"], 131)
        self.assertEqual(result["source_files"],
                         ["src/bin/worker.js", "src/services/idempotency.mjs"])

    def test_commonjs_counts_additions_and_deletions_without_dependencies(self):
        output = ("7\t3\tsrc/handler.cjs\0"
                  "900\t0\tnode_modules/pkg/index.cjs\0"
                  "50\t0\tpackage-lock.json\0")
        with patch("pairwise_console.gitops.run_command",
                   return_value=SimpleNamespace(stdout=output)):
            result = GitOps(None, None).delivery_diff_summary(Path("/tmp"), "base", "head")
        self.assertEqual(result["source_line_changes"], 10)
        self.assertEqual(result["source_files"], ["src/handler.cjs"])
        self.assertEqual(result["generated_roots"], ["node_modules"])

    def test_effective_business_source_excludes_tests_and_formatting(self):
        output = ("133\t47\tsrc/solver/probe.ts\0"
                  "144\t0\tsrc/solver/probe.test.ts\0"
                  "9\t4\tREADME.md\0"
                  "30\t0\tpackage-lock.json\0")
        with patch("pairwise_console.gitops.run_command",
                   return_value=SimpleNamespace(stdout=output)) as command:
            result = GitOps(None, None).effective_business_source_diff(
                Path("/tmp"), "base", "head")
        self.assertIn("--ignore-all-space", command.call_args.args[0])
        self.assertEqual(result, {
            "files": [{"path": "src/solver/probe.ts", "additions": 133, "deletions": 47}],
            "moduleCount": 1, "additions": 133, "deletions": 47,
        })

    def test_business_script_is_counted_but_reproduction_script_is_not(self):
        self.assertTrue(GitOps._is_effective_business_source_path("scripts/solver.py"))
        self.assertFalse(GitOps._is_effective_business_source_path("scripts/repro_bug.py"))


if __name__ == "__main__":
    unittest.main()
