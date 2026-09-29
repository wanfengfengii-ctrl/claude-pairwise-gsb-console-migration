"""Private, SHA-bound non-browser Bug verification in disposable checkouts."""
import json
import hashlib
import os
from pathlib import Path
import re
import tempfile
import time

from .commands import redact, run_command
from .resources import DOCKER_WORK
from .db import now_iso


def validate_specs(specs, repair=False):
    if not isinstance(specs, list) or not specs:
        raise ValueError("缺少仓库外业务验证命令")
    for spec in specs:
        args = spec.get("composeArgs") if isinstance(spec, dict) else None
        if not isinstance(args, list) or len(args) < 3 or args[0] not in ("exec", "run"):
            raise ValueError("业务验证只允许 Compose exec/run")
        if not all(isinstance(value, str) and value for value in args):
            raise ValueError("业务验证参数无效")
        if any(value in ("-v", "--volume", "--privileged", "--entrypoint", "--service-ports")
               or value.startswith(("--volume=", "--entrypoint=")) for value in args):
            raise ValueError("业务验证不得扩展容器宿主权限")
        if repair and (not spec.get("expectedOutputContains") or not spec.get("failureOutputContains")
                       or spec["expectedOutputContains"] == spec["failureOutputContains"]):
            raise ValueError("修复验证必须提供不同的正确结果与业务失败标记")


def service_rows(stdout):
    try:
        value = json.loads(stdout)
        return value if isinstance(value, list) else [value]
    except ValueError:
        return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def wait_for_services(base, workspace, env, names, timeout=180, runner=None):
    runner = runner or run_command
    deadline = time.monotonic() + timeout
    while True:
        result = runner(base + ["ps", "--format", "json"], cwd=workspace,
                             env=env, check=False, timeout=30)
        try:
            rows = service_rows(result.stdout) if result.returncode == 0 else []
        except (ValueError, TypeError):
            rows = []
        by_name = {row.get("Service"): row for row in rows if isinstance(row, dict)}
        if names and all(
            name in by_name and str(by_name[name].get("State", "")).lower() == "running"
            and str(by_name[name].get("Health") or "").lower() in ("", "healthy")
            for name in names
        ):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("环境未就绪：应用服务健康检查未全部完成")
        time.sleep(1)


def private_container_name(project, service):
    identity = json.dumps([project, service], ensure_ascii=False)
    return "pairwise-bug-" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def check_isolation(config, workspace, private_names=None):
    root = workspace.resolve()
    private_names = private_names or {}
    for name, volume in (config.get("volumes") or {}).items():
        if volume.get("external") or volume.get("driver_opts") or volume.get("name"):
            raise ValueError("清洁复现不能使用外部或固定名称卷：" + name)
    for name, network in (config.get("networks") or {}).items():
        if network.get("external"):
            raise ValueError("清洁复现不能共享外部网络：" + name)
    for name, service in (config.get("services") or {}).items():
        if (service.get("privileged") or service.get("devices")
                or service.get("network_mode") == "host"
                or (service.get("container_name")
                    and service["container_name"] != private_names.get(name))):
            raise ValueError("清洁复现服务存在共享名称或宿主权限：" + name)
        build = service.get("build") or {}
        context = build.get("context") if isinstance(build, dict) else build
        if context:
            path = Path(context).resolve()
            if path != root and root not in path.parents:
                raise ValueError("清洁复现构建上下文超出快照：" + name)
        for mount in service.get("volumes") or []:
            if mount.get("type") == "bind":
                path = Path(mount.get("source") or "").resolve()
                if path != root and root not in path.parents:
                    raise ValueError("清洁复现存在快照外的宿主挂载：" + name)


def clean_commands(source, sha, project, specs, repair=False, db=None):
    with DOCKER_WORK:
        return _clean_commands(source, sha, project, specs, repair, db)


def private_image_tag(project, name, tree, build, platform=""):
    identity = json.dumps([project, name, tree, build, platform], ensure_ascii=False)
    return "pairwise-private-check:" + hashlib.sha256(identity.encode()).hexdigest()[:20]


