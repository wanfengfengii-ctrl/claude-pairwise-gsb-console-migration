import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = 15


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.transaction() as c:
            c.executescript(SCHEMA)
            task_columns = {row[1] for row in c.execute("PRAGMA table_info(tasks)")}
            if "project_category" not in task_columns:
                c.execute("ALTER TABLE tasks ADD COLUMN project_category TEXT NOT NULL DEFAULT '未记录'")
            from .classification import normalize_project_category, normalize_stack
            for row in c.execute("SELECT id,project_category,title,prompt,stack FROM tasks").fetchall():
                category = normalize_project_category(row[1], row[2], row[3], row[4])
                stack = normalize_stack(row[4])
                if row[1] != category or row[4] != stack:
                    c.execute("UPDATE tasks SET project_category=?,stack=? WHERE id=?", (category, stack, row[0]))
            # Bug tasks are created from a concrete completed Pair and must
            # inherit its main language/framework metadata for submission.
            c.execute(
                """UPDATE tasks SET stack=COALESCE((
                       SELECT source_task.stack FROM pairs source_pair
                         JOIN tasks source_task ON source_task.id=source_pair.task_id
                        WHERE source_pair.id=tasks.parent_pair_id
                     ),stack)
                     WHERE task_type='bugfix' AND stack=''
                       AND parent_pair_id<>''"""
            )
            columns = {row[1] for row in c.execute("PRAGMA table_info(arm_runs)")}
            for name, definition in (
                ("result", "TEXT NOT NULL DEFAULT ''"),
                ("warning_at", "TEXT"),
                ("attempt_no", "INTEGER NOT NULL DEFAULT 1"),
                ("error_retry_count", "INTEGER NOT NULL DEFAULT 0"),
                ("api_retry_count", "INTEGER NOT NULL DEFAULT 0"),
                ("api_retry_after", "TEXT"),
                ("last_api_error", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in columns:
                    c.execute("ALTER TABLE arm_runs ADD COLUMN %s %s" % (name, definition))
            pair_columns = {row[1] for row in c.execute("PRAGMA table_info(pairs)")}
            if "development_failure_count" not in pair_columns:
                c.execute(
                    "ALTER TABLE pairs ADD COLUMN development_failure_count INTEGER NOT NULL DEFAULT 0"
                )
            for name, definition in (
                ("model_scheme", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("model_a", "TEXT NOT NULL DEFAULT ''"),
                ("model_b", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in pair_columns:
                    c.execute("ALTER TABLE pairs ADD COLUMN %s %s" % (name, definition))
            bug_columns = {row[1] for row in c.execute("PRAGMA table_info(bug_candidates)")}
            for name, definition in (
                ("reproduction_commands_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("repair_verification_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("reproduction_results_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("source_paths_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("estimated_module_count", "INTEGER NOT NULL DEFAULT 0"),
                ("estimated_source_lines_min", "INTEGER NOT NULL DEFAULT 0"),
                ("estimated_source_lines_max", "INTEGER NOT NULL DEFAULT 0"),
                ("estimated_minutes_min", "INTEGER NOT NULL DEFAULT 0"),
                ("estimated_minutes_max", "INTEGER NOT NULL DEFAULT 0"),
                ("complexity_axes_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if name not in bug_columns:
                    c.execute("ALTER TABLE bug_candidates ADD COLUMN %s %s" % (name, definition))
            task_columns = {row[1] for row in c.execute("PRAGMA table_info(tasks)")}
            for name, definition in (
                ("estimated_module_count", "INTEGER NOT NULL DEFAULT 0"),
                ("repair_verification_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("estimated_source_lines_min", "INTEGER NOT NULL DEFAULT 0"),
                ("estimated_source_lines_max", "INTEGER NOT NULL DEFAULT 0"),
                ("estimated_minutes_min", "INTEGER NOT NULL DEFAULT 0"),
                ("estimated_minutes_max", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_minutes_min", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_minutes_max", "INTEGER NOT NULL DEFAULT 0"),
                ("estimate_work_items_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("estimate_risk", "TEXT NOT NULL DEFAULT ''"),
                ("complexity_axes_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if name not in task_columns:
                    c.execute("ALTER TABLE tasks ADD COLUMN %s %s" % (name, definition))
            recording_columns = {row[1] for row in c.execute("PRAGMA table_info(recordings)")}
            for name, definition in (
                ("commit_sha", "TEXT NOT NULL DEFAULT ''"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("steps_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("upload_status", "TEXT NOT NULL DEFAULT 'local'"),
                ("direct_url", "TEXT NOT NULL DEFAULT ''"),
                ("commit_match", "INTEGER NOT NULL DEFAULT 0"),
                ("attempt_id", "TEXT NOT NULL DEFAULT ''"),
                ("capture_mode", "TEXT NOT NULL DEFAULT 'browser'"),
                ("entry_url", "TEXT NOT NULL DEFAULT ''"),
                ("review_status", "TEXT NOT NULL DEFAULT 'confirmed'"),
                ("reviewed_by", "TEXT NOT NULL DEFAULT '刘昱（按授权默认确认）'"),
                ("reviewed_at", "TEXT"),
            ):
                if name not in recording_columns:
                    c.execute("ALTER TABLE recordings ADD COLUMN %s %s" % (name, definition))
            attempt_columns = {row[1] for row in c.execute("PRAGMA table_info(recording_attempts)")}
            if "interaction_mode" not in attempt_columns:
                c.execute("ALTER TABLE recording_attempts ADD COLUMN interaction_mode TEXT NOT NULL DEFAULT 'auto'")
            c.execute("UPDATE recordings SET capture_mode='screen' WHERE attempt_id='' AND path LIKE '%.mov'")
            c.execute(
                """UPDATE recordings SET review_status='confirmed',
                     reviewed_by=CASE WHEN reviewed_by='' THEN '刘昱（按授权默认确认）' ELSE reviewed_by END,
                     reviewed_at=COALESCE(reviewed_at,updated_at)
                   WHERE status='passed'"""
            )
            c.execute(
                """INSERT OR IGNORE INTO recording_attempts(
                     id,pair_id,arm,commit_sha,path,capture_mode,entry_url,width,height,duration_seconds,
                     sha256,status,error,started_at,finished_at,created_at,updated_at)
                   SELECT CASE WHEN attempt_id<>'' THEN attempt_id ELSE 'legacy-'||id END,
                     pair_id,arm,commit_sha,path,COALESCE(NULLIF(capture_mode,''),'screen'),entry_url,
                     width,height,duration_seconds,sha256,status,error,started_at,finished_at,created_at,updated_at
                   FROM recordings WHERE path<>''"""
            )
            gsb_columns = {row[1] for row in c.execute("PRAGMA table_info(gsb_reviews)")}
            for name, definition in (
                ("draft_verdict", "TEXT NOT NULL DEFAULT ''"),
                ("draft_reason", "TEXT NOT NULL DEFAULT ''"),
                ("final_verdict", "TEXT NOT NULL DEFAULT ''"),
                ("final_reason", "TEXT NOT NULL DEFAULT ''"),
                ("evidence_version", "TEXT NOT NULL DEFAULT ''"),
                ("a_reason", "TEXT NOT NULL DEFAULT ''"),
                ("b_reason", "TEXT NOT NULL DEFAULT ''"),
                ("preference_reason", "TEXT NOT NULL DEFAULT ''"),
                ("a_score_delivery", "INTEGER NOT NULL DEFAULT 0"),
                ("a_desc_delivery", "TEXT NOT NULL DEFAULT ''"),
                ("b_score_delivery", "INTEGER NOT NULL DEFAULT 0"),
                ("b_desc_delivery", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in gsb_columns:
                    c.execute("ALTER TABLE gsb_reviews ADD COLUMN %s %s" % (name, definition))
            c.execute(
                """UPDATE gsb_reviews SET
                     draft_verdict=CASE WHEN draft_verdict='' THEN verdict ELSE draft_verdict END,
                     draft_reason=CASE WHEN draft_reason='' THEN reason ELSE draft_reason END,
                     final_verdict=CASE WHEN status='confirmed' AND final_verdict='' THEN verdict ELSE final_verdict END,
                     final_reason=CASE WHEN status='confirmed' AND final_reason='' THEN reason ELSE final_reason END"""
            )
            c.execute(
                """UPDATE gsb_reviews SET preference_reason=reason
                     WHERE preference_reason='' AND a_reason='' AND b_reason='' AND reason<>''"""
            )
            recheck_columns = {row[1] for row in c.execute("PRAGMA table_info(gsb_rechecks)")}
            for name, definition in (
                ("suggested_a_reason", "TEXT NOT NULL DEFAULT ''"),
                ("suggested_b_reason", "TEXT NOT NULL DEFAULT ''"),
                ("suggested_preference_reason", "TEXT NOT NULL DEFAULT ''"),
                ("applied_at", "TEXT"),
                ("applied_by", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in recheck_columns:
                    c.execute("ALTER TABLE gsb_rechecks ADD COLUMN %s %s" % (name, definition))
            delivery_columns = {row[1] for row in c.execute("PRAGMA table_info(delivery_submissions)")}
            for name, definition in (
                ("payload_sha256", "TEXT NOT NULL DEFAULT ''"),
                ("remote_status", "TEXT NOT NULL DEFAULT ''"),
                ("qc_summary", "TEXT NOT NULL DEFAULT ''"),
                ("remote_updated_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in delivery_columns:
                    c.execute("ALTER TABLE delivery_submissions ADD COLUMN %s %s" % (name, definition))
            c.execute(
                """UPDATE delivery_submissions SET status='ready_to_submit',error=''
                     WHERE status='needs_review' AND remote_id=''
                       AND EXISTS(SELECT 1 FROM gsb_reviews g
                                   WHERE g.pair_id=delivery_submissions.pair_id
                                     AND g.status='confirmed')
                       AND EXISTS(SELECT 1 FROM recording_attempts ra
                                   WHERE ra.pair_id=delivery_submissions.pair_id
                                     AND ra.interaction_mode='manual' AND ra.status='passed')"""
            )
            c.execute(
                """UPDATE recordings SET commit_sha=COALESCE((
                     SELECT commit_sha FROM arm_runs a
                      WHERE a.pair_id=recordings.pair_id AND a.arm=recordings.arm
                   ),'') WHERE commit_sha=''"""
            )
            c.execute(
                """UPDATE recordings SET commit_match=CASE WHEN EXISTS(
                     SELECT 1 FROM arm_runs a WHERE a.pair_id=recordings.pair_id
                      AND a.arm=recordings.arm AND a.commit_sha<>''
                      AND a.commit_sha=recordings.commit_sha
                   ) THEN 1 ELSE commit_match END
                   WHERE commit_sha<>''"""
            )
            c.execute(
                "INSERT INTO metadata(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.transaction() as c:
            cur = c.execute(sql, tuple(params))
            return int(cur.lastrowid or 0)

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        with self.connect() as c:
            row = c.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self.connect() as c:
            return [dict(row) for row in c.execute(sql, tuple(params)).fetchall()]

    def audit(self, event_type: str, entity_type: str = "", entity_id: str = "", detail: Any = None) -> None:
        self.execute(
            "INSERT INTO audit_events(event_type,entity_type,entity_id,detail_json,created_at) VALUES(?,?,?,?,?)",
            (event_type, entity_type, entity_id, json.dumps(detail or {}, ensure_ascii=False), now_iso()),
        )

    def setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value_json FROM settings WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except ValueError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
            (key, json.dumps(value, ensure_ascii=False), now_iso()),
        )

    def page(self, table: str, page: int, size: int, where: str = "1=1", params: Iterable[Any] = (), order: str = "created_at DESC") -> Dict[str, Any]:
        allowed = {
            "tasks", "project_chains", "pairs", "codex_jobs", "bug_candidates",
            "artifact_checks", "recordings", "gsb_reviews", "gsb_rechecks",
            "difficulty_reviews", "delivery_submissions", "audit_events", "git_repositories",
        }
        if table not in allowed:
            raise ValueError("unknown table")
        page, size = max(1, page), min(100, max(1, size))
        with self.connect() as c:
            total = c.execute("SELECT COUNT(*) FROM %s WHERE %s" % (table, where), tuple(params)).fetchone()[0]
            rows = c.execute(
                "SELECT * FROM %s WHERE %s ORDER BY %s LIMIT ? OFFSET ?" % (table, where, order),
                tuple(params) + (size, (page - 1) * size),
            ).fetchall()
        return {"items": [dict(r) for r in rows], "page": page, "size": size, "total": total}


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL DEFAULT '',
  task_type TEXT NOT NULL CHECK(task_type IN ('zero_to_one','feature','bugfix')),
  title TEXT NOT NULL,
  prompt TEXT NOT NULL,
  stack TEXT NOT NULL DEFAULT '',
  project_category TEXT NOT NULL DEFAULT '未记录',
  acceptance_json TEXT NOT NULL DEFAULT '[]',
  difficulty TEXT NOT NULL,
  difficulty_evidence_json TEXT NOT NULL DEFAULT '[]',
  estimated_module_count INTEGER NOT NULL DEFAULT 0,
  estimated_source_lines_min INTEGER NOT NULL DEFAULT 0,
  estimated_source_lines_max INTEGER NOT NULL DEFAULT 0,
  estimated_minutes_min INTEGER NOT NULL DEFAULT 0,
  estimated_minutes_max INTEGER NOT NULL DEFAULT 0,
  reviewed_minutes_min INTEGER NOT NULL DEFAULT 0,
  reviewed_minutes_max INTEGER NOT NULL DEFAULT 0,
  estimate_work_items_json TEXT NOT NULL DEFAULT '[]',
  estimate_risk TEXT NOT NULL DEFAULT '',
  complexity_axes_json TEXT NOT NULL DEFAULT '[]',
  baseline_path TEXT NOT NULL DEFAULT '',
  baseline_repo_url TEXT NOT NULL DEFAULT '',
  baseline_sha TEXT NOT NULL DEFAULT '',
  parent_pair_id TEXT NOT NULL DEFAULT '',
  fingerprint TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'candidate',
  rejection_reason TEXT NOT NULL DEFAULT '',
  locked_by TEXT NOT NULL DEFAULT '',
  used_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_pool ON tasks(status,difficulty,task_type,created_at);
CREATE TABLE IF NOT EXISTS generation_batches (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  requested_count INTEGER NOT NULL,
  generated_count INTEGER NOT NULL DEFAULT 0,
  accepted_count INTEGER NOT NULL DEFAULT 0,
  rejected_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS project_chains (
  id TEXT PRIMARY KEY,
  root_task_id TEXT NOT NULL REFERENCES tasks(id),
  status TEXT NOT NULL DEFAULT 'active',
  followup_required INTEGER NOT NULL DEFAULT 1,
  followup_completed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS pairs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  chain_id TEXT NOT NULL REFERENCES project_chains(id),
  status TEXT NOT NULL DEFAULT 'queued',
  stage TEXT NOT NULL DEFAULT 'queued',
  repo_id TEXT NOT NULL DEFAULT '',
  baseline_sha TEXT NOT NULL DEFAULT '',
  winner TEXT NOT NULL DEFAULT '',
  development_failure_count INTEGER NOT NULL DEFAULT 0,
  model_scheme TEXT NOT NULL DEFAULT 'legacy',
  model_a TEXT NOT NULL DEFAULT '',
  model_b TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pairs_status ON pairs(status,stage,created_at);
CREATE TABLE IF NOT EXISTS git_repositories (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL UNIQUE REFERENCES pairs(id),
  owner TEXT NOT NULL,
  name TEXT NOT NULL,
  visibility TEXT NOT NULL,
  remote_url TEXT NOT NULL DEFAULT '',
  local_root TEXT NOT NULL,
  main_sha TEXT NOT NULL DEFAULT '',
  a_sha TEXT NOT NULL DEFAULT '',
  b_sha TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'planned',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS arm_runs (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL REFERENCES pairs(id),
  arm TEXT NOT NULL CHECK(arm IN ('A','B')),
  branch TEXT NOT NULL CHECK(branch IN ('A','B')),
  workspace_path TEXT NOT NULL,
  container_name TEXT NOT NULL,
  screen_name TEXT NOT NULL,
  model TEXT NOT NULL,
  image TEXT NOT NULL,
  image_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  prompt_sent_at TEXT,
  finished_at TEXT,
  session_id TEXT NOT NULL DEFAULT '',
  prompt_id TEXT NOT NULL DEFAULT '',
  trace_path TEXT NOT NULL DEFAULT '',
  commit_sha TEXT NOT NULL DEFAULT '',
  exit_code INTEGER,
  result TEXT NOT NULL DEFAULT '',
  warning_at TEXT,
  attempt_no INTEGER NOT NULL DEFAULT 1,
  error_retry_count INTEGER NOT NULL DEFAULT 0,
  api_retry_count INTEGER NOT NULL DEFAULT 0,
  api_retry_after TEXT,
  last_api_error TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(pair_id,arm)
);
CREATE TABLE IF NOT EXISTS codex_jobs (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL DEFAULT '',
  job_type TEXT NOT NULL,
  model TEXT NOT NULL,
  reasoning_effort TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  cwd TEXT NOT NULL DEFAULT '',
  input_path TEXT NOT NULL DEFAULT '',
  schema_path TEXT NOT NULL DEFAULT '',
  events_path TEXT NOT NULL DEFAULT '',
  output_path TEXT NOT NULL DEFAULT '',
  exit_code INTEGER,
  result_json TEXT NOT NULL DEFAULT '{}',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_codex_jobs_status ON codex_jobs(status,job_type,created_at);
CREATE TABLE IF NOT EXISTS bug_candidates (
  id TEXT PRIMARY KEY,
  source_pair_id TEXT NOT NULL REFERENCES pairs(id),
  source_arm TEXT NOT NULL CHECK(source_arm IN ('A','B')),
  source_sha TEXT NOT NULL,
  title TEXT NOT NULL,
  preconditions TEXT NOT NULL,
  reproduction_steps_json TEXT NOT NULL,
  reproduction_commands_json TEXT NOT NULL DEFAULT '[]',
  reproduction_results_json TEXT NOT NULL DEFAULT '[]',
  source_paths_json TEXT NOT NULL DEFAULT '[]',
  actual_result TEXT NOT NULL,
  expected_result TEXT NOT NULL,
  reproduce_count INTEGER NOT NULL DEFAULT 0,
  difficulty TEXT NOT NULL DEFAULT 'pending',
  difficulty_evidence_json TEXT NOT NULL DEFAULT '[]',
  estimated_module_count INTEGER NOT NULL DEFAULT 0,
  estimated_source_lines_min INTEGER NOT NULL DEFAULT 0,
  estimated_source_lines_max INTEGER NOT NULL DEFAULT 0,
  estimated_minutes_min INTEGER NOT NULL DEFAULT 0,
  estimated_minutes_max INTEGER NOT NULL DEFAULT 0,
  complexity_axes_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'candidate',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_checks (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL REFERENCES pairs(id),
  arm TEXT NOT NULL CHECK(arm IN ('A','B')),
  commit_sha TEXT NOT NULL,
  compose_file TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  checks_json TEXT NOT NULL DEFAULT '[]',
  started_at TEXT,
  finished_at TEXT,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(pair_id,arm,commit_sha)
);
CREATE TABLE IF NOT EXISTS recordings (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL REFERENCES pairs(id),
  arm TEXT NOT NULL CHECK(arm IN ('A','B')),
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL DEFAULT '',
  width INTEGER NOT NULL DEFAULT 0,
  height INTEGER NOT NULL DEFAULT 0,
  duration_seconds REAL NOT NULL DEFAULT 0,
  commit_sha TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  finished_at TEXT,
  steps_json TEXT NOT NULL DEFAULT '[]',
  upload_status TEXT NOT NULL DEFAULT 'local',
  direct_url TEXT NOT NULL DEFAULT '',
  commit_match INTEGER NOT NULL DEFAULT 0,
  attempt_id TEXT NOT NULL DEFAULT '',
  capture_mode TEXT NOT NULL DEFAULT 'browser',
  entry_url TEXT NOT NULL DEFAULT '',
  review_status TEXT NOT NULL DEFAULT 'confirmed',
  reviewed_by TEXT NOT NULL DEFAULT '刘昱（按授权默认确认）',
  reviewed_at TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(pair_id,arm)
);
CREATE TABLE IF NOT EXISTS recording_attempts (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL REFERENCES pairs(id),
  arm TEXT NOT NULL CHECK(arm IN ('A','B')),
  commit_sha TEXT NOT NULL,
  path TEXT NOT NULL,
  capture_mode TEXT NOT NULL DEFAULT 'browser',
  interaction_mode TEXT NOT NULL DEFAULT 'auto',
  entry_url TEXT NOT NULL DEFAULT '',
  runtime_port INTEGER NOT NULL DEFAULT 0,
  runtime_project TEXT NOT NULL DEFAULT '',
  compose_file TEXT NOT NULL DEFAULT '',
  width INTEGER NOT NULL DEFAULT 0,
  height INTEGER NOT NULL DEFAULT 0,
  duration_seconds REAL NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'starting',
  error TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recording_attempts_pair_arm ON recording_attempts(pair_id,arm,created_at DESC);
CREATE TABLE IF NOT EXISTS difficulty_reviews (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL UNIQUE REFERENCES pairs(id),
  original_difficulty TEXT NOT NULL,
  a_difficulty TEXT NOT NULL DEFAULT '',
  b_difficulty TEXT NOT NULL DEFAULT '',
  assessed_difficulty TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  a_commit_sha TEXT NOT NULL DEFAULT '',
  b_commit_sha TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT NOT NULL DEFAULT '',
  reviewed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_difficulty_reviews_status ON difficulty_reviews(status,updated_at);
CREATE TABLE IF NOT EXISTS gsb_reviews (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL UNIQUE REFERENCES pairs(id),
  verdict TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  draft_verdict TEXT NOT NULL DEFAULT '',
  draft_reason TEXT NOT NULL DEFAULT '',
  final_verdict TEXT NOT NULL DEFAULT '',
  final_reason TEXT NOT NULL DEFAULT '',
  evidence_version TEXT NOT NULL DEFAULT '',
  a_reason TEXT NOT NULL DEFAULT '',
  b_reason TEXT NOT NULL DEFAULT '',
  preference_reason TEXT NOT NULL DEFAULT '',
  a_score_delivery INTEGER NOT NULL DEFAULT 0,
  a_desc_delivery TEXT NOT NULL DEFAULT '',
  b_score_delivery INTEGER NOT NULL DEFAULT 0,
  b_desc_delivery TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  confirmed_by TEXT NOT NULL DEFAULT '',
  confirmed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gsb_rechecks (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL REFERENCES pairs(id),
  evidence_version TEXT NOT NULL,
  input_verdict TEXT NOT NULL,
  input_reason TEXT NOT NULL,
  result_status TEXT NOT NULL CHECK(result_status IN ('passed','suggested_revision','fact_conflict')),
  suggested_verdict TEXT NOT NULL DEFAULT '',
  suggested_reason TEXT NOT NULL DEFAULT '',
  suggested_a_reason TEXT NOT NULL DEFAULT '',
  suggested_b_reason TEXT NOT NULL DEFAULT '',
  suggested_preference_reason TEXT NOT NULL DEFAULT '',
  issues_json TEXT NOT NULL DEFAULT '[]',
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  model TEXT NOT NULL,
  reasoning_effort TEXT NOT NULL,
  codex_job_id TEXT NOT NULL DEFAULT '',
  applied_at TEXT,
  applied_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gsb_rechecks_pair ON gsb_rechecks(pair_id,created_at DESC);
CREATE TABLE IF NOT EXISTS delivery_submissions (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL UNIQUE REFERENCES pairs(id),
  status TEXT NOT NULL DEFAULT 'not_submitted',
  remote_id TEXT NOT NULL DEFAULT '',
  remote_url TEXT NOT NULL DEFAULT '',
  payload_sha256 TEXT NOT NULL DEFAULT '',
  remote_status TEXT NOT NULL DEFAULT '',
  qc_summary TEXT NOT NULL DEFAULT '',
  remote_updated_at TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  hidden_at TEXT,
  submitted_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  entity_type TEXT NOT NULL DEFAULT '',
  entity_id TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_entity_event ON audit_events(entity_id,event_type,id);
CREATE TABLE IF NOT EXISTS pipeline_operations (
  operation TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  error_kind TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  retry_after TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bug_task_source_unique ON tasks(source,source_id)
 WHERE source='bug_discovery' AND source_id<>'';
CREATE VIEW IF NOT EXISTS development_pairs AS
 SELECT * FROM pairs WHERE stage NOT IN ('artifact_validation','difficulty_review','recording','gsb_ready','gsb_confirmation');
CREATE TABLE IF NOT EXISTS bug_verification_results (
 pair_id TEXT NOT NULL, arm TEXT NOT NULL, commit_sha TEXT NOT NULL,
 verifier_hash TEXT NOT NULL, status TEXT NOT NULL, evidence_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(pair_id,arm,commit_sha,verifier_hash)
);
CREATE TABLE IF NOT EXISTS evidence_cache (
 cache_key TEXT PRIMARY KEY, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_resources (
 project TEXT PRIMARY KEY, owner_pid INTEGER NOT NULL, workspace TEXT NOT NULL,
 compose_file TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""
