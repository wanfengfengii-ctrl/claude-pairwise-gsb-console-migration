from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from .db import Database


SHANGHAI = timezone(timedelta(hours=8))


def dashboard(db: Database) -> Dict[str, Any]:
    task_counts = db.all("SELECT status,COUNT(*) count FROM tasks GROUP BY status")
    pair_counts = db.all("SELECT status,COUNT(*) count FROM pairs GROUP BY status")
    verdicts = db.all("SELECT verdict,COUNT(*) count FROM gsb_reviews WHERE status='confirmed' GROUP BY verdict")
    types = db.all(
        """SELECT t.task_type,COUNT(*) count FROM pairs p JOIN tasks t ON t.id=p.task_id
           GROUP BY t.task_type ORDER BY count DESC,t.task_type"""
    )
    categories = db.all(
        """SELECT t.project_category,COUNT(*) count FROM pairs p JOIN tasks t ON t.id=p.task_id
           GROUP BY t.project_category ORDER BY count DESC,t.project_category"""
    )
    artifact = db.all("SELECT status,COUNT(*) count FROM artifact_checks GROUP BY status")
    recording = db.all("SELECT status,COUNT(*) count FROM recordings GROUP BY status")
    recent_rows = db.all(
        """WITH first_completions AS (
             SELECT entity_id,MIN(created_at) completed_at FROM audit_events
              WHERE event_type='gsb.confirmed' GROUP BY entity_id
           )
           SELECT substr(datetime(completed_at,'+8 hours'),1,13) hour,COUNT(*) count
           FROM first_completions WHERE datetime(completed_at) >= datetime('now','-24 hours')
           GROUP BY hour ORDER BY hour"""
    )
    recent_counts = {row["hour"]: int(row["count"]) for row in recent_rows}
    current_hour = datetime.now(SHANGHAI).replace(minute=0, second=0, microsecond=0)
    recent = []
    for offset in range(23, -1, -1):
        point = current_hour - timedelta(hours=offset)
        key = point.strftime("%Y-%m-%d %H")
        recent.append({"hour": key, "label": point.strftime("%H"), "count": recent_counts.get(key, 0)})
    total = db.one("SELECT COUNT(*) count FROM pairs") or {"count": 0}
    completed_total = db.one("SELECT COUNT(*) count FROM pairs WHERE status='completed'") or {"count": 0}
    active = db.one(
        "SELECT COUNT(*) count FROM pairs WHERE status IN ('queued','running','review')"
    ) or {"count": 0}
    confirmed = db.one("SELECT COUNT(*) count FROM gsb_reviews WHERE status='confirmed'") or {"count": 0}
    peak = max(recent, key=lambda x: x["count"], default={"hour": "", "count": 0})
    active_hours = sum(1 for row in recent if row["count"])
    return {
        "generatedAt": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "summary": {
            "totalPairs": total["count"],
            "completedPairs": completed_total["count"],
            "activePairs": active["count"],
            "confirmedGsb": confirmed["count"],
            "peakHour": peak["hour"],
            "peakHourCount": peak["count"],
            "activeHours24h": active_hours,
            "completedPairs24h": sum(x["count"] for x in recent),
            "hourlyAverage24h": round(sum(x["count"] for x in recent) / 24.0, 2),
        },
        "taskStatus": task_counts,
        "pairStatus": pair_counts,
        "verdicts": verdicts,
        "taskTypes": types,
        "projectCategories": categories,
        "artifactChecks": artifact,
        "recordings": recording,
        "trend24h": recent,
    }
