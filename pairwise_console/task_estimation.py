"""Independent, itemized end-to-end estimates for generated tasks."""

from __future__ import annotations

import re
from typing import Any

from .prompts import GENERATED_TASK_MAX_ESTIMATED_MINUTES

TARGET_MINUTES = GENERATED_TASK_MAX_ESTIMATED_MINUTES


def summarize_reviewed_estimate(
    work_items: Any, generated_min: int, generated_max: int
) -> dict[str, Any]:
    """Audit development and Docker work separately, but cap their combined time."""
    if not isinstance(work_items, list) or not 3 <= len(work_items) <= 8:
        raise ValueError("独立估时必须包含 3 至 8 个工作项")
    normalized: list[dict[str, Any]] = []
    for item in work_items:
        if not isinstance(item, dict):
            raise ValueError("独立估时工作项格式无效")
        name = str(item.get("name") or "").strip()
        basis = str(item.get("basis") or "").strip()
        phase = str(item.get("phase") or "").strip()
        low = item.get("minMinutes")
        high = item.get("maxMinutes")
        if (not name or not basis or phase not in ("development", "docker_delivery")
                or not isinstance(low, int) or isinstance(low, bool)
                or not isinstance(high, int) or isinstance(high, bool)
                or low < 0 or high < low):
            raise ValueError("独立估时工作项缺少阶段、有效依据或分钟区间")
        if phase == "development" and re.search(
            r"docker|compose|dockerfile|容器|镜像|verify\s*服务|清洁环境验收",
            name + " " + basis, re.I,
        ):
            raise ValueError("Docker 交付或清洁验收不能计入开发阶段")
        normalized.append({"name": name, "phase": phase, "minMinutes": low,
                           "maxMinutes": high, "basis": basis})
    development = [item for item in normalized if item["phase"] == "development"]
    docker_delivery = [item for item in normalized if item["phase"] == "docker_delivery"]
    if len(development) < 2 or not docker_delivery:
        raise ValueError("独立估时需分列至少两项业务开发和一项后续 Docker 交付/验收")
    reviewed_min = sum(item["minMinutes"] for item in normalized)
    reviewed_max = sum(item["maxMinutes"] for item in normalized)
    if reviewed_min < 10:
        raise ValueError("独立估时总下界低于 10 分钟")
    risks: list[str] = []
    if reviewed_max > TARGET_MINUTES or generated_max > TARGET_MINUTES:
        risks.append(f"超过{TARGET_MINUTES}分钟上限")
    if generated_max and reviewed_max > generated_max + 30:
        risks.append("生成方可能低估")
    if generated_min and reviewed_min + 30 < generated_min:
        risks.append("生成方可能高估")
    return {
        "min": reviewed_min, "max": reviewed_max,
        "dockerDeliveryMin": sum(item["minMinutes"] for item in docker_delivery),
        "dockerDeliveryMax": sum(item["maxMinutes"] for item in docker_delivery),
        "workItems": normalized, "risk": "、".join(risks),
    }
