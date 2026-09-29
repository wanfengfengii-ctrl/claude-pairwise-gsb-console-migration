"""Conservative, explainable A/B recording similarity for human review."""

import json
from typing import Any, Dict, Iterable


def _json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _interaction(row: Dict[str, Any]) -> Dict[str, Any]:
    detail = _json(row.get("detail_json", ""), {})
    for event in detail.get("recorderEvents", []):
        if event.get("event") == "interaction":
            return event
    return {}


def _workflow(row: Dict[str, Any]) -> tuple:
    steps = _json(row.get("steps_json", ""), [])
    if isinstance(steps, list) and steps:
        return tuple(
            (str(step.get("method", "")).lower(), str(step.get("path", "")),
             json.dumps(step.get("body"), sort_keys=True, ensure_ascii=False))
            for step in steps if isinstance(step, dict) and step.get("path")
        )
    event = _interaction(row)
    if event.get("path") and event.get("method"):
        return ((str(event["method"]).lower(), str(event["path"]), ""),)
    return ()


def similar_recording_pairs(rows: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Only flag matching *observed* workflows with near-equal durations.

    Equal length alone is never evidence of duplicate content.  This is a
    search aid, not a pass/fail gate or a visual-frame similarity claim.
    """
    grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        if row.get("status") == "passed" and row.get("commit_match"):
            grouped.setdefault(row["pair_id"], {})[row["arm"]] = row
    matches = {}
    for pair_id, arms in grouped.items():
        if "A" not in arms or "B" not in arms:
            continue
        a, b = arms["A"], arms["B"]
        if a.get("sha256") and a["sha256"] == b.get("sha256"):
            matches[pair_id] = "两侧录像文件哈希相同"
            continue
        a_duration, b_duration = float(a.get("duration_seconds") or 0), float(b.get("duration_seconds") or 0)
        if min(a_duration, b_duration) <= 0 or abs(a_duration - b_duration) > max(2.0, max(a_duration, b_duration) * 0.15):
            continue
        a_workflow, b_workflow = _workflow(a), _workflow(b)
        if a_workflow and a_workflow == b_workflow:
            matches[pair_id] = "业务请求流程相同且时长接近"
            continue
        a_event, b_event = _interaction(a), _interaction(b)
        a_clicks, b_clicks = a_event.get("clicks"), b_event.get("clicks")
        if isinstance(a_clicks, list) and a_clicks and a_clicks == b_clicks:
            matches[pair_id] = "页面操作顺序相同且时长接近"
    return matches
