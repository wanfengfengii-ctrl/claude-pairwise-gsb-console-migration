import unittest

from scripts.migrate_ready_tasks import _canonical_repo_url


class MigrationTests(unittest.TestCase):
    def test_github_remote_rewrite_keeps_repository_identity(self):
        expected = "https://github.com/owner/example"
        self.assertEqual(_canonical_repo_url("git@github.com:owner/example.git"), expected)
        self.assertEqual(_canonical_repo_url("ssh://git@github.com/owner/example.git"), expected)
        self.assertEqual(_canonical_repo_url("https://github.com/owner/example.git"), expected)


if __name__ == "__main__":
    unittest.main()
