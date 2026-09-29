import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pairwise_console.recording import RecordingManager
from pairwise_console.recording_similarity import similar_recording_pairs


def row(arm, *, duration=5.12, path="/v1/profile", clicks=None, sha=""):
    event = {"event": "interaction", "clicks": clicks} if clicks is not None else {
        "event": "interaction", "method": "get", "path": path,
    }
    return {
        "pair_id": "pair-similar", "arm": arm, "status": "passed", "commit_match": 1,
        "sha256": sha or arm.lower() * 64, "duration_seconds": duration,
        "steps_json": json.dumps([{"method": "get", "path": path, "body": None}]) if clicks is None else "[]",
        "detail_json": json.dumps({"recorderEvents": [event]}),
    }


class RecordingSimilarityTests(unittest.TestCase):
    def test_matching_workflows_are_searchable_but_duration_alone_is_not(self):
        self.assertIn("pair-similar", similar_recording_pairs([row("A"), row("B")]))
        self.assertNotIn("pair-similar", similar_recording_pairs([
            row("A"), row("B", path="/v1/other"),
        ]))
        self.assertNotIn("pair-similar", similar_recording_pairs([
            row("A"), row("B", duration=14),
        ]))

    def test_frontend_clicks_and_identical_files_are_detected(self):
        a, b = row("A", clicks=["运行", "结果"]), row("B", clicks=["运行", "结果"])
        self.assertIn("pair-similar", similar_recording_pairs([a, b]))
        b["detail_json"] = json.dumps({"recorderEvents": [{"event": "interaction", "clicks": ["运行", "详情"]}]})
        self.assertNotIn("pair-similar", similar_recording_pairs([a, b]))
        b["sha256"] = a["sha256"]
        self.assertIn("pair-similar", similar_recording_pairs([a, b]))

    def test_second_backend_operation_requires_documentation_and_successful_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            (workspace / "README.md").write_text(
                "GET /healthz\nGET /v1/profile\nGET /v1/summary\n", encoding="utf-8",
            )
            primary = {"method": "get", "path": "/v1/profile", "body": None}
            response = MagicMock(status=200)
            response.__enter__.return_value = response
            with patch("pairwise_console.recording.urlopen", return_value=response) as request:
                alternative = RecordingManager._discover_alternative_read_only_demo(
                    workspace, primary, "http://127.0.0.1:8000/healthz",
                )
            self.assertEqual(alternative["path"], "/v1/summary")
            self.assertEqual(request.call_args.args[0].full_url, "http://127.0.0.1:8000/v1/summary")
            with patch("pairwise_console.recording.urlopen", side_effect=OSError("offline")):
                self.assertIsNone(RecordingManager._discover_alternative_read_only_demo(
                    workspace, primary, "http://127.0.0.1:8000/healthz",
                ))

    def test_recorder_rejects_wrong_commit_missing_events_and_identical_video(self):
        manager = object.__new__(RecordingManager)
        manager.db = MagicMock()
        attempt = {"pair_id": "pair-similar", "arm": "A", "commit_sha": "abc", "path": "/tmp/a.mp4", "interaction_mode": "auto"}
        manager.db.one.side_effect = [attempt, {"commit_sha": "different"}]
        events = [{"event": "interaction", "ok": True}, {"event": "finished"}]
        self.assertIn("提交", manager._recording_integrity_error("attempt-a", {"sha256": "a"}, events))

        manager.db.one.side_effect = [attempt, {"commit_sha": "abc"}]
        self.assertIn("完成事件", manager._recording_integrity_error("attempt-a", {"sha256": "a"}, []))

        manager.db.one.side_effect = [attempt, {"commit_sha": "abc"}, {"path": "/tmp/b.mp4", "sha256": "a"}]
        self.assertIn("完全相同", manager._recording_integrity_error("attempt-a", {"sha256": "a"}, events))


if __name__ == "__main__":
    unittest.main()
