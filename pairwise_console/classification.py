import json
import re
from pathlib import Path
from typing import Any


PROJECT_CATEGORIES = ("纯后端", "纯前端", "全栈")

PRIMARY_FRAMEWORKS = {
    "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
    "react": "React", "vue": "Vue", "vue.js": "Vue.js", "angular": "Angular",
    "svelte": "Svelte", "sveltekit": "SvelteKit", "solidjs": "SolidJS",
    "next.js": "Next.js", "nextjs": "Next.js", "nuxt": "Nuxt",
    "express": "Express", "express.js": "Express", "nestjs": "NestJS", "hono": "Hono",
    "spring": "Spring", "spring boot": "Spring Boot",
    "gin": "Gin", "fiber": "Fiber", "echo": "Echo", "rails": "Rails",
    "ruby on rails": "Rails", "laravel": "Laravel", "symfony": "Symfony",
    "asp.net": "ASP.NET", "asp.net core": "ASP.NET Core", "actix": "Actix",
    "axum": "Axum", "rocket": "Rocket", "ktor": "Ktor", "phoenix": "Phoenix",
    "flutter": "Flutter", "electron": "Electron", "tauri": "Tauri",
}
PRIMARY_LANGUAGES = {
    "python": "Python", "typescript": "TypeScript", "javascript": "JavaScript",
    "go": "Go", "golang": "Go", "java": "Java", "kotlin": "Kotlin", "rust": "Rust",
    "c#": "C#", "c++": "C++", "c": "C", "ruby": "Ruby", "php": "PHP",
    "swift": "Swift", "dart": "Dart", "scala": "Scala", "elixir": "Elixir",
    "erlang": "Erlang", "node.js": "Node.js", "nodejs": "Node.js",
    "deno": "Deno", "bun": "Bun",
}


def normalize_stack(value: Any = "") -> str:
    """Return a compact, comma-separated list of technology names.

    The submission field is not a general technology-stack description. Keep
    only the main programming languages/runtimes and application frameworks;
    omit libraries, build tools, tests, databases and container tooling.
    """
    raw = str(value or "").strip()
    parts = re.split(r"[,，、;；\n|]+|\s+\+\s+", raw)
    names = []
    seen = set()
    for part in parts:
        token = part.strip().strip(".。:：-—•· ")
        if not token or re.search(r"[\u3400-\u9fff]", token):
            continue
        # A slash commonly joins two library names in generated metadata.
        candidates = re.split(r"\s*/\s*", token) if re.fullmatch(
            r"[A-Za-z0-9_.+#() -]+\s*/\s*[A-Za-z0-9_.+#() -]+", token
        ) else [token]
        for candidate in candidates:
            candidate = re.sub(r"\s+", " ", candidate).strip()
            if not candidate or len(candidate) > 48 or ":" in candidate:
                continue
            versioned = re.fullmatch(r"(.+?)\s+(\d+(?:\.\d+)*)", candidate)
            base = (versioned.group(1) if versioned else candidate).casefold()
            if base in PRIMARY_LANGUAGES:
                candidate = PRIMARY_LANGUAGES[base] + ((" " + versioned.group(2)) if versioned else "")
            elif base in PRIMARY_FRAMEWORKS:
                candidate = PRIMARY_FRAMEWORKS[base] + ((" " + versioned.group(2)) if versioned else "")
            else:
                continue
            key = candidate.casefold()
            if key not in seen:
                seen.add(key)
                names.append(candidate)
    return ", ".join(names)[:255]


def normalize_language_framework(value: Any = "") -> str:
    """Backward-compatible name for the primary stack normalizer."""
    return normalize_stack(value)


def infer_project_stack(root: Any, fallback: Any = "") -> str:
    """Infer the delivered project's main language/framework from its files.

    Follow-up Bug tasks run against one concrete Arm artifact, which may use a
    different implementation stack from the original generated task.  Prefer
    the artifact manifests over that historical task metadata so the
    submission field describes the code developers will actually edit.
    """
    project = Path(str(root or ""))
    if not project.is_dir():
        return normalize_stack(fallback)

    def read(name: str) -> str:
        try:
            return (project / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    dockerfile = read("Dockerfile")
    requirements = "\n".join(
        read(name) for name in ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "Pipfile")
    )
    go_mod = read("go.mod")
    package_raw = read("package.json")
    package_text = package_raw.casefold()
    try:
        package = json.loads(package_raw) if package_raw else {}
        package_text += " " + json.dumps(
            {key: package.get(key, {}) for key in ("dependencies", "devDependencies")},
            ensure_ascii=True,
        ).casefold()
    except (TypeError, ValueError):
        pass

    names = []
    python_match = re.search(r"(?im)^\s*from\s+python:(\d+(?:\.\d+)*)", dockerfile)
    if requirements.strip() or python_match or any(project.glob("*.py")):
        names.append("Python" + ((" " + python_match.group(1)) if python_match else ""))
        if re.search(r"(?im)(?:^|[^a-z])fastapi(?:[^a-z]|$)", requirements):
            names.append("FastAPI")
        elif re.search(r"(?im)(?:^|[^a-z])django(?:[^a-z]|$)", requirements):
            names.append("Django")
        elif re.search(r"(?im)(?:^|[^a-z])flask(?:[^a-z]|$)", requirements):
            names.append("Flask")

    if go_mod.strip():
        go_match = re.search(r"(?im)^\s*go\s+(\d+(?:\.\d+)*)", go_mod)
        names.append("Go" + ((" " + go_match.group(1)) if go_match else ""))
        if "github.com/gin-gonic/gin" in go_mod:
            names.append("Gin")
        elif "github.com/gofiber/fiber" in go_mod:
            names.append("Fiber")
        elif "github.com/labstack/echo" in go_mod:
            names.append("Echo")

    if package_raw:
        names.append("TypeScript" if (project / "tsconfig.json").exists() else "JavaScript")
        for marker, label in (
            ('"react"', "React"), ('"next"', "Next.js"), ('"vue"', "Vue"),
            ('"svelte"', "Svelte"), ('"@nestjs/', "NestJS"), ('"express"', "Express"),
        ):
            if marker in package_text:
                names.append(label)
                break

    detected = normalize_stack(", ".join(names))
    return detected or normalize_stack(fallback)


def normalize_project_category(value: Any = "", *context: Any) -> str:
    """Return one of the three project categories used by the console.

    Explicit metadata wins.  The fallback is deliberately conservative and is
    only used for historical rows created before project_category was stored.
    """
    candidate = str(value or "").strip()
    if candidate in PROJECT_CATEGORIES:
        return candidate

    text = " ".join(str(item or "") for item in context).casefold()
    for label in PROJECT_CATEGORIES:
        if label in text:
            return label

    frontend_markers = ("react", "vue", "vite", "svelte", "angular", "纯前端", "浏览器内")
    backend_markers = (
        "fastapi", "django", "flask", "spring boot", "gin", "gorm", "fiber", "sqlalchemy",
        "alembic", "postgresql", "mysql", "redis", "纯后端", "后端服务", "rest api",
    )
    has_frontend = any(marker in text for marker in frontend_markers)
    has_backend = any(marker in text for marker in backend_markers)
    if has_frontend and has_backend:
        return "全栈"
    if has_frontend:
        return "纯前端"
    return "纯后端"