def _clean_commands(source, sha, project, specs, repair=False, db=None):
    from .artifact import ArtifactChecker, isolated_compose_environment
    validate_specs(specs, repair)
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("缺少准确产物 SHA")
    with tempfile.TemporaryDirectory(prefix="pairwise-bug-check-") as folder:
        workspace = Path(folder) / "snapshot"
        run_command(["git", "clone", "--no-hardlinks", "--no-checkout", str(source), str(workspace)], timeout=180)
        run_command(["git", "checkout", "--detach", sha], cwd=workspace, timeout=60)
        tree = run_command(["git", "rev-parse", "HEAD^{tree}"], cwd=workspace, timeout=30).stdout.strip()
        compose = ArtifactChecker._compose_path(workspace)
        if not compose:
            raise ValueError("准确提交缺少 Compose")
        env, ports = isolated_compose_environment(compose)
        base = ["docker", "compose", "-p", project, "-f", str(compose)]
        config = json.loads(run_command(base + ["config", "--format", "json"], cwd=workspace, env=env, timeout=60).stdout)
        private_names = {
            name: private_container_name(project, name)
            for name, service in (config.get("services") or {}).items()
            if service.get("container_name")
        }
        if private_names:
            override = Path(folder) / "private-containers.json"
            override.write_text(json.dumps({"services": {
                name: {"container_name": private_name}
                for name, private_name in private_names.items()
            }}), encoding="utf-8")
            base += ["-f", str(override)]
            config = json.loads(run_command(base + ["config", "--format", "json"],
                                            cwd=workspace, env=env, timeout=60).stdout)
        # Compose synthesizes project-scoped names; these are safe. Explicit
        # external/custom names remain forbidden and cannot share run state.
        for key, value in (config.get("volumes") or {}).items():
            if value.get("name") == project + "_" + key:
                value.pop("name", None)
        check_isolation(config, workspace, private_names)
        overrides = {}
        for name, service in config["services"].items():
            if service.get("build"):
                build = json.dumps(service["build"], sort_keys=True).replace(str(workspace), "<snapshot>")
                # Compose can build services with identical contexts concurrently.
                # A shared tag makes the second exporter fail with "image already exists".
                # Scope the tag to this isolated run and service; Docker's layer cache
                # still handles reuse across clean reproductions.
                overrides[name] = {"image": private_image_tag(
                    project, name, tree, build, str(service.get("platform") or ""),
                )}
        if overrides:
            override = Path(folder) / "images.json"
            override.write_text(json.dumps({"services": overrides}), encoding="utf-8")
            base += ["-f", str(override)]
        names = [name for name in config["services"] if name.lower() not in ("verify", "test", "tests", "acceptance", "smoke")]
        if not names:
            raise ValueError("清洁复现缺少应用服务")
        results = []
        if db:
            db.execute("INSERT OR REPLACE INTO runtime_resources VALUES(?,?,?,?,?,?)",
                       (project, os.getpid(), str(workspace), str(compose), "active", now_iso()))
        try:
            run_command(base + ["up", "-d", "--build"] + names, cwd=workspace, env=env, timeout=None)
            wait_for_services(base, workspace, env, names)
            for spec in specs:
                args = list(spec["composeArgs"])
                if args[0] == "run":
                    args[1:1] = ["--rm", "--no-deps"]
                result = run_command(base + args, cwd=workspace, env=env, timeout=600, check=False)
                output = result.stdout + "\n" + result.stderr
                matched = result.returncode == int(spec.get("expectedExitCode", 0)) and str(spec.get("expectedOutputContains") or "") in output
                failed = bool(repair and spec.get("failureOutputContains") in output)
                # Contradictory output cannot count as a successful fix.
                if repair and matched and failed:
                    matched = failed = False
                results.append({"scenario": spec.get("scenario", "original"), "matched": matched,
                                "businessFailed": failed, "exitCode": result.returncode,
                                "output": redact(output)[-6000:]})
            return {"commitSha": sha, "treeSha": tree, "assignedPorts": ports,
                    "commands": results, "passed": all(row["matched"] for row in results)}
        finally:
            cleaned = False
            try:
                cleanup = run_command(base + ["--profile", "*", "down", "-v", "--remove-orphans"],
                                      cwd=workspace, env=env, timeout=180, check=False)
                cleaned = cleanup.returncode == 0
            finally:
                if db:
                    db.execute("UPDATE runtime_resources SET status=?,updated_at=? WHERE project=?",
                               ("cleaned" if cleaned else "needs_review", now_iso(), project))
