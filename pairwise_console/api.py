import json
import mimetypes
import re
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .analytics import dashboard
from .config import APP_NAME, Config, MAX_CLAUDE_TERMINALS, MAX_PAIR_PROJECTS
from .db import Database, now_iso
from .exports import build_xlsx
from .service import PairwiseService
from .recording_similarity import similar_recording_pairs


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, config: Config, db: Database, service: PairwiseService):
        super().__init__(address, handler)
        self.config = config
        self.db = db
        self.service = service


class Handler(BaseHTTPRequestHandler):
    server_version = "PairwiseGSB/0.1"

    @property
    def app(self) -> AppServer:
        return self.server  # type: ignore

    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)

    def do_GET(self) -> None:
        try:
            path, query = self._path_query()
            if path == "/api/health":
                return self._json(200, {"ok": True, "name": APP_NAME, "time": now_iso()})
            if path == "/api/preflight":
                return self._json(200, self.app.service.preflight())
            if path == "/api/automation":
                return self._json(200, self.app.service.automation_status())
            if path == "/api/dashboard":
                return self._json(200, dashboard(self.app.db))
            if path == "/api/settings":
                rows = self.app.db.all("SELECT key,value_json,updated_at FROM settings ORDER BY key")
                return self._json(200, {row["key"]: json.loads(row["value_json"]) for row in rows})
            if path == "/api/tasks":
                return self._json(200, self._tasks_page(query))
            match = re.fullmatch(r"/api/tasks/([^/]+)", path)
            if match:
                task = self.app.db.one("SELECT * FROM tasks WHERE id=?", (match.group(1),))
                return self._json(200, task) if task else self._json(404, {"error": "题目不存在"})
            if path == "/api/pairs":
                return self._json(200, self._pairs_page(query))
            match = re.fullmatch(r"/api/pairs/([^/]+)", path)
            if match:
                return self._json(200, self.app.service.pair_detail(match.group(1)))
            if path == "/api/codex-jobs":
                return self._json(200, self._page("codex_jobs", query, self._filter(query, ("status", "job_type"))))
            if path == "/api/bug-candidates":
                return self._json(200, self._page("bug_candidates", query, self._filter(query, ("status", "difficulty"))))
            if path == "/api/evidence":
                return self._json(200, self._evidence_page(query))
            if path == "/api/artifact-checks":
                return self._json(200, self._page("artifact_checks", query, self._filter(query, ("status", "arm"))))
            if path == "/api/recordings":
                return self._json(200, self._page("recordings", query, self._filter(query, ("status", "arm"))))
            if path == "/api/recordings/active":
                return self._json(200, self._active_recording())
            match = re.fullmatch(r"/api/recordings/([^/]+)/content", path)
            if match:
                return self._recording_content(match.group(1))
            if path == "/api/gsb-reviews":
                return self._json(200, self._reviews_page(query))
            if path == "/api/deliveries":
                return self._json(200, self._deliveries_page(query))
            if path == "/api/solo-qa/submissions":
                return self._json(200, {"items": self.app.db.all(
                    """SELECT pair_id,status,remote_id,remote_url,payload_sha256,remote_status,
                              qc_summary,remote_updated_at,error,submitted_at,updated_at
                       FROM delivery_submissions WHERE remote_id<>'' ORDER BY updated_at DESC"""
                )})
            match = re.fullmatch(r"/api/solo-qa/pairs/(pair-[a-f0-9]{16})/payload", path)
            if match:
                return self._json(200, self.app.service.solo_qa_payload(match.group(1)))
            match = re.fullmatch(r"/api/solo-qa/pairs/(pair-[a-f0-9]{16})/files/([a-z_]+)", path)
            if match:
                return self._solo_qa_file_content(match.group(1), match.group(2))
            if path == "/api/audit":
                return self._json(200, self._page("audit_events", query, self._filter(query, ("event_type", "entity_type")), order="id DESC"))
            match = re.fullmatch(r"/api/operations/([^/]+)", path)
            if match:
                return self._json(200, self.app.service.operation(match.group(1)))
            return self._static(path)
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path, _ = self._path_query()
            body = self._body()
            if path == "/api/automation/start":
                return self._json(200, self.app.service.set_auto_pipeline(True))
            if path == "/api/automation/stop":
                return self._json(200, self.app.service.set_auto_pipeline(False))
            if path == "/api/tasks/import-historical":
                return self._json(200, self.app.service.import_historical(int(body.get("limit", 500))))
            if path == "/api/tasks/generate":
                operation = self.app.service.generate_tasks_async(int(body.get("count", 1)), str(body.get("taskType", "zero_to_one")))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/tasks/([^/]+)/validate", path)
            if match:
                operation = self.app.service.validate_task_async(match.group(1))
                return self._json(202, {"operationId": operation})
            if path == "/api/pairs":
                return self._json(201, self.app.service.create_pair(str(body.get("taskId", ""))))
            match = re.fullmatch(r"/api/pairs/([^/]+)/prepare", path)
            if match:
                operation = self.app.service.prepare_pair_repository_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/start", path)
            if match:
                operation = self.app.service.start_pair_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/lineage/normalize", path)
            if match:
                return self._json(200, self.app.service.normalize_delivery_lineage(match.group(1)))
            match = re.fullmatch(r"/api/pairs/([^/]+)/cancel", path)
            if match:
                operation = self.app.service.cancel_pair_async(
                    match.group(1), str(body.get("reason", "人工停止")),
                )
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/pause-claude", path)
            if match:
                return self._json(200, self.app.service.pause_claude_sessions(
                    match.group(1), str(body.get("reason", "人工暂停模型")),
                ))
            match = re.fullmatch(r"/api/pairs/([^/]+)/reset-retries", path)
            if match:
                return self._json(200, self.app.service.reset_pair_retries(match.group(1)))
            match = re.fullmatch(r"/api/pairs/([^/]+)/arms/([AB])/queue", path)
            if match:
                operation = self.app.service.queue_arm_manually_async(
                    match.group(1), match.group(2),
                    int(body.get("exceptionApprovalId") or 0),
                )
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/arms/([AB])/restart-active", path)
            if match:
                operation = self.app.service.restart_active_arm_async(match.group(1), match.group(2))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/difficulty", path)
            if match:
                return self._json(200, self.app.service.edit_pair_difficulty(
                    match.group(1), str(body.get("difficulty", "")), str(body.get("note", "")),
                ))
            match = re.fullmatch(r"/api/pairs/([^/]+)/difficulty/review", path)
            if match:
                operation = self.app.service.reassess_actual_difficulty_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/gsb", path)
            if match:
                operation = self.app.service.generate_gsb_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/delivery-assessment", path)
            if match:
                operation = self.app.service.generate_delivery_assessment_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/delivery-assessment/confirm", path)
            if match:
                return self._json(200, self.app.service.update_delivery_assessment(
                    match.group(1), body.get("aScoreDelivery"), body.get("aDescDelivery"),
                    body.get("bScoreDelivery"), body.get("bDescDelivery"),
                    str(body.get("expectedUpdatedAt", "")), body.get("expectedAssessment"),
                ))
            match = re.fullmatch(r"/api/pairs/([^/]+)/traces/([AB])/repair", path)
            if match:
                operation = self.app.service.repair_trace_prompt_async(match.group(1), match.group(2))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/gsb/recheck", path)
            if match:
                operation = self.app.service.recheck_gsb_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/gsb/colloquiali[sz]e", path)
            if match:
                operation = self.app.service.colloquialize_gsb_async(match.group(1), body)
                return self._json(202, {"operationId": operation})
            if path == "/api/gsb-reviews/colloquialize":
                jobs = []
                for pair_id in self._pair_ids(body):
                    source = self.app.service.gsb_colloquial_source(pair_id)
                    operation = self.app.service.colloquialize_gsb_async(pair_id, source)
                    jobs.append({"pairId": pair_id, "operationId": operation, "source": source})
                return self._json(202, {"count": len(jobs), "jobs": jobs})
            if path == "/api/gsb-reviews/colloquialize/apply":
                return self._json(200, self.app.service.apply_gsb_colloquial_batch(body.get("items")))
            if path == "/api/gsb-reviews/recheck":
                pair_ids = self._pair_ids(body)
                operations = [self.app.service.recheck_gsb_async(pair_id) for pair_id in pair_ids]
                return self._json(202, {"count": len(operations), "operationIds": operations})
            if path == "/api/gsb-reviews/recheck/apply":
                return self._json(200, self.app.service.apply_latest_gsb_rechecks(self._pair_ids(body)))
            match = re.fullmatch(r"/api/pairs/([^/]+)/gsb/recheck/([^/]+)/apply", path)
            if match:
                return self._json(200, self.app.service.apply_gsb_recheck(match.group(1), match.group(2)))
            match = re.fullmatch(r"/api/pairs/([^/]+)/bugs/discover", path)
            if match:
                operation = self.app.service.discover_bugs_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/features/generate", path)
            if match:
                operation = self.app.service.generate_followup_feature_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/recordings/([AB])/(start|stop)", path)
            if match:
                pair_id, arm, action = match.groups()
                if action == "start":
                    result = self.app.service.start_recording(
                        pair_id, arm, int(body.get("x", 0)), int(body.get("y", 0)), bool(body.get("manual", False)),
                        body.get("demoOverride"),
                    )
                else:
                    result = self.app.service.stop_recording(pair_id, arm)
                return self._json(200, result)
            match = re.fullmatch(r"/api/pairs/([^/]+)/gsb/confirm", path)
            if match:
                legacy_reason = str(body.get("reason", ""))
                result = self.app.service.confirm_gsb(
                    match.group(1), str(body.get("verdict", "")),
                    str(body.get("aReason", legacy_reason)), str(body.get("bReason", legacy_reason)),
                    str(body.get("confirmedBy", "人工确认")),
                )
                return self._json(200, result)
            if path == "/api/deliveries/preflight":
                pair_ids = self._pair_ids(body)
                results = [self.app.service.delivery_preflight(pair_id, include_platform=True) for pair_id in pair_ids]
                return self._json(200, {"checked_at": now_iso(), "results": results})
            if path == "/api/deliveries/export.xlsx":
                pair_ids = self._pair_ids(body)
                rows = self._delivery_rows_by_ids(pair_ids)
                payload, filename = build_xlsx(rows)
                return self._bytes(200, payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                   'attachment; filename="%s"' % filename)
            if path == "/api/solo-qa/state":
                return self._json(200, self.app.service.update_solo_qa_state(body))
            if path in ("/api/deliveries/hide", "/api/deliveries/restore", "/api/deliveries/submit"):
                pair_ids = self._pair_ids(body)
                results = []
                for pair_id in pair_ids:
                    if path.endswith("/hide"):
                        results.append(self.app.service.set_delivery_hidden(pair_id, True))
                    elif path.endswith("/restore"):
                        results.append(self.app.service.set_delivery_hidden(pair_id, False))
                    else:
                        results.append(self.app.service.submit_delivery(pair_id))
                return self._json(200, {"count": len(results), "items": results})
            if path == "/api/settings":
                retired_settings = {
                    "task_mix_zero_to_one", "task_mix_feature",
                    "task_mix_bugfix", "task_mix_started_at",
                }
                if "max_pairs_parallel" in body:
                    pair_limit = int(body["max_pairs_parallel"])
                    if pair_limit < 1 or pair_limit > MAX_PAIR_PROJECTS:
                        raise ValueError(
                            "Pair 并发只能设置为 1–%d；开发终端并发可单独设置"
                            % MAX_PAIR_PROJECTS
                        )
                if "max_claude_terminals" in body:
                    terminal_limit = int(body["max_claude_terminals"])
                    if terminal_limit < 1 or terminal_limit > MAX_CLAUDE_TERMINALS:
                        raise ValueError(
                            "Claude 开发终端并发只能设置为 1–%d"
                            % MAX_CLAUDE_TERMINALS
                        )
                if "ab_prompt_stagger_seconds" in body:
                    prompt_stagger = int(body["ab_prompt_stagger_seconds"])
                    if prompt_stagger < 0 or prompt_stagger > 300:
                        raise ValueError("A/B 题面发送间隔只能设置为 0–300 秒")
                if "feature_ready_target" in body:
                    feature_target = int(body["feature_ready_target"])
                    if feature_target < 0 or feature_target > 20:
                        raise ValueError("迭代题目储备只能设置为 0–20 道")
                for key, value in body.items():
                    if str(key) in retired_settings:
                        continue
                    self.app.db.set_setting(str(key), value)
                self.app.db.execute(
                    """DELETE FROM settings WHERE key IN
                       ('task_mix_zero_to_one','task_mix_feature','task_mix_bugfix','task_mix_started_at')"""
                )
                self.app.db.audit("settings.updated", "settings", "", {"keys": list(body)})
                return self._json(200, {"ok": True})
            match = re.fullmatch(r"/api/bug-candidates/([^/]+)/(reproduce|convert)", path)
            if match:
                candidate_id, action = match.groups()
                if action == "reproduce":
                    operation = self.app.service.reproduce_bug_async(candidate_id)
                    return self._json(202, {"operationId": operation})
                return self._json(201, self.app.service.convert_bug_to_task(candidate_id))
            self._json(404, {"error": "接口不存在"})
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"error": str(exc)})

    def _path_query(self) -> Tuple[str, Dict[str, list]]:
        parsed = urllib.parse.urlsplit(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 2_000_000:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return data

    def _json(self, status: int, value: Any) -> None:
        payload = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _bytes(self, status: int, payload: bytes, content_type: str, disposition: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(payload)

    def _recording_content(self, recording_id: str) -> None:
        row = self.app.db.one("SELECT path FROM recordings WHERE id=?", (recording_id,))
        if not row:
            return self._json(404, {"error": "录像不存在"})
        path = Path(str(row["path"])).expanduser().resolve()
        root = (self.app.config.data_dir / "recordings").resolve()
        if root not in path.parents or not path.is_file():
            return self._json(404, {"error": "录像文件不存在"})
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                self.send_response(416); self.send_header("Content-Range", "bytes */%d" % size); self.end_headers(); return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2) or min(size - 1, start + 4 * 1024 * 1024 - 1))
            else:
                suffix = int(match.group(2) or 0)
                start = max(0, size - suffix)
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416); self.send_header("Content-Range", "bytes */%d" % size); self.end_headers(); return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "video/quicktime")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "private, max-age=60")
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _solo_qa_file_content(self, pair_id: str, field_key: str) -> None:
        item = self.app.service.solo_qa_file(pair_id, field_key)
        path = Path(str(item["path"])).resolve()
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", str(item.get("content_type") or "application/octet-stream"))
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % path.name.replace('"', ""))
        self.send_header("X-Content-SHA256", str(item.get("sha256") or ""))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (self.app.config.web_dir / relative).resolve()
        root = self.app.config.web_dir.resolve()
        if root not in target.parents and target != root:
            return self._json(403, {"error": "拒绝访问"})
        if not target.exists() or not target.is_file():
            # SPA fallback for client-side routes.
            target = root / "index.html"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _page(self, table: str, query: Dict[str, list], condition, order: str = "created_at DESC") -> Dict[str, Any]:
        page = int((query.get("page") or ["1"])[0])
        size = int((query.get("size") or ["20"])[0])
        where, params = condition
        return self.app.db.page(table, page, size, where, params, order)

    def _active_recording(self) -> Dict[str, Any]:
        return self.app.db.one(
            """SELECT ra.*,t.title FROM recording_attempts ra
                 JOIN pairs p ON p.id=ra.pair_id
                 JOIN tasks t ON t.id=p.task_id
                WHERE ra.status IN ('starting','recording','stopping')
                ORDER BY ra.created_at DESC LIMIT 1"""
        ) or {}

    @staticmethod
    def _filter(query: Dict[str, list], fields) -> Tuple[str, tuple]:
        clauses, params = [], []
        for field in fields:
            value = (query.get(field) or [""])[0].strip()
            if value:
                clauses.append("%s=?" % field)
                params.append(value)
        return " AND ".join(clauses) or "1=1", tuple(params)

    def _tasks_page(self, query: Dict[str, list]) -> Dict[str, Any]:
        clauses, params = [], []
        q = self._query(query, "q")
        if q:
            clauses.append("(t.id LIKE ? OR t.title LIKE ? OR t.prompt LIKE ?)")
            params += ["%" + q + "%"] * 3
        for key in ("status", "task_type", "difficulty", "project_category"):
            value = self._query(query, key)
            if value:
                clauses.append("t.%s=?" % key)
                params.append(value)
        usage = self._query(query, "usage")
        if usage == "used":
            clauses.append("t.status='used'")
        elif usage == "unused":
            clauses.append("t.status<>'used'")
        return self._joined_page("SELECT t.*", "FROM tasks t", clauses, params, "t.created_at DESC,t.id", query)

    def _pairs_page(self, query: Dict[str, list]) -> Dict[str, Any]:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = min(100, max(1, int((query.get("size") or ["20"])[0])))
        clauses, params = [], []
        q = self._query(query, "q")
        if q:
            clauses.append("(p.id LIKE ? OR p.chain_id LIKE ? OR t.title LIKE ?)")
            params += ["%" + q + "%"] * 3
        for key, column in (("status", "p.status"), ("stage", "p.stage"), ("task_type", "t.task_type"),
                            ("difficulty", "t.difficulty"), ("project_category", "t.project_category")):
            value = self._query(query, key)
            if value:
                clauses.append(column + "=?")
                params.append(value)
        where = " AND ".join(clauses) or "1=1"
        from_sql = "FROM pairs p JOIN tasks t ON t.id=p.task_id LEFT JOIN difficulty_reviews dr ON dr.pair_id=p.id"
        count = self.app.db.one("SELECT COUNT(*) count " + from_sql + " WHERE " + where, params) or {"count": 0}
        rows = self.app.db.all(
            """SELECT p.*,t.title,t.task_type,t.difficulty,t.project_category,
               t.estimated_minutes_min,t.estimated_minutes_max,
               t.reviewed_minutes_min,t.reviewed_minutes_max,t.estimate_risk,
               aa.status a_arm_status,aa.prompt_sent_at a_prompt_sent_at,
               aa.finished_at a_finished_at,aa.updated_at a_arm_updated_at,
               bb.status b_arm_status,bb.prompt_sent_at b_prompt_sent_at,
               bb.finished_at b_finished_at,bb.updated_at b_arm_updated_at,
               g.verdict,g.reason,g.status gsb_status,
               dr.original_difficulty,dr.a_difficulty,dr.b_difficulty,dr.assessed_difficulty,
               dr.reason difficulty_reason,dr.status difficulty_review_status
               FROM pairs p JOIN tasks t ON t.id=p.task_id
               LEFT JOIN arm_runs aa ON aa.pair_id=p.id AND aa.arm='A'
               LEFT JOIN arm_runs bb ON bb.pair_id=p.id AND bb.arm='B'
               LEFT JOIN gsb_reviews g ON g.pair_id=p.id
               LEFT JOIN difficulty_reviews dr ON dr.pair_id=p.id
               WHERE %s ORDER BY p.created_at DESC LIMIT ? OFFSET ?""" % where,
            tuple(params) + (size, (page - 1) * size),
        )
        return {"items": rows, "page": page, "size": size, "total": count["count"]}

    @staticmethod
    def _query(query: Dict[str, list], name: str) -> str:
        return (query.get(name) or [""])[0].strip()

    def _paging(self, query: Dict[str, list]) -> Tuple[int, int]:
        return max(1, int(self._query(query, "page") or "1")), min(100, max(1, int(self._query(query, "size") or "20")))

    def _joined_page(self, select_sql: str, from_sql: str, clauses, params, order: str,
                     query: Dict[str, list]) -> Dict[str, Any]:
        page, size = self._paging(query)
        where = " AND ".join(clauses) or "1=1"
        count = self.app.db.one("SELECT COUNT(*) count " + from_sql + " WHERE " + where, params) or {"count": 0}
        rows = self.app.db.all(select_sql + " " + from_sql + " WHERE " + where + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                               tuple(params) + (size, (page - 1) * size))
        total = int(count["count"])
        return {"items": rows, "page": page, "page_size": size, "size": size, "total": total,
                "total_pages": max(1, (total + size - 1) // size)}

    def _evidence_page(self, query: Dict[str, list]) -> Dict[str, Any]:
        clauses, params = [], []
        similarity_rows = self.app.db.all(
            """SELECT r.pair_id,r.arm,r.status,r.commit_match,r.sha256,r.duration_seconds,
                      r.steps_json,e.detail_json
                 FROM recordings r LEFT JOIN audit_events e ON e.id=(
                   SELECT event.id FROM audit_events event
                    WHERE event.entity_id=r.attempt_id AND event.event_type='recording.finished'
                    ORDER BY event.id DESC LIMIT 1)
                WHERE r.status='passed' AND r.commit_match=1"""
        )
        similar_pairs = similar_recording_pairs(similarity_rows)
        if self._query(query, "high_similarity") == "yes":
            if similar_pairs:
                clauses.append("p.id IN (%s)" % ",".join("?" for _ in similar_pairs))
                params.extend(similar_pairs)
            else:
                clauses.append("1=0")
        q = self._query(query, "q")
        if q:
            clauses.append("(p.id LIKE ? OR p.chain_id LIKE ? OR t.title LIKE ? OR a.commit_sha LIKE ? OR d.remote_id LIKE ?)")
            params += ["%" + q + "%"] * 5
        for key, column in (("arm", "a.arm"), ("task_type", "t.task_type"), ("project_category", "t.project_category"),
                            ("artifact_status", "c.status"), ("recording_status", "r.status"),
                            ("pair_status", "p.status"),
                            ("submission_status", "COALESCE(d.status,'not_submitted')")):
            value = self._query(query, key)
            if value:
                clauses.append(column + "=?"); params.append(value)
        if self._query(query, "recording_duration") == "over_50":
            clauses.append("r.duration_seconds>50")
        manual_rerecorded = self._query(query, "manual_rerecorded")
        # Starting an attempt must not move the row between the "人工重录"
        # filters while the operator still needs its controls.  The row is
        # classified as manually rerecorded only after the MP4 is saved and
        # validated successfully.
        manual_attempt = """EXISTS(SELECT 1 FROM recording_attempts manual
          WHERE manual.pair_id=a.pair_id AND manual.arm=a.arm
            AND manual.interaction_mode='manual' AND manual.status='passed')"""
        if manual_rerecorded == "yes":
            clauses.append(manual_attempt)
        elif manual_rerecorded == "no":
            clauses.append("NOT " + manual_attempt)
        if self._query(query, "missing") == "1":
            clauses.append(
                "(c.status IS NULL OR c.status<>'passed' OR "
                "r.status IS NULL OR r.status<>'passed' OR r.commit_match<>1)"
            )
        select = """SELECT p.id pair_id,p.chain_id project_number,p.status pair_status,p.stage pair_stage,p.error pair_error,
          p.model_scheme,p.model_a,p.model_b,
          t.title,t.task_type,t.difficulty,t.project_category,a.arm,
          dr.original_difficulty,dr.a_difficulty,dr.b_difficulty,dr.assessed_difficulty,
          dr.reason difficulty_reason,dr.status difficulty_review_status,
          a.branch,a.commit_sha,c.id check_id,c.status artifact_status,c.checks_json,c.error artifact_error,
          c.started_at check_started_at,c.finished_at check_finished_at,r.id recording_id,
          r.status recording_status,
          r.path,r.sha256,r.width,r.height,r.duration_seconds,r.commit_sha recording_commit_sha,
          r.commit_match,
          r.review_status,
          r.reviewed_by,r.reviewed_at,r.error recording_error,r.capture_mode,r.entry_url,r.updated_at,
          latest.id latest_attempt_id,latest.status latest_attempt_status,latest.interaction_mode latest_interaction_mode,latest.error latest_attempt_error,
          latest.entry_url latest_attempt_url,latest.created_at latest_attempt_at,
          CASE WHEN %s THEN 1 ELSE 0 END manual_rerecorded,
          COALESCE(d.status,'not_submitted') submission_status,d.remote_id,d.remote_status"""
        from_sql = """FROM arm_runs a JOIN pairs p ON p.id=a.pair_id JOIN tasks t ON t.id=p.task_id
          LEFT JOIN artifact_checks c ON c.pair_id=a.pair_id AND c.arm=a.arm AND c.commit_sha=a.commit_sha
          LEFT JOIN recordings r ON r.pair_id=a.pair_id AND r.arm=a.arm
          LEFT JOIN recording_attempts latest ON latest.id=(SELECT id FROM recording_attempts x
            WHERE x.pair_id=a.pair_id AND x.arm=a.arm ORDER BY x.created_at DESC LIMIT 1)
          LEFT JOIN difficulty_reviews dr ON dr.pair_id=p.id
          LEFT JOIN delivery_submissions d ON d.pair_id=p.id"""
        result = self._joined_page(select % manual_attempt, from_sql, clauses, params, "p.updated_at DESC,p.id,a.arm", query)
        result["recording_duration_filter_applied"] = self._query(query, "recording_duration") == "over_50"
        for row in result["items"]:
            row["high_similarity"] = row["pair_id"] in similar_pairs
            row["similarity_reason"] = similar_pairs.get(row["pair_id"], "")
        return result

    def _reviews_page(self, query: Dict[str, list]) -> Dict[str, Any]:
        clauses, params = [], []
        q = self._query(query, "q")
        if q:
            clauses.append("(g.pair_id LIKE ? OR p.chain_id LIKE ? OR t.title LIKE ? OR g.reason LIKE ? OR g.a_reason LIKE ? OR g.b_reason LIKE ? OR g.preference_reason LIKE ? OR g.confirmed_by LIKE ? OR d.remote_id LIKE ?)")
            params += ["%" + q + "%"] * 9
        for key, column in (("status", "g.status"), ("verdict", "g.verdict"), ("task_type", "t.task_type"),
                            ("project_category", "t.project_category"),
                            ("difficulty", "t.difficulty"), ("recheck_status", "r.result_status"),
                            ("submission_status", "COALESCE(d.status,'not_submitted')")):
            value = self._query(query, key)
            if value:
                clauses.append(column + "=?"); params.append(value)
        date_from, date_to = self._query(query, "date_from"), self._query(query, "date_to")
        review_date = "date(COALESCE(g.confirmed_at,g.updated_at),'+8 hours')"
        if date_from:
            clauses.append(review_date + ">=date(?)"); params.append(date_from)
        if date_to:
            clauses.append(review_date + "<=date(?)"); params.append(date_to)
        select = """SELECT g.id,g.pair_id,g.verdict,g.reason,g.evidence_json,g.draft_verdict,g.draft_reason,
          g.final_verdict,g.final_reason,g.evidence_version,g.a_reason,g.b_reason,
          g.a_score_delivery,g.a_desc_delivery,g.b_score_delivery,g.b_desc_delivery,
          g.status,g.confirmed_by,
          g.confirmed_at,g.created_at,g.updated_at,p.chain_id project_number,p.status pair_status,p.stage,
          p.model_scheme,p.model_a,p.model_b,t.title,t.task_type,t.difficulty,t.project_category,
          dr.original_difficulty,dr.a_difficulty,dr.b_difficulty,dr.assessed_difficulty,
          dr.reason difficulty_reason,dr.status difficulty_review_status,
          r.id recheck_id,r.result_status recheck_status,r.suggested_verdict,r.suggested_reason,
          r.suggested_a_reason,r.suggested_b_reason,r.issues_json,
          r.evidence_refs_json,r.model recheck_model,r.reasoning_effort recheck_effort,r.evidence_version recheck_evidence_version,
          r.applied_at recheck_applied_at,r.applied_by recheck_applied_by,r.created_at rechecked_at,
          COALESCE(d.status,'not_submitted') submission_status,d.remote_id,d.remote_status"""
        from_sql = """FROM gsb_reviews g JOIN pairs p ON p.id=g.pair_id JOIN tasks t ON t.id=p.task_id
          LEFT JOIN difficulty_reviews dr ON dr.pair_id=p.id
          LEFT JOIN gsb_rechecks r ON r.id=(SELECT id FROM gsb_rechecks x WHERE x.pair_id=g.pair_id ORDER BY x.created_at DESC LIMIT 1)"""
        from_sql += " LEFT JOIN delivery_submissions d ON d.pair_id=p.id"
        return self._joined_page(select, from_sql, clauses, params, "g.updated_at DESC,g.pair_id", query)

    def _delivery_select(self) -> Tuple[str, str]:
        select = """SELECT p.id pair_id,p.chain_id project_number,p.status pair_status,p.stage,p.completed_at,
          p.model_scheme,p.model_a,p.model_b,
          t.title,t.task_type,t.difficulty,t.project_category,t.stack,t.source,t.prompt,repo.remote_url,repo.main_sha,
          dr.original_difficulty,dr.a_difficulty,dr.b_difficulty,dr.assessed_difficulty,
          dr.reason difficulty_reason,dr.status difficulty_review_status,
          aa.session_id a_session_id,aa.prompt_id a_prompt_id,aa.commit_sha a_commit,
          bb.session_id b_session_id,bb.prompt_id b_prompt_id,bb.commit_sha b_commit,
          ca.status a_check_status,cb.status b_check_status,ra.id a_recording_id,
          ra.status a_recording_status,
          ra.sha256 a_recording_sha,
          ra.commit_match a_recording_match,
          ra.review_status a_recording_review_status,
          rb.id b_recording_id,
          rb.status b_recording_status,
          rb.sha256 b_recording_sha,
          rb.commit_match b_recording_match,
          rb.review_status b_recording_review_status,
          g.verdict,g.reason,g.a_reason,g.b_reason,g.status gsb_status,
          g.confirmed_by,g.confirmed_at,g.evidence_version,
          r.id recheck_id,r.result_status recheck_status,r.evidence_version recheck_evidence_version,
          r.applied_at recheck_applied_at,r.applied_by recheck_applied_by,
          d.status submission_status,d.remote_id,d.remote_url submission_url,d.remote_status,d.qc_summary,
          d.remote_updated_at,d.error submission_error,d.hidden_at,d.submitted_at"""
        from_sql = """FROM pairs p JOIN tasks t ON t.id=p.task_id
          LEFT JOIN git_repositories repo ON repo.pair_id=p.id
          LEFT JOIN arm_runs aa ON aa.pair_id=p.id AND aa.arm='A'
          LEFT JOIN arm_runs bb ON bb.pair_id=p.id AND bb.arm='B'
          LEFT JOIN artifact_checks ca ON ca.pair_id=p.id AND ca.arm='A' AND ca.commit_sha=aa.commit_sha
          LEFT JOIN artifact_checks cb ON cb.pair_id=p.id AND cb.arm='B' AND cb.commit_sha=bb.commit_sha
          LEFT JOIN recordings ra ON ra.pair_id=p.id AND ra.arm='A'
          LEFT JOIN recordings rb ON rb.pair_id=p.id AND rb.arm='B'
          LEFT JOIN gsb_reviews g ON g.pair_id=p.id
          LEFT JOIN difficulty_reviews dr ON dr.pair_id=p.id
          LEFT JOIN gsb_rechecks r ON r.id=(SELECT id FROM gsb_rechecks x WHERE x.pair_id=p.id ORDER BY x.created_at DESC LIMIT 1)
          LEFT JOIN delivery_submissions d ON d.pair_id=p.id"""
        return select, from_sql

    @staticmethod
    def _decorate_delivery(row: Dict[str, Any]) -> Dict[str, Any]:
        issues = []
        warnings = []
        for prefix in ("a", "b"):
            arm = prefix.upper()
            if not row.get(prefix + "_session_id"):
                issues.append(arm + " 缺少 SessionID")
            if not row.get(prefix + "_prompt_id"):
                issues.append(arm + " 缺少 PromptID")
            if not row.get(prefix + "_commit"):
                issues.append(arm + " 缺少最终提交")
            check_status = row.get(prefix + "_check_status")
            if check_status == "failed":
                warnings.append(arm + " Docker 验收失败，已作为最终 GSB 证据保留")
            elif check_status != "passed":
                issues.append(arm + " 缺少通过的 Docker 验收")
            if check_status == "passed":
                recording_status = row.get(prefix + "_recording_status")
                if recording_status != "passed":
                    issues.append(arm + (" 录像未通过" if recording_status else " 缺少合格录像"))
                else:
                    if int(row.get(prefix + "_recording_match") or 0) != 1:
                        issues.append(arm + " 录像与最终提交不匹配")
                    if row.get(prefix + "_recording_review_status") != "confirmed":
                        issues.append(arm + " 录像尚未确认")
        gsb_status = row.get("gsb_status")
        if gsb_status != "confirmed":
            issues.append("GSB 尚未确认" if gsb_status else "缺少 GSB")
        if row.get("recheck_status") == "fact_conflict" and not row.get("recheck_applied_at"):
            issues.append("复检发现公开理由存在事实冲突")
        if row.get("difficulty_review_status") and row.get("difficulty_review_status") != "passed":
            issues.append("实际难度复评未通过")
        row["readiness_issues"] = issues
        row["readiness_warnings"] = warnings
        row["readiness"] = "blocked" if issues else "ready"
        if row["readiness"] == "ready" and str(row.get("submission_status") or "not_submitted") == "not_submitted":
            row["submission_status"] = "ready_to_submit"
        return row

    def _deliveries_page(self, query: Dict[str, list]) -> Dict[str, Any]:
        clauses, params = ["(g.id IS NOT NULL OR p.completed_at IS NOT NULL)"], []
        ready_expression = """(
          COALESCE(aa.session_id,'')<>'' AND COALESCE(aa.prompt_id,'')<>'' AND COALESCE(aa.commit_sha,'')<>'' AND
          COALESCE(bb.session_id,'')<>'' AND COALESCE(bb.prompt_id,'')<>'' AND COALESCE(bb.commit_sha,'')<>'' AND
          ca.status='passed' AND cb.status='passed' AND
          ra.status='passed' AND rb.status='passed' AND
          COALESCE(ra.commit_match,0)=1 AND COALESCE(rb.commit_match,0)=1 AND
          ra.review_status='confirmed' AND rb.review_status='confirmed' AND
          g.status='confirmed' AND
          (dr.id IS NULL OR dr.status='passed') AND
          (COALESCE(r.result_status,'')<>'fact_conflict' OR r.applied_at IS NOT NULL)
        )"""
        if self._query(query, "include_hidden") != "1":
            clauses.append("d.hidden_at IS NULL")
        q = self._query(query, "q")
        if q:
            clauses.append("(p.id LIKE ? OR p.chain_id LIKE ? OR t.title LIKE ? OR t.prompt LIKE ?)")
            params += ["%" + q + "%"] * 4
        for key, column in (("task_type", "t.task_type"), ("project_category", "t.project_category"),
                            ("difficulty", "t.difficulty"), ("recheck_status", "r.result_status")):
            value = self._query(query, key)
            if value:
                clauses.append(column + "=?"); params.append(value)
        submission_status = self._query(query, "submission_status")
        if submission_status == "ready_to_submit":
            clauses.append(
                "(d.status='ready_to_submit' OR "
                "(COALESCE(d.status,'not_submitted')='not_submitted' AND " + ready_expression + "))"
            )
        elif submission_status:
            clauses.append("d.status=?"); params.append(submission_status)
        date_from, date_to = self._query(query, "date_from"), self._query(query, "date_to")
        if date_from: clauses.append("date(p.completed_at)>=date(?)"); params.append(date_from)
        if date_to: clauses.append("date(p.completed_at)<=date(?)"); params.append(date_to)
        readiness = self._query(query, "readiness")
        if readiness == "ready":
            clauses.append(ready_expression)
        elif readiness == "blocked":
            clauses.append("NOT " + ready_expression)
        select, from_sql = self._delivery_select()
        result = self._joined_page(select, from_sql, clauses, params, "COALESCE(p.completed_at,p.updated_at) DESC,p.id", query)
        result["items"] = [self._decorate_delivery(row) for row in result["items"]]
        return result

    def _delivery_rows_by_ids(self, pair_ids) -> list:
        select, from_sql = self._delivery_select()
        placeholders = ",".join("?" for _ in pair_ids)
        rows = self.app.db.all(select + " " + from_sql + " WHERE p.id IN (" + placeholders + ") ORDER BY p.completed_at,p.id", pair_ids)
        by_id = {row["pair_id"]: self._decorate_delivery(row) for row in rows}
        return [by_id[pair_id] for pair_id in pair_ids if pair_id in by_id]

    @staticmethod
    def _pair_ids(body: Dict[str, Any]) -> list:
        values = body.get("pairIds")
        if not isinstance(values, list) or not values:
            raise ValueError("请至少选择一个 Pair")
        result = []
        for value in values[:100]:
            pair_id = str(value or "").strip()
            if not re.fullmatch(r"pair-[a-zA-Z0-9]+", pair_id):
                raise ValueError("Pair ID 格式不正确")
            if pair_id not in result:
                result.append(pair_id)
        return result


def serve(config: Config, db: Database, service: PairwiseService) -> None:
    server = AppServer((config.host, config.port), Handler, config, db, service)
    print("%s running at http://%s:%s" % (APP_NAME, config.host, config.port), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
