import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from pairwise_console.artifact import ArtifactChecker, isolated_compose_environment


def committed_project(root: Path, files: dict) -> str:
    root.mkdir()
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=root, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          text=True, capture_output=True).stdout.strip()


def checker(tmp_path: Path) -> ArtifactChecker:
    db = SimpleNamespace(path=tmp_path / "state" / "pairwise.db",
                         setting=lambda key, fallback=None: "claude-eval-runtime:claude-2.1.269-tools")
    return ArtifactChecker(db)


def test_system_adapter_is_outside_fixed_commit_and_has_replayable_checks(tmp_path):
    source = tmp_path / "source"
    sha = committed_project(source, {
        "package.json": json.dumps({"scripts": {"dev": "vite --host", "test": "vitest run"},
                                    "devDependencies": {"vite": "8.0.0", "vitest": "5.0.0"}}),
        "src/main.ts": "export const result = 1;\n",
    })
    adapter = checker(tmp_path)._system_adapter(source, sha)
    assert adapter != source
    assert (adapter / "Dockerfile").exists()
    assert not (source / "Dockerfile").exists()
    compose = json.loads((adapter / "compose.yaml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"app", "gateway", "verify"}
    assert compose["services"]["app"]["networks"] == ["private"]
    assert compose["networks"]["private"]["internal"] is True
    assert compose["services"]["verify"]["profiles"] == ["verification"]
    assert "npm test" in compose["services"]["verify"]["command"][-1]
    assert subprocess.run(["git", "status", "--porcelain"], cwd=source, check=True,
                          text=True, capture_output=True).stdout == ""


def test_adapter_rejects_missing_project_tests(tmp_path):
    source = tmp_path / "source"
    sha = committed_project(source, {
        "package.json": json.dumps({"scripts": {"start": "node index.js"}}),
        "index.js": "process.stdout.write('ok');\n",
    })
    with pytest.raises(RuntimeError, match="项目测试"):
        checker(tmp_path)._system_adapter(source, sha)


def test_external_service_manifest_is_not_self_contained(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "requirements.txt").write_text("fastapi\nredis\n", encoding="utf-8")
    assert "无外部依赖" in checker(tmp_path)._external_dependency_issue(source)


def test_fullstack_node_adapter_prefers_application_start_and_builds_frontend(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"dev": "vite", "build": "vite build", "start": "node server.js",
                    "test": "node --test"},
        "devDependencies": {"vite": "8.0.0"},
    }), encoding="utf-8")
    dockerfile, start, verify = ArtifactChecker._adapter_recipe(tmp_path)
    assert start == "npm start"
    assert "npm run build" in dockerfile
    assert verify == "npm test"


@pytest.mark.skipif(not os.environ.get("PAIRWISE_DOCKER_INTEGRATION"), reason="explicit Docker integration only")
def test_host_replays_dockerless_delivery_without_changing_commit(tmp_path):
    source = tmp_path / "source"
    sha = committed_project(source, {
        "package.json": json.dumps({"scripts": {"start": "node server.js", "test": "node --test"}}),
        "server.js": (
            "const http = require('node:http');\n"
            "http.createServer((_req, res) => { res.writeHead(200); res.end('ready'); })"
            ".listen(Number(process.env.PORT || 8080), '0.0.0.0');\n"
        ),
        "test/smoke.test.js": (
            "const test = require('node:test');\n"
            "const assert = require('node:assert/strict');\n"
            "test('result', () => assert.equal(1 + 1, 2));\n"
        ),
    })
    result = checker(tmp_path)._probe(source, "adapter-smoke-" + sha[:8], sha,
                                      allow_system_adapter=True, require_self_contained=True)
    assert result["status"] == "passed", result
    assert "artifact-adapters" in result["compose_file"]
    assert not (source / "Dockerfile").exists()
    compose = Path(result["compose_file"])
    env, ports = isolated_compose_environment(compose)
    base = ["docker", "compose", "-p", "adapter-recording-smoke-" + sha[:8], "-f", str(compose)]
    try:
        subprocess.run(base + ["up", "-d", "--build", "app", "gateway"], cwd=compose.parent,
                       env=env, check=True, capture_output=True)
        for attempt in range(60):
            try:
                with urllib.request.urlopen("http://127.0.0.1:" + ports["APP_PORT"], timeout=2) as response:
                    assert response.read() == b"ready"
                break
            except OSError:
                if attempt == 59:
                    raise
                time.sleep(0.5)
    finally:
        subprocess.run(base + ["down", "-v", "--remove-orphans"], cwd=compose.parent,
                       env=env, check=False, capture_output=True)
