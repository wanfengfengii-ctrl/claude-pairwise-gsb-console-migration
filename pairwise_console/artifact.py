import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import tarfile
import time
import uuid
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Dict, List, Tuple

from .commands import CommandResult, redact, run_command
from .db import Database, now_iso
from .resources import DOCKER_WORK
from .bug_verification import wait_for_services


def isolated_compose_environment(compose: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return a Compose environment with every declared host port isolated.

    Generated projects have historically used both API_PORT and WEB_PORT.  A
    validator that sets only one of them appears isolated in its audit output
    while Compose still binds the other's default (usually 8080).  Discover
    PORT variables from the actual Compose file and give each one a separate
    free host port; keep the conventional aliases for older projects.
    """
    env = os.environ.copy()
    text = compose.read_text(encoding="utf-8", errors="replace")
    variables = {
        name for name in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", text)
        if "PORT" in name.upper()
    }
    variables.update(("API_PORT", "WEB_PORT", "APP_PORT", "HOST_PORT", "HTTP_PORT"))
    assigned: Dict[str, str] = {}
    for name in sorted(variables):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            assigned[name] = str(int(probe.getsockname()[1]))
        env[name] = assigned[name]
    return env, assigned


class ArtifactChecker:
    def __init__(self, db: Database):
        self.db = db

    def preflight(self, workspace: Path, project_key: str,
                  allow_system_adapter: bool = False) -> Dict[str, Any]:
        """Validate an existing task baseline without creating delivery evidence."""
        return self._probe(workspace, "baseline-%s" % project_key[-16:].lower(),
                           allow_system_adapter=allow_system_adapter,
                           require_self_contained=allow_system_adapter)

    def validate(self, pair_id: str, arm: str, workspace: Path, commit_sha: str) -> Dict[str, Any]:
        check_id = "check-" + uuid.uuid4().hex[:16]
        stamp = now_iso()
        with self.db.transaction() as conn:
            previous = conn.execute(
                "SELECT * FROM artifact_checks WHERE pair_id=? AND arm=? AND commit_sha=?",
                (pair_id, arm, commit_sha),
            ).fetchone()
            if previous:
                # The unique key requires replacing the current check.  Keep
                # the complete previous evidence before that replacement.
                conn.execute(
                    """INSERT INTO audit_events(event_type,entity_type,entity_id,detail_json,created_at)
                       VALUES(?,?,?,?,?)""",
                    ("artifact.recheck_previous_evidence", "pair", pair_id,
                     json.dumps(dict(previous), ensure_ascii=False), stamp),
                )
            conn.execute(
                """INSERT OR REPLACE INTO artifact_checks(id,pair_id,arm,commit_sha,status,started_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (check_id, pair_id, arm, commit_sha, "running", stamp, stamp, stamp),
            )
        task = self.db.one(
            """SELECT t.created_at,t.prompt FROM pairs p JOIN tasks t ON t.id=p.task_id
               WHERE p.id=?""", (pair_id,),
        ) or {}
        cutoff = str(self.db.setting("dockerless_task_policy_started_at", "") or "")
        system_adapter = bool(cutoff and str(task.get("created_at") or "") >= cutoff
                              and not re.search(r"\b(?:docker|compose|dockerfile)\b", str(task.get("prompt") or ""), re.I))
        result = self._probe(
            workspace, "paircheck-%s-%s" % (pair_id[-8:].lower(), arm.lower()),
            commit_sha=commit_sha, allow_system_adapter=system_adapter,
            require_self_contained=system_adapter,
        )
        try:
            self.db.execute(
                """UPDATE artifact_checks SET compose_file=?,status=?,checks_json=?,error=?,
                   finished_at=?,updated_at=? WHERE id=?""",
                (result["compose_file"], result["status"],
                 json.dumps(result["checks"], ensure_ascii=False), result["error"],
                 now_iso(), now_iso(), check_id),
            )
            return self.db.one("SELECT * FROM artifact_checks WHERE id=?", (check_id,)) or {}
        except Exception:
            self.db.execute(
                """UPDATE artifact_checks SET compose_file=?,status='failed',checks_json=?,error=?,finished_at=?,updated_at=? WHERE id=?""",
                (result["compose_file"], json.dumps(result["checks"], ensure_ascii=False),
                 result["error"], now_iso(), now_iso(), check_id),
            )
            return self.db.one("SELECT * FROM artifact_checks WHERE id=?", (check_id,)) or {}

    def _probe(self, workspace: Path, project: str, commit_sha: str = "",
               allow_system_adapter: bool = False,
               require_self_contained: bool = False) -> Dict[str, Any]:
        with DOCKER_WORK:
            if require_self_contained:
                dependency = self._external_dependency_issue(workspace)
                if dependency:
                    return {"status": "failed", "compose_file": "", "error": dependency,
                            "checks": [{"name": "external_dependency", "passed": False,
                                        "detail": dependency}]}
            if allow_system_adapter:
                try:
                    adapted = self._system_adapter(workspace, commit_sha)
                except Exception as exc:
                    detail = redact(str(exc))
                    return {"status": "failed", "compose_file": "", "error": detail,
                            "checks": [{"name": "system_adapter", "passed": False,
                                        "detail": detail}]}
                if require_self_contained:
                    dependency = self._external_dependency_issue(adapted)
                    if dependency:
                        return {"status": "failed", "compose_file": str(adapted / "compose.yaml"),
                                "error": dependency,
                                "checks": [{"name": "external_dependency", "passed": False,
                                            "detail": dependency}]}
                result = self._probe_with_capacity(adapted, project)
                result["checks"].insert(0, {"name": "system_adapter", "passed": True,
                                            "detail": "系统在原仓库外生成验收环境；交付提交未被修改"})
                if require_self_contained:
                    result["checks"].insert(0, {"name": "self_contained_source", "passed": True,
                                                "detail": "未发现运行时外部服务或远端 API 依赖"})
                return result
            result = self._probe_with_capacity(workspace, project)
            if require_self_contained:
                result["checks"].insert(0, {"name": "self_contained_source", "passed": True,
                                            "detail": "未发现运行时外部服务或远端 API 依赖"})
            return result

    @staticmethod
    def _external_dependency_issue(workspace: Path) -> str:
        """Fail closed on common runtime services and remote API endpoints.

        This is a guard, not a proof by itself: the isolated runtime check
        remains necessary before a delivery can be considered reproducible.
        """
        manifests = ("requirements.txt", "pyproject.toml", "package.json", "go.mod",
                     "compose.yaml", "compose.yml", "docker-compose.yml")
        service_names = re.compile(
            r"\b(?:redis|postgres(?:ql)?|mysql|mariadb|mongodb|kafka|rabbitmq|"
            r"boto3|aws-sdk|stripe|twilio|firebase|supabase)\b", re.I,
        )
        for name in manifests:
            path = workspace / name
            if path.is_file() and service_names.search(path.read_text(encoding="utf-8", errors="replace")):
                return "交付清单依赖独立服务或第三方 API，不能选择无外部依赖验收：" + name
        endpoint = re.compile(r"https?://(?!localhost\b|127\.0\.0\.1\b|0\.0\.0\.0\b|\[::1\])([A-Za-z0-9.-]+)", re.I)
        for extension in ("*.py", "*.js", "*.jsx", "*.ts", "*.tsx", "*.go"):
            for path in workspace.rglob(extension):
                if any(part in {"node_modules", ".venv", "venv", "tests", "test", "dist", "build"}
                       for part in path.relative_to(workspace).parts):
                    continue
                if not path.is_file():
                    continue
                if endpoint.search(path.read_text(encoding="utf-8", errors="replace")):
                    return "业务源码含远端 HTTP 地址，不能证明无外部依赖：" + str(path.relative_to(workspace))
        return ""

    def _system_adapter(self, workspace: Path, commit_sha: str) -> Path:
        """Materialize only committed files in an external, system-owned harness."""
        sha = commit_sha or run_command(["git", "rev-parse", "HEAD"], cwd=workspace, timeout=20).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise RuntimeError("没有可核对的固定提交，无法独立验收")
        folder = self.db.path.parent / "artifact-adapters" / (sha[:12] + "-" + uuid.uuid4().hex[:8])
        folder.mkdir(parents=True, exist_ok=False)
        archive = folder / "source.tar"
        exported = run_command(
            ["git", "archive", "--format=tar", "--output", str(archive), sha],
            cwd=workspace, check=False, timeout=120,
        )
        if exported.returncode != 0:
            raise RuntimeError("固定提交导出失败：" + redact(exported.stderr or exported.stdout))
        with tarfile.open(archive) as bundle:
            for member in bundle:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                    raise RuntimeError("提交包含无法安全复制的路径或符号链接")
                destination = folder.joinpath(*path.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    stream = bundle.extractfile(member)
                    if stream is None:
                        raise RuntimeError("提交快照文件读取失败")
                    with destination.open("wb") as output:
                        shutil.copyfileobj(stream, output)
                    destination.chmod(0o755 if member.mode & 0o111 else 0o644)
        archive.unlink(missing_ok=True)
        dependency = self._external_dependency_issue(folder)
        if dependency:
            raise RuntimeError(dependency)
        dockerfile, start, verify = self._adapter_recipe(folder)
        image = str(self.db.setting("claude_image", "") or "")
        if not re.fullmatch(r"[A-Za-z0-9./:_-]+", image):
            raise RuntimeError("系统验收镜像名称不安全或未配置")
        (folder / "Dockerfile").write_text("FROM " + image + "\nENTRYPOINT []\nUSER root\nWORKDIR /app\nCOPY . .\n"
                                           + dockerfile + "\nUSER node\n", encoding="utf-8")
        gateway = (
            "const http=require('http');"
            "http.createServer((request,response)=>{"
            "const upstream=http.request({hostname:'app',port:8080,path:request.url,"
            "method:request.method,headers:request.headers},result=>{"
            "response.writeHead(result.statusCode,result.headers);result.pipe(response)});"
            "upstream.on('error',()=>{response.writeHead(502);response.end()});"
            "request.pipe(upstream)}).listen(8080,'0.0.0.0')"
        )
        (folder / "compose.yaml").write_text(json.dumps({
            "networks": {"private": {"internal": True}, "edge": {}},
            "services": {
                "app": {
                    "build": ".", "command": ["sh", "-lc", start],
                    "environment": {"PORT": "8080", "HOST": "0.0.0.0"},
                    "networks": ["private"],
                    "healthcheck": {"test": ["CMD-SHELL", "node -e \"fetch('http://127.0.0.1:8080/').then(r=>process.exit(r.status<500?0:1)).catch(()=>process.exit(1))\""],
                                    "interval": "5s", "timeout": "4s", "retries": 24},
                },
                "gateway": {
                    "image": image, "entrypoint": ["node"], "command": ["-e", gateway],
                    "ports": ["${APP_PORT:-8080}:8080"], "networks": ["private", "edge"],
                    "depends_on": {"app": {"condition": "service_healthy"}},
                },
                "verify": {"build": ".", "command": ["sh", "-lc", "timeout 600 " + verify],
                           "networks": ["private"], "profiles": ["verification"]},
            },
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return folder

    @staticmethod
    def _adapter_recipe(folder: Path) -> Tuple[str, str, str]:
        """Recognize a self-contained single-service project, otherwise fail closed."""
        package = folder / "package.json"
        requirements = folder / "requirements.txt"
        pyproject = folder / "pyproject.toml"
        go_mod = folder / "go.mod"
        kinds = [package.is_file(), requirements.is_file() or pyproject.is_file(), go_mod.is_file()]
        if sum(kinds) != 1:
            raise RuntimeError("无法唯一识别项目运行入口；不能把未验证项目判为通过")
        forbidden = re.compile(r"\b(?:redis|postgres(?:ql)?|mysql|mariadb|mongodb|kafka|rabbitmq|boto3|aws-sdk|stripe|twilio)\b", re.I)
        manifests = [path for path in (package, requirements, pyproject, go_mod) if path.is_file()]
        if any(forbidden.search(path.read_text(encoding="utf-8", errors="replace")) for path in manifests):
            raise RuntimeError("代码清单包含独立服务或第三方 API 依赖，不能标为无外部依赖")
        if package.is_file():
            data = json.loads(package.read_text(encoding="utf-8"))
            scripts = data.get("scripts") or {}
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
            if "start" in scripts:
                start = "npm start"
            elif "dev" in scripts and "vite" in deps:
                start = "npm run dev -- --host 0.0.0.0 --port 8080"
            elif "dev" in scripts and "next" in deps:
                start = "npm run dev -- --hostname 0.0.0.0 --port 8080"
            else:
                raise RuntimeError("Node 项目缺少可识别的启动脚本")
            if "verify" in scripts:
                verify = "npm run verify"
            elif "test" in scripts:
                verify = "npm test -- --run" if "vitest" in deps else "npm test"
            else:
                raise RuntimeError("Node 项目缺少可重跑的项目测试")
            install = "npm ci" if (folder / "package-lock.json").is_file() else "npm install --no-audit --no-fund"
            build = " && npm run build" if "build" in scripts else ""
            return "RUN " + install + build + " && chown -R node:node /app", start, verify
        if requirements.is_file() or pyproject.is_file():
            start = ""
            for relative in ("main.py", "app.py", "app/main.py", "src/main.py"):
                path = folder / relative
                if not path.is_file():
                    continue
                body = path.read_text(encoding="utf-8", errors="replace")
                module = relative[:-3].replace("/", ".")
                if re.search(r"\bFastAPI\s*\(", body):
                    start = "uvicorn %s:app --host 0.0.0.0 --port 8080" % module
                    break
                if re.search(r"\bFlask\s*\(", body):
                    start = "flask --app %s:app run --host 0.0.0.0 --port 8080" % module
                    break
            if not start and (folder / "manage.py").is_file():
                start = "python3 manage.py runserver 0.0.0.0:8080"
            if not start or not ((folder / "tests").is_dir() or list(folder.glob("test_*.py"))):
                raise RuntimeError("Python 项目缺少可识别的 Web 入口或项目测试")
            install = ("/opt/venv/bin/pip install -r requirements.txt pytest uvicorn flask" if requirements.is_file()
                       else "/opt/venv/bin/pip install -e . pytest uvicorn flask")
            return "RUN python3 -m venv /opt/venv && " + install + " && chown -R node:node /app\nENV PATH=/opt/venv/bin:$PATH", start, "python3 -m pytest -q"
        if not list(folder.glob("**/*_test.go")):
            raise RuntimeError("Go 项目缺少可重跑的项目测试")
        return "RUN go mod download && chown -R node:node /app", "go run .", "go test ./..."

    def _probe_with_capacity(self, workspace: Path, project: str) -> Dict[str, Any]:
        checks: List[Dict[str, Any]] = []
        compose = self._compose_path(workspace)
        compose_env: Dict[str, str] = {}
        try:
            self._record(checks, "compose_file", bool(compose), str(compose or "未找到 Compose 文件"))
            dockerfile = next(iter(workspace.glob("**/Dockerfile")), None)
            self._record(checks, "dockerfile", bool(dockerfile), str(dockerfile or "未找到 Dockerfile"))
            if not compose or not dockerfile:
                raise RuntimeError("缺少 Docker Compose 或 Dockerfile")
            compose_env, assigned_ports = isolated_compose_environment(compose)
            self._record(
                checks, "isolated_host_port", True,
                json.dumps(assigned_ports, ensure_ascii=False, sort_keys=True),
            )
            config = run_command(
                ["docker", "compose", "-f", str(compose), "--profile", "*", "config"],
                cwd=workspace, check=False, timeout=60, env=compose_env,
            )
            self._record(
                checks, "compose_config", config.returncode == 0,
                redact(config.stderr or config.stdout),
                "docker compose -f %s --profile '*' config" % compose.name,
                config.returncode,
            )
            if config.returncode != 0:
                raise RuntimeError("Compose 配置无效")
            base = ["docker", "compose", "-p", project, "-f", str(compose)]
            services = run_command(
                base + ["--profile", "*", "config", "--services"],
                cwd=workspace, check=False, timeout=60, env=compose_env,
            )
            service_names = {line.strip() for line in services.stdout.splitlines() if line.strip()}
            verifier_names = {
                name for name in service_names
                if name.casefold() in {"verify", "acceptance", "smoke", "test", "tests"}
            }
            verification_service = next(
                (name for preferred in ("verify", "acceptance", "smoke", "test", "tests")
                 for name in sorted(verifier_names) if name.casefold() == preferred),
                "",
            )
            documented_verifier = (
                None if verification_service
                else self._documented_verifier(workspace, assigned_ports)
            )
            has_verify = bool(verification_service or documented_verifier)
            verify_detail = (
                "Compose service: %s" % verification_service
                if verification_service
                else " ".join(documented_verifier or [])
            )
            application_services = sorted(service_names - verifier_names)
            self._record(
                checks, "application_service_present", bool(application_services),
                ", ".join(application_services) or "Compose 中没有可运行的应用服务",
            )
            self._record(
                checks, "verify_service_present", has_verify,
                verify_detail or ", ".join(sorted(service_names)),
            )
            if not application_services:
                raise RuntimeError("Compose 缺少可运行的应用服务")
            if verification_service:
                # Dependency downloads and image construction are environment
                # preparation, not the acceptance result.  Build the verifier
                # once under the same isolated Compose project name, then run
                # the project's original one-shot entry without rebuilding or
                # recreating its already-running application dependencies.
                prepared = run_command(
                    base + ["--profile", "*", "build", verification_service],
                    cwd=workspace, check=False, timeout=None, env=compose_env,
                )
                self._record(
                    checks, "verify_image_prepared", prepared.returncode == 0,
                    redact(prepared.stderr or prepared.stdout),
                    "docker compose -p %s -f %s --profile '*' build %s"
                    % (project, compose.name, verification_service),
                    prepared.returncode,
                )
                if prepared.returncode != 0:
                    raise RuntimeError("verify 镜像准备失败")
            run_command(base + ["--profile", "*", "down", "-v", "--remove-orphans"], cwd=workspace, check=False, timeout=180, env=compose_env)
            # Start every application service exactly once, but never start a
            # one-shot verifier through `compose up`.  It is run once below so
            # destructive or non-idempotent acceptance scenarios are not
            # accidentally executed twice.
            up_command = base + ["up", "-d", "--build"] + application_services
            up = run_command(
                up_command, cwd=workspace, check=False,
                timeout=None, env=compose_env,
            )
            self._record(
                checks, "clean_start", up.returncode == 0, redact(up.stderr or up.stdout),
                "docker compose -p %s -f %s up -d --build %s"
                % (project, compose.name, " ".join(application_services)),
                up.returncode,
            )
            if up.returncode != 0:
                raise RuntimeError("Docker Compose 清洁启动失败")
            wait_for_services(base, workspace, compose_env, application_services, runner=run_command)
            ps = run_command(base + ["ps", "--format", "json"], cwd=workspace, check=False, timeout=60, env=compose_env)
            running = ps.returncode == 0 and ("running" in ps.stdout.casefold() or "healthy" in ps.stdout.casefold())
            self._record(
                checks, "containers_running", running, redact(ps.stdout or ps.stderr),
                "docker compose -p %s -f %s ps --format json" % (project, compose.name),
                ps.returncode,
            )
            if verification_service:
                verify = self._run_project_verifier(
                    # Application dependencies are already running and have
                    # passed their health checks above.  Letting `compose run`
                    # reconcile them again can recreate a shared-image proxy
                    # and race its still-bound host port, producing a false
                    # baseline failure before the verifier even starts.
                    base + ["--profile", "*", "run", "--rm", "--no-deps", verification_service],
                    workspace, compose_env,
                )
                self._record(
                    checks, "verify_service", verify.returncode == 0,
                    self._verifier_detail(verify),
                    "docker compose -p %s -f %s --profile '*' run --rm --no-deps %s"
                    % (project, compose.name, verification_service),
                    verify.returncode,
                )
            elif documented_verifier:
                verify = self._run_project_verifier(documented_verifier, workspace, compose_env)
                self._record(
                    checks, "verify_service", verify.returncode == 0,
                    self._verifier_detail(verify),
                    " ".join(documented_verifier), verify.returncode,
                )
            down = run_command(base + ["down", "-v", "--remove-orphans"], cwd=workspace, check=False, timeout=180, env=compose_env)
            self._record(
                checks, "cleanup", down.returncode == 0, redact(down.stderr or down.stdout),
                "docker compose -p %s -f %s down -v --remove-orphans" % (project, compose.name),
                down.returncode,
            )
            status = "passed" if all(item["passed"] for item in checks) else "failed"
            return {"status": status, "compose_file": str(compose), "checks": checks,
                    "error": "" if status == "passed" else "Docker 基线验收未全部通过"}
        except Exception as exc:
            if compose and compose_env:
                run_command(
                    ["docker", "compose", "-p", project, "-f", str(compose), "--profile", "*", "down", "-v", "--remove-orphans"],
                    cwd=workspace, check=False, timeout=180, env=compose_env,
                )
            return {"status": "failed", "compose_file": str(compose or ""),
                    "checks": checks, "error": redact(str(exc))}

    @staticmethod
    def _compose_path(workspace: Path):
        for name in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
            path = workspace / name
            if path.exists():
                return path
        return None

    @staticmethod
    def _documented_verifier(workspace: Path, assigned_ports: Dict[str, str]):
        """Return a safe project-owned verifier command documented by README."""
        readme = workspace / "README.md"
        try:
            text = readme.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        host_port = (
            assigned_ports.get("API_PORT") or assigned_ports.get("HOST_PORT")
            or assigned_ports.get("HTTP_PORT") or assigned_ports.get("APP_PORT")
            or next(iter(assigned_ports.values()), "")
        )
        for raw_line in text.splitlines():
            line = raw_line.strip().lstrip("$ ")
            if not re.match(r"^python(?:3(?:\.\d+)?)?\s+", line, re.IGNORECASE):
                continue
            try:
                command = shlex.split(line)
            except ValueError:
                continue
            if len(command) < 2 or not re.search(
                r"(?:^|/)(?:verify|acceptance|smoke)[^/]*\.py$", command[1], re.IGNORECASE
            ):
                continue
            script = (workspace / command[1]).resolve()
            root = workspace.resolve()
            if not script.is_file() or (script != root and root not in script.parents):
                continue
            if host_port:
                command = [
                    re.sub(
                        r"(https?://(?:localhost|127\.0\.0\.1))(?::\d+)?",
                        r"\1:%s" % host_port,
                        item,
                        flags=re.IGNORECASE,
                    )
                    for item in command
                ]
            return command
        return None

    @staticmethod
    def _verifier_detail(result: CommandResult) -> str:
        """Keep the verifier's final verdict alongside test-runner stderr."""
        stderr = redact(result.stderr or "")[-350:]
        stdout = redact(result.stdout or "")[-800:]
        return ("stderr:\n" + stderr + "\n" if stderr else "") + "stdout:\n" + stdout

    @staticmethod
    def _run_project_verifier(command: List[str], workspace: Path,
                              environment: Dict[str, str]) -> CommandResult:
        """Run the project-owned acceptance entry until it exits."""
        return run_command(
            command, cwd=workspace, check=False, timeout=None, env=environment,
        )

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    @staticmethod
    def _record(checks: List[Dict[str, Any]], name: str, passed: bool, detail: str,
                command: str = "", exit_code: Any = None) -> None:
        item = {"name": name, "passed": bool(passed), "detail": detail[-1200:]}
        if command:
            item["command"] = command
        if exit_code is not None:
            item["exit_code"] = int(exit_code)
        checks.append(item)


def validate_recording(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"ok": False, "error": "录像文件不存在"}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    probe = run_command([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path),
    ], check=False, timeout=60)
    if probe.returncode != 0:
        return {"ok": False, "sha256": digest, "error": redact(probe.stderr)}
    data = json.loads(probe.stdout)
    stream = (data.get("streams") or [{}])[0]
    duration = float((data.get("format") or {}).get("duration") or 0)
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    return {
        "ok": width == 1280 and height == 720 and 0 < duration < 90,
        "sha256": digest, "width": width, "height": height, "duration_seconds": duration,
        "error": "" if width == 1280 and height == 720 and 0 < duration < 90 else "录像必须为 1280×720 且少于 90 秒",
    }
