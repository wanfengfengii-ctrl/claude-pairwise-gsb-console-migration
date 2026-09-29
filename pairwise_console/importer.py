import hashlib
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict

from .db import Database, now_iso
from .classification import normalize_project_category, normalize_stack


TASK_TYPES = {
    "0-1 代码生成": "zero_to_one",
    "0-1 重跑": "zero_to_one",
    "Feature 迭代": "feature",
    "Feature 迭代重跑": "feature",
}


def fingerprint(task_type: str, prompt: str, baseline_sha: str) -> str:
    normalized = " ".join(prompt.casefold().split())
    return hashlib.sha256((task_type + "\n" + normalized + "\n" + baseline_sha).encode("utf-8")).hexdigest()


def import_historical_tasks(db: Database, old_db_path: Path, limit: int = 500) -> Dict[str, int]:
    stats = {"scanned": 0, "imported": 0, "skipped": 0}
    if not old_db_path.exists():
        return stats
    source = sqlite3.connect("file:%s?mode=ro" % old_db_path, uri=True)
    source.row_factory = sqlite3.Row
    try:
        source_columns = {row[1] for row in source.execute("PRAGMA table_info(runs)").fetchall()}
        category_select = "project_category" if "project_category" in source_columns else "'未记录' AS project_category"
        rows = source.execute(
            """SELECT id,repo_name,task_type,task_difficulty,language_framework,%s,repo_path,repo_url,
                      base_sha,first_prompt,status_detail,phase,created_at
               FROM runs
               WHERE deleted_at IS NULL AND task_difficulty IN ('困难','地狱')
                 AND task_type IN ('0-1 代码生成','0-1 重跑','Feature 迭代','Feature 迭代重跑')
               ORDER BY created_at DESC LIMIT ?""" % category_select,
            (max(1, min(limit, 2000)),),
        ).fetchall()
        for row in rows:
            stats["scanned"] += 1
            kind = TASK_TYPES.get(row["task_type"])
            prompt = str(row["first_prompt"] or "").strip()
            if not kind or not prompt:
                stats["skipped"] += 1
                continue
            base_sha = str(row["base_sha"] or "")
            key = fingerprint(kind, prompt, base_sha)
            category = normalize_project_category(row["project_category"], row["language_framework"], prompt)
            existing = db.one("SELECT id,project_category FROM tasks WHERE fingerprint=?", (key,))
            if existing:
                if existing.get("project_category") != category:
                    db.execute("UPDATE tasks SET project_category=?,updated_at=? WHERE id=?", (category, now_iso(), existing["id"]))
                stats["skipped"] += 1
                continue
            # A finished 0-1 task is reused as a prompt with a clean baseline. A
            # feature keeps its recorded task-time repository location for later
            # baseline verification before it can be paired.
            baseline_path = str(row["repo_path"] or "") if kind == "feature" else ""
            baseline_complete = bool(kind == "zero_to_one" or (baseline_path and base_sha))
            task_id = "task-" + uuid.uuid4().hex[:16]
            stamp = now_iso()
            db.execute(
                """INSERT INTO tasks(id,source,source_id,task_type,title,prompt,stack,project_category,difficulty,
                   difficulty_evidence_json,baseline_path,baseline_repo_url,baseline_sha,fingerprint,status,
                   rejection_reason,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, "legacy", row["id"], kind, row["repo_name"], prompt,
                 normalize_stack(row["language_framework"]), category, row["task_difficulty"],
                 '["来源记录已判定为困难或地狱","进入 Pair 前仍需完成禁题、去重和基线复核"]',
                 baseline_path, str(row["repo_url"] or ""), base_sha, key,
                 "candidate" if baseline_complete else "rejected",
                 "" if baseline_complete else "历史 Feature 缺少准确基线位置或提交", stamp, stamp),
            )
            stats["imported"] += 1
        db.audit("legacy.imported", "task", "", stats)
        return stats
    finally:
        source.close()
