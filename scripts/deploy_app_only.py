"""Application-only upgrade. Refuses to interrupt background work.

Stop new scheduling using the maintenance settings first. Claude Docker/Screen
sessions are independent and must never be stopped by this script.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import sys


FILES = (
    "pairwise_console/service.py", "pairwise_console/api.py", "pairwise_console/db.py",
    "pairwise_console/config.py",
    "pairwise_console/analytics.py", "pairwise_console/artifact.py",
    "pairwise_console/prompts.py", "pairwise_console/gsb_rewrite.py",
    "pairwise_console/codex_runner.py",
    "pairwise_console/claude_runner.py", "pairwise_console/gitops.py",
    "pairwise_console/recording.py", "pairwise_console/recording_similarity.py",
    "pairwise_console/pipeline_state.py", "pairwise_console/bug_verification.py",
    "pairwise_console/resources.py", "scripts/browser_recorder.mjs",
    "scripts/recording_openapi.mjs",
    "pairwise_console/task_estimation.py", "scripts/recording_timing.mjs", "web/app.js",
    "web/extra.css",
    "chrome-solo-qa-gsb-helper/background.js",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    app_home = Path.home() / "Library/Application Support/Claude A-B GSB Console"
    app = app_home / "app"
    database = app_home / ".data/pairwise.db"
    with sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True) as db:
        settings = {key: json.loads(value) for key, value in db.execute("SELECT key,value_json FROM settings")}
        paused = settings.get("pipeline_drain") or (
            not settings.get("auto_pipeline_enabled", True)
            and not settings.get("manual_bug_auto_refill_enabled", True)
            and not settings.get("auto_refill_enabled", True)
        )
        if args.apply and not paused:
            raise SystemExit("Refusing upgrade: pause new scheduling first")
        busy = []
        for table, condition in (
            ("codex_jobs", "status='running'"),
            ("artifact_checks", "status='running'"),
            ("recording_attempts", "status IN ('starting','recording','stopping')"),
            ("generation_batches", "status='running'"),
            ("bug_candidates", "status='reproducing'"),
            ("arm_runs", "status IN ('checkpointing','exporting','manual_preparing')"),
        ):
            busy.extend((table, row[0]) for row in db.execute("SELECT id FROM " + table + " WHERE " + condition))
        print(json.dumps({"busy": busy, "paused": bool(paused)}, ensure_ascii=False), flush=True)
        if busy or not args.apply:
            raise SystemExit(2 if busy else 0)
    backup = app_home / ".data/backups" / ("application-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(str(database)) as current, sqlite3.connect(str(backup / "pairwise.db")) as saved:
        current.backup(saved)
    # Additive schema must exist before any request can load updated code.
    sys.path.insert(0, str(source))
    from pairwise_console.db import Database
    Database(database).initialize()
    manifest = {}
    with tempfile.TemporaryDirectory(prefix="app-staging-", dir=str(app_home)) as staging:
        for name in FILES:
            original, target = source / name, app / name
            staged = Path(staging) / name
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, staged)
            if target.exists():
                previous = backup / name
                previous.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, previous)
            manifest[name] = hashlib.sha256(staged.read_bytes()).hexdigest()
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        for name in FILES:
            (app / name).parent.mkdir(parents=True, exist_ok=True)
            os.replace(Path(staging) / name, app / name)
    subprocess.run(["launchctl", "kickstart", "-k", "gui/%d/com.local.claude-pairwise-gsb-console" % os.getuid()], check=True)
    for _ in range(30):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8865/api/health", timeout=2) as response:
                if json.load(response).get("ok"):
                    print(json.dumps({"healthy": True, "backup": str(backup), "files": len(FILES)}), flush=True)
                    return
        except Exception:
            time.sleep(1)
    raise SystemExit("Health check failed; preserve maintenance pause and inspect backup: " + str(backup))


if __name__ == "__main__":
    main()
