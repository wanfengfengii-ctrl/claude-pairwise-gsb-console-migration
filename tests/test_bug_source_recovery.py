import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pairwise_console.commands import run_command
from pairwise_console.db import Database, now_iso
from pairwise_console.service import PairwiseService


class BugSourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.db = Database(self.root / 'test.db')
        self.db.initialize()
        self.service = object.__new__(PairwiseService)
        self.service.db = self.db
        self.service.config = SimpleNamespace(data_dir=self.root / 'data',
                                             git_author_name='test', git_author_email='test@example.com')
        self.db.set_setting('manual_bug_only_mode', True)
        self.db.set_setting('manual_bug_auto_refill_enabled', True)
        stamp = now_iso()
        self.db.execute("INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,fingerprint,status,created_at,updated_at) "
                        "VALUES('task','test','zero_to_one','source','original','困难','source','used',?,?)", (stamp, stamp))
        self.db.execute("INSERT INTO project_chains(id,root_task_id,created_at,updated_at) "
                        "VALUES('chain','task',?,?)", (stamp, stamp))
        self.db.execute("INSERT INTO pairs(id,task_id,chain_id,status,stage,created_at,updated_at) "
                        "VALUES('pair','task','chain','completed','completed',?,?)", (stamp, stamp))

    def tearDown(self):
        self.tmp.cleanup()

    def arm(self, name, sha='', check_sha='', status='observed_failed'):
        stamp = now_iso()
        self.db.execute("INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,"
                        "model,image,status,commit_sha,created_at,updated_at) "
                        "VALUES(?,'pair',?,?,?,'container','screen','model','image','completed',?,?,?)",
                        ('arm-' + name, name, name, str(self.root), sha, stamp, stamp))
        self.db.execute("INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at) "
                        "VALUES(?,'pair',?,?,?,?,?)", ('check-' + name, name, check_sha or sha, status, stamp, stamp))

    def test_successful_peer_and_its_discovery_do_not_hide_failed_arm(self):
        self.arm('A', 'a' * 40)
        self.arm('B', 'b' * 40, status='passed')
        self.db.audit('bug.discovery_completed', 'pair', 'pair', {'arm': 'B', 'candidateIds': []})
        self.db.audit('bug.source_repair_completed', 'pair', 'pair', {'sourceArm': 'B', 'sourceSha': 'b' * 40})
        self.assertEqual([(r['arm'], r['commit_sha']) for r in self.service._failed_bug_sources()], [('A', 'a' * 40)])

    def test_history_check_recovers_missing_arm_pointer_without_mutation(self):
        self.arm('A', '', check_sha='a' * 40)
        self.assertEqual(self.service._failed_bug_sources()[0]['commit_sha'], 'a' * 40)
        self.assertEqual(self.db.one("SELECT commit_sha FROM arm_runs WHERE arm='A'")['commit_sha'], '')

    def test_repair_audit_is_scoped_to_arm_and_sha(self):
        self.arm('A', 'a' * 40)
        self.arm('B', 'b' * 40)
        self.db.audit('bug.source_repair_failed', 'pair', 'pair', {'sourceArm': 'A', 'sourceSha': 'c' * 40})
        self.assertEqual(len(self.service._failed_bug_sources()), 2)
        self.db.audit('bug.source_repair_failed', 'pair', 'pair', {'sourceArm': 'A', 'sourceSha': 'a' * 40})
        self.assertEqual([r['arm'] for r in self.service._failed_bug_sources()], ['B'])

    def test_running_pair_never_selected(self):
        self.arm('A', 'a' * 40)
        self.db.execute("UPDATE pairs SET status='running'")
        self.assertEqual(self.service._failed_bug_sources(), [])

    def test_priority_passes_exact_identity_to_repair(self):
        self.arm('A', 'a' * 40)
        self.arm('B', 'b' * 40)
        self.db.set_setting('manual_bug_source_priority', [
            {'pairId': 'pair', 'arm': 'B', 'sourceSha': 'b' * 40},
            {'pairId': 'pair', 'arm': 'A', 'sourceSha': 'a' * 40},
        ])
        with patch.object(self.service, '_submit_auto', return_value=True) as submit:
            self.assertTrue(self.service._schedule_priority_bug_sources())
        self.assertEqual(submit.call_args.args[2:], ('pair', 'B', 'b' * 40))

    def test_repaired_priority_is_not_hidden_by_peer_or_old_exhaustion(self):
        self.arm('A', 'a' * 40)
        self.arm('B', 'b' * 40, status='passed')
        self.db.set_setting('manual_bug_source_priority', [{'pairId': 'pair', 'arm': 'A', 'sourceSha': 'a' * 40}])
        self.db.audit('bug.source_repair_completed', 'pair', 'pair', {
            'sourceArm': 'A', 'sourceSha': 'a' * 40, 'baselineSha': 'c' * 40,
            'workspacePath': str(self.root), 'validation': {'status': 'passed'},
        })
        self.db.audit('bug.discovery_completed', 'pair', 'pair', {'arm': 'A', 'sourceSha': 'a' * 40, 'exhausted': True})
        with patch.object(self.service, '_submit_auto', return_value=True) as submit:
            self.assertTrue(self.service._schedule_priority_bug_sources())
        self.assertEqual(submit.call_args.args[1].__func__, self.service.discover_bugs.__func__)
        self.assertEqual(submit.call_args.args[2:], ('pair', 'A', 'c' * 40))
        self.db.audit('bug.discovery_completed', 'pair', 'pair', {'arm': 'A', 'sourceSha': 'c' * 40, 'exhausted': True})
        self.assertFalse(self.service._schedule_priority_bug_sources())

    def test_unverified_repair_is_not_a_source(self):
        self.db.audit('bug.source_repair_completed', 'pair', 'pair', {
            'sourceArm': 'A', 'baselineSha': 'c' * 40, 'workspacePath': str(self.root),
        })
        self.assertIsNone(self.service._repaired_bug_source('pair'))

    def test_exact_git_snapshot_ignores_dirty_worktree_and_reinitializes_history(self):
        source = self.root / 'original'
        source.mkdir()
        for command in (['git', 'init', '-b', 'main'], ['git', 'config', 'user.name', 'test'],
                        ['git', 'config', 'user.email', 'test@example.com']):
            run_command(command, cwd=source)
        (source / 'app.py').write_text('original\n')
        run_command(['git', 'add', '.'], cwd=source)
        run_command(['git', 'commit', '-m', 'source'], cwd=source)
        sha = run_command(['git', 'rev-parse', 'HEAD'], cwd=source).stdout.strip()
        (source / 'app.py').write_text('private dirty edit\n')
        item = {'workspace_path': str(source), 'commit_sha': sha}
        self.assertEqual(self.service._resolve_failed_bug_workspace(item), source)
        target, new_sha = self.service._create_isolated_bug_baseline({'source_sha': sha}, item, 'isolated')
        self.assertEqual((target / 'app.py').read_text(), 'original\n')
        self.assertEqual((source / 'app.py').read_text(), 'private dirty edit\n')
        self.assertEqual(run_command(['git', 'rev-parse', 'HEAD'], cwd=source).stdout.strip(), sha)
        self.assertNotEqual(new_sha, sha)
        self.assertEqual(run_command(['git', 'rev-list', '--count', 'HEAD'], cwd=target).stdout.strip(), '1')
        with self.assertRaises(ValueError):
            self.service._resolve_failed_bug_workspace({**item, 'commit_sha': 'f' * 40})


if __name__ == '__main__':
    unittest.main()
