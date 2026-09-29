#!/usr/bin/env python3
"""Export only unused ready task definitions, or import them on a new host.

No Pair, audit, submission, recording, trajectory, or database file is copied.
Feature/Bug baselines are referenced by their repository URL and exact commit;
the importer fetches those repositories into a local cache on the new host.
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pairwise_console.db import Database, now_iso  # noqa: E402
from pairwise_console.importer import fingerprint  # noqa: E402


TASK_FIELDS = (
    "id", "task_type", "title", "prompt", "stack", "project_category",
    "acceptance_json", "difficulty", "difficulty_evidence_json",
    "estimated_module_count", "estimated_source_lines_min",
    "estimated_source_lines_max", "estimated_minutes_min",
    "estimated_minutes_max", "reviewed_minutes_min", "reviewed_minutes_max",
    "estimate_work_items_json", "estimate_risk", "complexity_axes_json",
    "repair_verification_json", "baseline_repo_url", "baseline_sha",
    "fingerprint",
)
ARCHIVE_FIELDS = (
    "id", "task_type", "title", "prompt", "stack", "project_category",
    "acceptance_json", "difficulty", "baseline_sha", "fingerprint", "created_at",
)
JSON_FIELDS = (
    "acceptance_json", "difficulty_evidence_json", "estimate_work_items_json",
    "complexity_axes_json", "repair_verification_json",
)
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


def _git(*args):
    result = subprocess.run(["git", *map(str, args)], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("git %s: %s" % (args[0], result.stderr.strip()))
    return result.stdout.strip()


def _check_task(task, local_path=""):
    if task["task_type"] not in ("zero_to_one", "feature", "bugfix"):
        raise ValueError("不支持的题型：%s" % task["id"])
    if task["difficulty"] not in ("困难", "地狱") or not task["prompt"].strip():
        raise ValueError("题目未达到可用条件：%s" % task["id"])
    if task["fingerprint"] != fingerprint(
        task["task_type"], task["prompt"], task["baseline_sha"]
    ):
        raise ValueError("题目指纹不一致：%s" % task["id"])
    for field in JSON_FIELDS:
        json.loads(task[field])
    if task["task_type"] == "zero_to_one":
        if task["baseline_repo_url"] or task["baseline_sha"]:
            raise ValueError("0–1 题不应携带基线：%s" % task["id"])
    else:
        if not task["baseline_repo_url"] or not SHA_PATTERN.fullmatch(task["baseline_sha"]):
            raise ValueError("题目缺少可迁移的远端基线：%s" % task["id"])
        if local_path:
            path = Path(local_path)
            if not path.is_dir():
                raise ValueError("本地基线目录不存在：%s" % task["id"])
            _git("-C", path, "cat-file", "-e", task["baseline_sha"] + "^{commit}")


def export_tasks(db_path, output, archive_output=None):
    source = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute(
            """SELECT t.* FROM tasks t WHERE t.status='ready'
                 AND NOT EXISTS (SELECT 1 FROM pairs p WHERE p.task_id=t.id)
                 ORDER BY t.created_at,t.id"""
        ).fetchall()
        tasks = []
        for row in rows:
            task = {field: row[field] for field in TASK_FIELDS}
            _check_task(task, row["baseline_path"])
            tasks.append(task)
        archived = []
        if archive_output:
            completed = source.execute(
                """SELECT DISTINCT t.* FROM tasks t JOIN pairs p ON p.task_id=t.id
                     WHERE p.status='completed' ORDER BY t.created_at,t.id"""
            ).fetchall()
            for row in completed:
                item = {field: row[field] for field in ARCHIVE_FIELDS}
                if not item["prompt"].strip():
                    raise ValueError("已完成题目缺少题面：%s" % item["id"])
                # The stored fingerprint can predate a corrected prompt. For
                # deduplication, index the final task text, not that stale key.
                item["fingerprint"] = fingerprint(
                    item["task_type"], item["prompt"], item["baseline_sha"]
                )
                json.loads(item["acceptance_json"])
                archived.append(item)
            if len({item["fingerprint"] for item in archived}) != len(archived):
                raise ValueError("已完成题目归档含重复指纹")
    finally:
        source.close()
    payload = {
        "format": "claude-pairwise-ready-tasks-v1",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tasks": tasks,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("导出 %d 道 ready、未绑定 Pair 的题目：%s" % (len(tasks), output))
    if archive_output:
        archive_output = Path(archive_output)
        archive_output.parent.mkdir(parents=True, exist_ok=True)
        archive_output.write_text(json.dumps({
            "format": "claude-pairwise-completed-dedup-v1",
            "exported_at": payload["exported_at"], "tasks": archived,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("导出 %d 道已完成题面的只读去重索引：%s" % (len(archived), archive_output))


def _clone_url(url, transport):
    if transport == "ssh" and url.startswith("https://github.com/"):
        return "git@github.com:" + url[len("https://github.com/"):]
    return url


def _canonical_repo_url(url):
    """Compare GitHub remotes even when Git rewrites SSH to HTTPS."""
    value = str(url or "").strip()
    for prefix in ("git@github.com:", "ssh://git@github.com/"):
        if value.startswith(prefix):
            value = "https://github.com/" + value[len(prefix):]
            break
    return value.rstrip("/").removesuffix(".git").casefold()


def _read_archive(archive_bundle):
    if not archive_bundle:
        return []
    payload = json.loads(Path(archive_bundle).read_text(encoding="utf-8"))
    if payload.get("format") != "claude-pairwise-completed-dedup-v1":
        raise ValueError("已完成题面归档格式不受支持")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("已完成题面归档格式错误")
    seen = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) != set(ARCHIVE_FIELDS):
            raise ValueError("已完成题面归档含非题目字段")
        if not task["prompt"].strip() or task["fingerprint"] != fingerprint(
            task["task_type"], task["prompt"], task["baseline_sha"]
        ):
            raise ValueError("已完成题面或指纹无效：%s" % task["id"])
        if task["id"] in seen:
            raise ValueError("已完成题目 ID 重复：%s" % task["id"])
        json.loads(task["acceptance_json"])
        seen.add(task["id"])
    return tasks


def import_tasks(bundle, db_path, baseline_dir, apply, transport, archive_bundle=None):
    payload = json.loads(Path(bundle).read_text(encoding="utf-8"))
    if payload.get("format") != "claude-pairwise-ready-tasks-v1":
        raise ValueError("题目包格式不受支持")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("题目包为空")
    ids = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) != set(TASK_FIELDS):
            raise ValueError("题目字段不符合纯题目迁移格式")
        _check_task(task)
        if task["id"] in ids:
            raise ValueError("题目 ID 重复：%s" % task["id"])
        ids.add(task["id"])
    archived = _read_archive(archive_bundle)
    if any(task["id"] in ids for task in archived):
        raise ValueError("待做题和已完成归档出现同一个 ID")
    if not apply:
        print("检查通过：%d 道待做题、%d 道已完成题面；未写入数据库或拉取基线。加 --apply 才执行导入。" % (
            len(tasks), len(archived)
        ))
        return

    db_path = Path(db_path).expanduser().resolve()
    baseline_dir = Path(baseline_dir).expanduser().resolve()
    db = Database(db_path)
    db.initialize()
    for task in tasks:
        existing = db.one("SELECT id FROM tasks WHERE id=? OR fingerprint=?", (
            task["id"], task["fingerprint"]
        ))
        if existing:
            print("跳过已有题目：%s" % task["id"])
            continue
        baseline_path = ""
        if task["task_type"] != "zero_to_one":
            baseline_dir.mkdir(parents=True, exist_ok=True)
            path = baseline_dir / task["id"]
            clone_url = _clone_url(task["baseline_repo_url"], transport)
            newly_cloned = not path.exists()
            if newly_cloned:
                _git("clone", "--no-checkout", clone_url, path)
            if not (path / ".git").is_dir():
                raise RuntimeError("基线路径不是预期的 Git 仓库：%s" % path)
            current_url = _git("-C", path, "remote", "get-url", "origin")
            if _canonical_repo_url(current_url) != _canonical_repo_url(clone_url) or (not newly_cloned and _git(
                "-C", path, "status", "--porcelain"
            )):
                raise RuntimeError("已有基线仓库的远端不符或含未提交修改：%s" % path)
            _git("-C", path, "cat-file", "-e", task["baseline_sha"] + "^{commit}")
            _git("-C", path, "checkout", "--detach", task["baseline_sha"])
            baseline_path = str(path)
        stamp = now_iso()
        columns = (
            "id", "source", "source_id", "task_type", "title", "prompt", "stack",
            "project_category", "acceptance_json", "difficulty", "difficulty_evidence_json",
            "estimated_module_count", "estimated_source_lines_min", "estimated_source_lines_max",
            "estimated_minutes_min", "estimated_minutes_max", "reviewed_minutes_min",
            "reviewed_minutes_max", "estimate_work_items_json", "estimate_risk",
            "complexity_axes_json", "repair_verification_json", "baseline_path",
            "baseline_repo_url", "baseline_sha", "parent_pair_id", "fingerprint",
            "status", "created_at", "updated_at",
        )
        values = {
            **task, "source": "legacy", "source_id": task["id"],
            "baseline_path": baseline_path, "parent_pair_id": "", "status": "ready",
            "created_at": stamp, "updated_at": stamp,
        }
        placeholders = ",".join("?" for _ in columns)
        db.execute("INSERT INTO tasks(%s) VALUES(%s)" % (",".join(columns), placeholders),
                   tuple(values[column] for column in columns))
        print("已导入：%s" % task["id"])
    archived_count = 0
    for task in archived:
        if db.one("SELECT id FROM tasks WHERE id=? OR fingerprint=?", (
            task["id"], task["fingerprint"]
        )):
            continue
        columns = (
            "id", "source", "source_id", "task_type", "title", "prompt", "stack",
            "project_category", "acceptance_json", "difficulty", "baseline_sha",
            "fingerprint", "status", "created_at", "updated_at",
        )
        values = {
            **task, "source": "migration_archive", "source_id": task["id"],
            "status": "archived_dedup", "updated_at": now_iso(),
        }
        db.execute("INSERT INTO tasks(%s) VALUES(%s)" % (
            ",".join(columns), ",".join("?" for _ in columns)
        ), tuple(values[column] for column in columns))
        archived_count += 1
    print("已导入 %d 道已完成题面，仅用于查重，不进入待做题池。" % archived_count)
    print("导入结束；未复制原数据库、Pair、日志或录像。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    export = sub.add_parser("export")
    export.add_argument("--db", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--archive-output", type=Path)
    importer = sub.add_parser("import")
    importer.add_argument("--bundle", type=Path, required=True)
    importer.add_argument("--archive-bundle", type=Path)
    importer.add_argument("--db", type=Path, required=True)
    importer.add_argument("--baseline-dir", type=Path, required=True)
    importer.add_argument("--transport", choices=("ssh", "https"), default="ssh")
    importer.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.action == "export":
        export_tasks(args.db, args.output, args.archive_output)
    else:
        import_tasks(args.bundle, args.db, args.baseline_dir, args.apply, args.transport,
                     args.archive_bundle)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, sqlite3.Error, OSError) as exc:
        parser_message = "题目迁移失败：%s" % exc
        print(parser_message, file=sys.stderr)
        raise SystemExit(1)
