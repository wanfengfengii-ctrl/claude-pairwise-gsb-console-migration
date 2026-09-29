"""Durable backoff for background jobs; never changes Claude failure budgets."""
import json
from datetime import datetime, timedelta, timezone


def failure_kind(message):
    text = str(message).casefold()
    if any(token in text for token in (
        "flagged for possible cybersecurity", "trusted access for cyber",
        "policy violation", "insufficient permissions", "unauthorized", "forbidden",
        "authentication", "permission denied", "安全拦截", "权限不足",
    )):
        return "blocked"
    if any(token in text for token in (
        "429", "504", "502", "timeout", "timed out", "connection", "network",
        "rate limit", "docker daemon", "port is already allocated", "证书", "网络",
    )):
        return "transient"
    return "unknown"


def cli_event_error(path):
    """Read only structured errors, not model prose or tool output."""
    try:
        # Read bounded tail even after a long model run.
        with open(path, "rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 131072))
            lines = stream.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") not in ("error", "turn.failed"):
                continue
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else event.get("message")
            if message:
                return str(message)[:2000]
    except OSError:
        pass
    return ""


def operation_ready(db, operation):
    row = db.one("SELECT * FROM pipeline_operations WHERE operation=?", (operation,))
    if not row:
        return True
    if row["status"] == "blocked":
        return False
    after = str(row.get("retry_after") or "")
    return not after or after <= datetime.now(timezone.utc).isoformat(timespec="seconds")


def operation_failed(db, operation, error):
    kind = failure_kind(error)
    now = datetime.now(timezone.utc)
    with db.transaction() as conn:
        row = conn.execute("SELECT attempts FROM pipeline_operations WHERE operation=?", (operation,)).fetchone()
        attempts = int(row[0]) + 1 if row else 1
        # Pause repeated unknown failures too: an unavailable source must not
        # monopolize discovery. Explicit operator review can clear the row.
        blocked = kind == "blocked" or (kind == "unknown" and attempts >= 3)
        delay = min(3600, 60 * (2 ** min(attempts - 1, 6)))
        retry = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO pipeline_operations(operation,status,attempts,error_kind,error,retry_after,updated_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(operation) DO UPDATE SET
               status=excluded.status,attempts=excluded.attempts,error_kind=excluded.error_kind,
               error=excluded.error,retry_after=excluded.retry_after,updated_at=excluded.updated_at""",
            (operation, "blocked" if blocked else "cooling", attempts, kind,
             str(error)[-2000:], retry, now.isoformat(timespec="seconds")),
        )
    return {"kind": kind, "attempts": attempts, "blocked": blocked, "retryAfter": retry}
