import json
import os
import re
import shlex
import shutil
import time
import uuid
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

from .commands import CommandError, redact, run_command
from .config import Config
from .db import Database, now_iso


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.").lower()
    return value[:70] or "pair-project"


class GitOps:
    GENERATED_DEPENDENCY_COMPONENTS = frozenset({
        ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".pnpm-store",
    })
    LOCAL_EXCLUDE_PATTERNS = (
        ".venv/", "venv/", "node_modules/", "__pycache__/", ".pytest_cache/",
        ".mypy_cache/", ".ruff_cache/", ".tox/", ".nox/", ".pnpm-store/",
        "*.py[cod]",
    )
    SOURCE_EXTENSIONS = frozenset({
        ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
        ".java", ".js", ".mjs", ".cjs", ".jsx", ".json", ".kt", ".kts", ".php", ".py",
        ".rb", ".rs", ".scss", ".sh", ".sql", ".svelte", ".swift", ".toml",
        ".ts", ".tsx", ".vue", ".xml", ".yaml", ".yml",
    })
    SOURCE_FILENAMES = frozenset({
        "containerfile", "dockerfile", "makefile", "compose.yaml", "compose.yml",
        "docker-compose.yaml", "docker-compose.yml",
    })
    LOCK_FILENAMES = frozenset({
        "bun.lock", "bun.lockb", "cargo.lock", "composer.lock", "package-lock.json",
        "pnpm-lock.yaml", "poetry.lock", "uv.lock", "yarn.lock",
    })

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    @classmethod
    def _generated_dependency_root(cls, value: str) -> str:
        parts = tuple(part for part in PurePosixPath(value.replace("\\", "/")).parts if part not in ("", "."))
        for index, part in enumerate(parts):
            if part.casefold() in cls.GENERATED_DEPENDENCY_COMPONENTS:
                return "/".join(parts[:index + 1])
        if parts and parts[-1].casefold().endswith((".pyc", ".pyo")):
            return "/".join(parts)
        return ""

    @classmethod
    def _is_source_delivery_path(cls, value: str) -> bool:
        if cls._generated_dependency_root(value):
            return False
        path = PurePosixPath(value.replace("\\", "/"))
        name = path.name.casefold()
        if name in cls.LOCK_FILENAMES or path.suffix.casefold() == ".lock":
            return False
        return name in cls.SOURCE_FILENAMES or path.suffix.casefold() in cls.SOURCE_EXTENSIONS

    def _ensure_local_excludes(self, workspace: Path) -> None:
        """Keep generated dependencies out of an Arm without changing its baseline."""
        git_path = run_command(
            ["git", "rev-parse", "--git-path", "info/exclude"], cwd=workspace, timeout=15,
        ).stdout.strip()
        exclude = Path(git_path)
        if not exclude.is_absolute():
            exclude = workspace / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
        known = {line.strip() for line in current.splitlines()}
        missing = [pattern for pattern in self.LOCAL_EXCLUDE_PATTERNS if pattern not in known]
        if not missing:
            return
        prefix = "" if not current or current.endswith("\n") else "\n"
        exclude.write_text(current + prefix + "\n".join(missing) + "\n", encoding="utf-8")

    def delivery_diff_summary(self, workspace: Path, baseline_sha: str, commit_sha: str) -> Dict[str, Any]:
        """Describe platform-visible source changes while excluding generated dependencies."""
        result = run_command(
            ["git", "diff", "--numstat", "--no-renames", "-z", "%s..%s" % (baseline_sha, commit_sha)],
            cwd=workspace, timeout=60,
        )
        changed_files: List[str] = []
        generated_files: List[str] = []
        source_files: List[str] = []
        source_line_changes = 0
        for record in result.stdout.split("\0"):
            if not record:
                continue
            fields = record.split("\t", 2)
            if len(fields) != 3:
                continue
            additions, deletions, path = fields
            changed_files.append(path)
            if self._generated_dependency_root(path):
                generated_files.append(path)
                continue
            if not self._is_source_delivery_path(path):
                continue
            source_files.append(path)
            if additions.isdigit():
                source_line_changes += int(additions)
            if deletions.isdigit():
                source_line_changes += int(deletions)
        return {
            "changed_files": changed_files,
            "generated_files": generated_files,
            "generated_roots": sorted({
                self._generated_dependency_root(path) for path in generated_files
                if self._generated_dependency_root(path)
            }),
            "source_files": source_files,
            "source_line_changes": source_line_changes,
        }

    @classmethod
    def _is_effective_business_source_path(cls, value: str) -> bool:
        """Exclude tests, build/config files and generated output from difficulty counts."""
        if not cls._is_source_delivery_path(value):
            return False
        path = PurePosixPath(value.replace("\\", "/"))
        parts = {part.casefold() for part in path.parts}
        name = path.name.casefold()
        if parts & {"test", "tests", "__tests__", "docs", "dist", "build", "generated"}:
            return False
        if (name.startswith(("test_", "repro_", "reproduce_")) or name.endswith(("_test.py", ".test.ts", ".test.tsx",
                                                          ".test.js", ".test.jsx", ".spec.ts",
                                                          ".spec.tsx", ".spec.js", ".spec.jsx"))):
            return False
        if name in cls.SOURCE_FILENAMES or name in {"package.json", "tsconfig.json", "vite.config.ts"}:
            return False
        return True

    def effective_business_source_diff(self, workspace: Path, baseline_sha: str,
                                       commit_sha: str) -> Dict[str, Any]:
        """Count non-whitespace business source additions/deletions by file."""
        result = run_command(
            ["git", "diff", "--numstat", "--no-renames", "--ignore-all-space", "-z",
             "%s..%s" % (baseline_sha, commit_sha)],
            cwd=workspace, timeout=60,
        )
        files: List[Dict[str, Any]] = []
        for record in result.stdout.split("\0"):
            fields = record.split("\t", 2)
            if len(fields) != 3 or not self._is_effective_business_source_path(fields[2]):
                continue
            additions, deletions, path = fields
            if additions.isdigit() and deletions.isdigit():
                files.append({"path": path, "additions": int(additions),
                              "deletions": int(deletions)})
        return {
            "files": files,
            "moduleCount": len(files),
            "additions": sum(item["additions"] for item in files),
            "deletions": sum(item["deletions"] for item in files),
        }

    def _remove_generated_dependencies_from_index(self, workspace: Path, baseline_sha: str) -> List[str]:
        changed = run_command(
            ["git", "diff", "--cached", "--name-only", "--no-renames", "-z", baseline_sha],
            cwd=workspace, timeout=60,
        )
        targets = sorted({
            root for root in (
                self._generated_dependency_root(path) for path in changed.stdout.split("\0") if path
            ) if root
        })
        for start in range(0, len(targets), 100):
            run_command(
                ["git", "reset", "-q", baseline_sha, "--"] + targets[start:start + 100],
                cwd=workspace, timeout=60,
            )
        return targets

    def preflight(self) -> Dict[str, Any]:
        author_name = str(self.db.setting("git_author_name", self.config.git_author_name))
        author_email = str(self.db.setting("git_author_email", self.config.git_author_email))
        result: Dict[str, Any] = {
            "ok": True,
            "git": {},
            "gh": {},
            "author": {"name": author_name, "email": author_email},
        }
        for binary in ("git", "gh"):
            path = shutil.which(binary)
            item = {"binary": path or "", "ok": bool(path), "version": "", "error": ""}
            if path:
                try:
                    probe = run_command([path, "--version"], check=False, timeout=15)
                    item.update(ok=probe.returncode == 0, version=(probe.stdout or probe.stderr).splitlines()[0])
                except Exception as exc:
                    item.update(ok=False, error=str(exc))
            result[binary] = item
            result["ok"] = result["ok"] and item["ok"]
        if result["gh"]["ok"]:
            auth = run_command(["gh", "auth", "status"], check=False, timeout=20)
            result["gh"]["authenticated"] = auth.returncode == 0
            result["gh"]["error"] = "" if auth.returncode == 0 else redact(auth.stderr or auth.stdout)
            account = run_command(["gh", "api", "user", "--jq", ".login"], check=False, timeout=20)
            result["gh"]["account"] = account.stdout.strip() if account.returncode == 0 else ""
            result["ok"] = result["ok"] and auth.returncode == 0
        if not author_email:
            result["ok"] = False
            result["author"]["error"] = "尚未配置 Git 提交邮箱"
        return result

    @staticmethod
    def _github_git(args, cwd: Optional[Path] = None, timeout: int = 180, check: bool = True):
        """Run GitHub network Git without user URL rewrite rules.

        This machine may use a github.com mirror through a global insteadOf
        rule. Background jobs must still use gh's credential helper against
        GitHub itself, without changing the user's global Git configuration.
        """
        gh = shutil.which("gh")
        if not gh:
            raise RuntimeError("找不到 gh CLI，无法为 GitHub 网络操作提供凭据")
        env = os.environ.copy()
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        command = [
            "git", "-c", "credential.helper=",
            "-c", "credential.helper=!%s auth git-credential" % shlex.quote(gh),
        ] + list(args)
        return run_command(command, cwd=cwd, timeout=timeout, check=check, env=env)

    def _remove_stale_index_lock(self, workspace: Path, stale_seconds: int = 30) -> bool:
        """Remove an old Git index lock only after confirming it has no owner."""
        lock = workspace / ".git" / "index.lock"
        if not lock.is_file():
            return False
        age = max(0.0, time.time() - lock.stat().st_mtime)
        if age < stale_seconds:
            return False
        lsof = shutil.which("lsof")
        if not lsof:
            return False
        owner = run_command([lsof, "-t", "--", str(lock)], check=False, timeout=15)
        if owner.stdout.strip():
            return False
        lock.unlink(missing_ok=True)
        self.db.audit("git.stale_index_lock_removed", "workspace", str(workspace), {
            "path": str(lock), "ageSeconds": int(age), "ownerCheck": "lsof",
        })
        return True

    def create_pair_repository(self, pair: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
        pair_id = pair["id"]
        existing = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,))
        if existing and existing["status"] == "ready":
            return existing
        owner = str(self.db.setting("github_owner", self.config.github_owner)) or self.preflight().get("gh", {}).get("account", "")
        if not owner:
            raise RuntimeError("无法确定 GitHub Owner")
        suffix = {"zero_to_one": "", "feature": "-feature", "bugfix": "-bugfix"}[task["task_type"]]
        # A Pair always gets a new repository. The short Pair id keeps names
        # deterministic while avoiding collisions with imported source repos.
        prefix = str(self.db.setting("repository_prefix", self.config.repository_prefix))
        visibility = str(self.db.setting("github_visibility", self.config.github_visibility))
        author_name = str(self.db.setting("git_author_name", self.config.git_author_name))
        author_email = str(self.db.setting("git_author_email", self.config.git_author_email))
        name = slugify("%sab-%s-%s%s" % (
            prefix, pair_id[-8:], task["title"], suffix
        ))
        local_root = self.config.projects_dir / pair_id
        baseline = local_root / "baseline"
        local_root.mkdir(parents=True, exist_ok=True)
        repo_id = existing["id"] if existing else "repo-" + uuid.uuid4().hex[:16]
        stamp = now_iso()
        if not existing:
            self.db.execute(
                """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (repo_id, pair_id, owner, name, visibility, str(local_root), "creating", stamp, stamp),
            )
        try:
            if baseline.exists():
                shutil.rmtree(baseline)
            source = Path(task["baseline_path"]) if task.get("baseline_path") else None
            if source and source.exists():
                # Feature/Bug tasks must use the exact recorded task-time
                # commit, never whatever happens to be in the source working
                # tree today.
                base_sha = str(task.get("baseline_sha") or "")
                is_commit = run_command(
                    ["git", "-C", str(source), "cat-file", "-e", "%s^{commit}" % base_sha],
                    check=False, timeout=30,
                ) if base_sha else None
                if is_commit and is_commit.returncode == 0:
                    run_command(["git", "clone", "--no-hardlinks", str(source), str(baseline)], timeout=180)
                    run_command(["git", "checkout", "--detach", base_sha], cwd=baseline, timeout=60)
                    shutil.rmtree(baseline / ".git")
                    for ignored in ("node_modules", ".venv", "__pycache__"):
                        for path in baseline.glob("**/%s" % ignored):
                            if path.is_dir():
                                shutil.rmtree(path, ignore_errors=True)
                else:
                    raise RuntimeError("来源目录中找不到记录的基线提交：%s" % base_sha)
            else:
                baseline.mkdir()
                (baseline / ".gitignore").write_text(
                    ".DS_Store\n.env\n.venv/\nvenv/\nnode_modules/\n__pycache__/\n"
                    ".pytest_cache/\n.mypy_cache/\n.ruff_cache/\n.tox/\n.nox/\n.pnpm-store/\n*.py[cod]\n",
                    encoding="utf-8",
                )
            run_command(["git", "init", "-b", "main"], cwd=baseline)
            self._ensure_local_excludes(baseline)
            run_command(["git", "config", "user.name", author_name], cwd=baseline)
            run_command(["git", "config", "user.email", author_email], cwd=baseline)
            run_command(["git", "add", "-A"], cwd=baseline)
            run_command(["git", "commit", "-m", "Initialize A/B baseline"], cwd=baseline)
            main_sha = run_command(["git", "rev-parse", "HEAD"], cwd=baseline).stdout.strip()
            remote_slug = "%s/%s" % (owner, name)
            exists = run_command(["gh", "repo", "view", remote_slug, "--json", "url", "--jq", ".url"], check=False, timeout=30)
            visibility_flag = "--private" if visibility == "private" else "--public"
            canonical_remote = "https://github.com/%s.git" % remote_slug
            if exists.returncode != 0:
                run_command(["gh", "repo", "create", remote_slug, visibility_flag], cwd=baseline, timeout=120)
            elif not existing:
                raise RuntimeError("目标仓库已经存在，不能覆盖：%s" % remote_slug)
            run_command(["git", "remote", "add", "origin", canonical_remote], cwd=baseline)
            self._github_git(["push", "-u", "origin", "main:main"], cwd=baseline, timeout=180)
            remote_url = canonical_remote
            for arm in ("A", "B"):
                run_command(["git", "branch", arm, main_sha], cwd=baseline)
                self._github_git(["push", "origin", "%s:%s" % (arm, arm)], cwd=baseline, timeout=120)
                arm_dir = local_root / arm
                self._github_git(["clone", "--branch", arm, "--single-branch", remote_url, str(arm_dir)], timeout=180)
                self._ensure_local_excludes(arm_dir)
                run_command(["git", "config", "user.name", author_name], cwd=arm_dir)
                run_command(["git", "config", "user.email", author_email], cwd=arm_dir)
            self.db.execute(
                """UPDATE git_repositories SET remote_url=?,main_sha=?,a_sha=?,b_sha=?,status='ready',error='',updated_at=?
                   WHERE id=?""",
                (remote_url, main_sha, main_sha, main_sha, now_iso(), repo_id),
            )
            self.db.execute("UPDATE pairs SET repo_id=?,baseline_sha=?,updated_at=? WHERE id=?", (repo_id, main_sha, now_iso(), pair_id))
            self.db.audit("git.repository_ready", "pair", pair_id, {"repo": remote_slug, "main_sha": main_sha})
            return self.db.one("SELECT * FROM git_repositories WHERE id=?", (repo_id,)) or {}
        except Exception as exc:
            error = redact(str(exc))
            self.db.execute("UPDATE git_repositories SET status='failed',error=?,updated_at=? WHERE id=?", (error, now_iso(), repo_id))
            self.db.audit("git.repository_failed", "pair", pair_id, {"error": error})
            raise

    def push_arm(self, pair_id: str, arm: str) -> str:
        if arm not in ("A", "B"):
            raise ValueError("arm must be A or B")
        repo = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,))
        if not repo:
            raise RuntimeError("Pair 尚未创建仓库")
        arm_run = self.db.one("SELECT workspace_path FROM arm_runs WHERE pair_id=? AND arm=?", (pair_id, arm))
        if not arm_run:
            raise RuntimeError("找不到 %s Arm 工作区" % arm)
        path = Path(arm_run["workspace_path"])
        pair = self.db.one("SELECT baseline_sha FROM pairs WHERE id=?", (pair_id,)) or {}
        baseline = str(pair.get("baseline_sha") or repo.get("main_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", baseline):
            raise RuntimeError("Pair 初始环境快照无效")
        if run_command(["git", "merge-base", "--is-ancestor", baseline, "HEAD"], cwd=path, check=False).returncode != 0:
            raise RuntimeError("%s 当前代码不是从初始环境快照派生" % arm)

        self._ensure_local_excludes(path)
        run_command(["git", "add", "-A"], cwd=path)
        current_sha = run_command(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()
        current_parent = run_command(
            ["git", "rev-parse", current_sha + "^"], cwd=path, check=False,
        )
        clean = not run_command(["git", "status", "--porcelain"], cwd=path).stdout.strip()
        current_summary = self.delivery_diff_summary(path, baseline, current_sha)
        if (current_parent.returncode == 0 and current_parent.stdout.strip() == baseline and clean
                and not current_summary["generated_files"]):
            sha = current_sha
        else:
            # Claude may commit dependencies while developing. Build the final
            # snapshot from the baseline, then unstage generated dependency
            # trees while leaving them available locally for Docker checks.
            run_command(["git", "reset", "--soft", baseline], cwd=path)
            removed = self._remove_generated_dependencies_from_index(path, baseline)
            if removed:
                self.db.audit("git.generated_dependencies_excluded", "pair", pair_id, {
                    "arm": arm, "paths": removed[:20], "count": len(removed),
                })
            changed = run_command(
                ["git", "diff", "--cached", "--quiet", baseline], cwd=path, check=False,
            )
            if changed.returncode == 0:
                raise RuntimeError("%s 没有相对初始环境的代码产出，不能作为交付快照" % arm)
            if changed.returncode != 1:
                raise RuntimeError("%s 无法核对相对初始环境的代码变更" % arm)
            # Claude 可以在开发过程中产生任意数量的本地提交；正式 A/B
            # 快照统一压成一个提交，使其唯一父提交始终是 main 基线。
            run_command(["git", "commit", "-m", "Deliver %s implementation" % arm], cwd=path)
            sha = run_command(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()
        parent = run_command(["git", "rev-parse", sha + "^"], cwd=path).stdout.strip()
        if parent != baseline:
            raise RuntimeError("%s 产物快照的父提交不是初始环境快照" % arm)
        summary = self.delivery_diff_summary(path, baseline, sha)
        if summary["generated_files"]:
            raise RuntimeError("%s 产物快照仍包含生成依赖目录，已停止推送" % arm)

        remote = self._github_git(
            ["ls-remote", "origin", "refs/heads/%s" % arm], cwd=path, timeout=60,
        ).stdout.strip().split()
        old_remote = remote[0] if remote else ""
        if old_remote != sha:
            if not re.fullmatch(r"[0-9a-f]{40}", old_remote):
                raise RuntimeError("无法确认远端 %s 分支当前提交" % arm)
            lease = "--force-with-lease=refs/heads/%s:%s" % (arm, old_remote)
            self._github_git(
                ["push", lease, "origin", "HEAD:refs/heads/%s" % arm], cwd=path, timeout=180,
            )
        verified = self._github_git(
            ["ls-remote", "origin", "refs/heads/%s" % arm], cwd=path, timeout=60,
        ).stdout.strip().split()
        remote_sha = verified[0] if verified else ""
        if sha != remote_sha:
            raise RuntimeError("%s 远端 SHA 校验失败" % arm)
        column = "a_sha" if arm == "A" else "b_sha"
        self.db.execute("UPDATE git_repositories SET %s=?,updated_at=? WHERE id=?" % column, (sha, now_iso(), repo["id"]))
        return sha

    def reset_arm_to_baseline(self, pair_id: str, arm: str) -> Path:
        """Restore a delivered arm to the Pair baseline before a clean retry.

        Artifact validation happens after the first implementation has already
        been pushed.  A retry must therefore rewind both the canonical checkout
        and its remote A/B branch; otherwise a fresh implementation is either
        based on the invalid delivery or rejected as a non-fast-forward push.
        """
        if arm not in ("A", "B"):
            raise ValueError("arm must be A or B")
        pair = self.db.one("SELECT baseline_sha FROM pairs WHERE id=?", (pair_id,)) or {}
        repo = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,)) or {}
        baseline = str(pair.get("baseline_sha") or repo.get("main_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", baseline):
            raise RuntimeError("Pair 基线提交无效，无法重跑 %s" % arm)
        path = Path(str(repo.get("local_root") or "")) / arm
        if not (path / ".git").is_dir():
            raise RuntimeError("找不到 %s 的规范仓库目录" % arm)

        remote = self._github_git(
            ["ls-remote", "origin", "refs/heads/%s" % arm], cwd=path, timeout=60,
        ).stdout.strip().split()
        remote_sha = remote[0] if remote else ""
        run_command(["git", "checkout", "-f", arm], cwd=path, timeout=60)
        run_command(["git", "reset", "--hard", baseline], cwd=path, timeout=60)
        run_command(["git", "clean", "-fd"], cwd=path, timeout=60)
        if remote_sha != baseline:
            if not re.fullmatch(r"[0-9a-f]{40}", remote_sha):
                raise RuntimeError("无法确认远端 %s 分支当前提交" % arm)
            lease = "--force-with-lease=refs/heads/%s:%s" % (arm, remote_sha)
            self._github_git(
                ["push", lease, "origin", "%s:refs/heads/%s" % (baseline, arm)],
                cwd=path, timeout=180,
            )
        verified = self._github_git(
            ["ls-remote", "origin", "refs/heads/%s" % arm], cwd=path, timeout=60,
        ).stdout.strip().split()
        if not verified or verified[0] != baseline:
            raise RuntimeError("%s 分支未能恢复到共同基线" % arm)
        column = "a_sha" if arm == "A" else "b_sha"
        self.db.execute(
            "UPDATE git_repositories SET %s=?,updated_at=? WHERE id=?" % column,
            (baseline, now_iso(), repo["id"]),
        )
        self.db.audit("git.arm_reset_to_baseline", "pair", pair_id, {
            "arm": arm, "baseline_sha": baseline, "replaced_sha": remote_sha,
        })
        return path

    def prepare_arm_commit(self, pair_id: str, arm: str, commit_sha: str) -> Path:
        """Prepare the canonical checkout from the exact delivered commit."""
        if arm not in ("A", "B"):
            raise ValueError("arm must be A or B")
        if not re.fullmatch(r"[0-9a-f]{40}", str(commit_sha or "")):
            raise RuntimeError("%s 已交付提交无效，无法返工" % arm)
        repo = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,)) or {}
        path = Path(str(repo.get("local_root") or "")) / arm
        if not (path / ".git").is_dir():
            raise RuntimeError("找不到 %s 的规范仓库目录" % arm)
        remote = self._github_git(
            ["ls-remote", "origin", "refs/heads/%s" % arm], cwd=path, timeout=60,
        ).stdout.strip().split()
        remote_sha = remote[0] if remote else ""
        if remote_sha != commit_sha:
            raise RuntimeError("远端 %s 分支与待返工提交不一致" % arm)
        self._github_git(
            ["fetch", "origin", "refs/heads/%s" % arm], cwd=path, timeout=120,
        )
        run_command(["git", "checkout", "-f", arm], cwd=path, timeout=60)
        run_command(["git", "reset", "--hard", commit_sha], cwd=path, timeout=60)
        run_command(["git", "clean", "-fd"], cwd=path, timeout=60)
        verified_sha = run_command(["git", "rev-parse", "HEAD"], cwd=path, timeout=30).stdout.strip()
        verified_branch = run_command(["git", "branch", "--show-current"], cwd=path, timeout=30).stdout.strip()
        verified_status = run_command(["git", "status", "--porcelain"], cwd=path, timeout=30).stdout.strip()
        if verified_sha != commit_sha or verified_branch != arm or verified_status:
            raise RuntimeError("%s 规范仓库未能准备为待返工提交" % arm)
        column = "a_sha" if arm == "A" else "b_sha"
        self.db.execute(
            "UPDATE git_repositories SET %s=?,updated_at=? WHERE id=?" % column,
            (commit_sha, now_iso(), repo["id"]),
        )
        self.db.audit("git.arm_prepared_from_commit", "pair", pair_id, {
            "arm": arm, "commit_sha": commit_sha,
        })
        return path
