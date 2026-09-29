import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class CommandResult:
    args: List[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult):
        detail = (result.stderr or result.stdout or "command failed").strip()
        super().__init__(detail[-4000:])
        self.result = result


def run_command(
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: Optional[int] = 120,
    check: bool = True,
    env: Optional[Dict[str, str]] = None,
    input_text: Optional[str] = None,
) -> CommandResult:
    # The LaunchAgent currently runs Apple's Python 3.9.  Its fork path can
    # deadlock when one monitor forks while another thread is allocating in
    # SQLite (fork waits for malloc; SQLite waits to reacquire the GIL).  Keep
    # cwd handling inside a tiny shell and make the executable absolute so
    # subprocess can use posix_spawn instead of fork.  Arguments remain
    # positional parameters, so no user/task text is interpolated as shell.
    command = list(args)
    if cwd:
        command = [
            "/bin/sh", "-c",
            'cd "$1" || exit 125; shift; exec "$@"',
            "pairwise-command", str(cwd), *command,
        ]
    elif command:
        executable = command[0]
        if os.path.dirname(executable):
            command[0] = str(Path(executable).expanduser().resolve())
        else:
            command[0] = shutil.which(
                executable, path=(env or os.environ).get("PATH"),
            ) or executable
    process = subprocess.run(
        command,
        cwd=None,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env or os.environ.copy(),
        close_fds=False,
    )
    result = CommandResult(args, str(cwd or ""), process.returncode, process.stdout, process.stderr)
    if check and process.returncode != 0:
        raise CommandError(result)
    return result


def redact(text: str) -> str:
    value = text or ""
    for marker in ("ghp_", "github_pat_", "sk-ant-", "Bearer "):
        start = 0
        while True:
            index = value.find(marker, start)
            if index < 0:
                break
            end = index + len(marker)
            while end < len(value) and not value[end].isspace() and value[end] not in "'\"":
                end += 1
            value = value[:index] + marker + "***" + value[end:]
            start = index + len(marker) + 3
    return value[-4000:]
