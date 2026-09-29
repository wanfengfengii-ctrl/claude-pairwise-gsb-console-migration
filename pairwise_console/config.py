import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


APP_NAME = "Claude A/B GSB Console"
DEFAULT_CODEX_MODEL = "gpt-5.6-terra"
DEFAULT_CLAUDE_MODEL = "auto_model/urm"
DEFAULT_CLAUDE_IMAGE = "claude-eval-runtime:prepared-2.1.269"
MAX_PAIR_PROJECTS = 5
MAX_CLAUDE_TERMINALS = MAX_PAIR_PROJECTS * 2
OLD_APP_DIR = Path.home() / "Library/Application Support/Claude Eval Console"


def _read_old_deployment() -> Dict[str, Any]:
    path = OLD_APP_DIR / ".data" / "deployment.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    data_dir: Path
    db_path: Path
    projects_dir: Path
    web_dir: Path
    old_db_path: Path
    codex_model: str
    claude_model: str
    claude_image: str
    codex_default_effort: str
    codex_bug_effort: str
    max_pairs_parallel: int
    max_claude_terminals: int
    task_generation_max_parallel: int
    github_owner: str
    github_visibility: str
    repository_prefix: str
    git_author_name: str
    git_author_email: str


def load_config(base_dir: Path = None) -> Config:
    root = (base_dir or Path(__file__).resolve().parent.parent).resolve()
    old = _read_old_deployment()
    data_dir = Path(os.environ.get("PAIRWISE_DATA_DIR", str(root / ".data"))).expanduser().resolve()
    image = (
        os.environ.get("PAIRWISE_CLAUDE_IMAGE")
        or old.get("CLAUDE_EVAL_DOCKER_IMAGE")
        or old.get("claude_eval_docker_image")
        or DEFAULT_CLAUDE_IMAGE
    )
    return Config(
        host=os.environ.get("PAIRWISE_HOST", "127.0.0.1"),
        port=int(os.environ.get("PAIRWISE_PORT", "8865")),
        data_dir=data_dir,
        db_path=data_dir / "pairwise.db",
        projects_dir=Path(os.environ.get("PAIRWISE_PROJECTS_DIR", str(root / "projects"))).expanduser().resolve(),
        web_dir=root / "web",
        old_db_path=Path(os.environ.get("PAIRWISE_OLD_DB", str(OLD_APP_DIR / ".data" / "console.db"))).expanduser(),
        codex_model=os.environ.get("PAIRWISE_CODEX_MODEL", DEFAULT_CODEX_MODEL),
        claude_model=os.environ.get("PAIRWISE_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL),
        claude_image=str(image),
        codex_default_effort=os.environ.get("PAIRWISE_CODEX_EFFORT", "medium"),
        codex_bug_effort=os.environ.get("PAIRWISE_CODEX_BUG_EFFORT", "high"),
        max_pairs_parallel=max(1, min(MAX_PAIR_PROJECTS, int(os.environ.get("PAIRWISE_MAX_PARALLEL", "3")))),
        max_claude_terminals=max(
            1,
            min(MAX_CLAUDE_TERMINALS, int(os.environ.get("PAIRWISE_MAX_TERMINALS", "3"))),
        ),
        task_generation_max_parallel=int(os.environ.get("PAIRWISE_TASK_GENERATION_PARALLEL", "6")),
        github_owner=os.environ.get("PAIRWISE_GITHUB_OWNER", ""),
        github_visibility=os.environ.get("PAIRWISE_GITHUB_VISIBILITY", "public"),
        repository_prefix=os.environ.get("PAIRWISE_REPOSITORY_PREFIX", ""),
        git_author_name=os.environ.get("PAIRWISE_GIT_AUTHOR_NAME", "刘昱"),
        git_author_email=os.environ.get("PAIRWISE_GIT_AUTHOR_EMAIL", ""),
    )
