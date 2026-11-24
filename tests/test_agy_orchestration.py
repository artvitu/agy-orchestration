#!/usr/bin/env python3
"""Автономный набор тестов для публичного плагина `agy-orchestration` (mock LanguageServerService)."""
from __future__ import annotations

from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "skills/agy-orchestration/scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import model_context as mc  # noqa: E402

_WAKE_SPEC = importlib.util.spec_from_file_location("agy_wake", SCRIPTS_DIR / "agy-wake.py")
agy_wake = importlib.util.module_from_spec(_WAKE_SPEC)
assert _WAKE_SPEC and _WAKE_SPEC.loader
_WAKE_SPEC.loader.exec_module(agy_wake)


class _FakeLSState:
    def __init__(self) -> None:
        self.csrf = "test-csrf-token"
        self.quota_frac = 0.75
        self.calls: list[tuple[str, dict]] = []
        self.projects = [
            {"id": "proj-demo", "name": "demo-app"},
            {"id": "outside-of-project", "name": "Outside of Project"},
        ]
        self.trajectories = {
            "11111111-1111-1111-1111-111111111111": {
                "summary": "Backend API",
                "stepCount": 4,
                "status": "CASCADE_RUN_STATUS_IDLE",
                "lastModifiedTime": "2026-11-24T12:00:00Z",
                "annotations": {"title": "Backend API"},
                "trajectoryMetadata": {"projectId": "proj-demo"},
            },
            "22222222-2222-2222-2222-222222222222": {
                "summary": "[qa] Тесты",
                "stepCount": 5,
                "status": "CASCADE_RUN_STATUS_RUNNING",
                "lastModifiedTime": "2026-11-24T12:05:00Z",
                "annotations": {"title": "[qa] Тесты"},
                "trajectoryMetadata": {"projectId": "proj-demo"},
            },
            "44444444-4444-4444-4444-444444444444": {
                "summary": "",
                "stepCount": 2,
                "status": "CASCADE_RUN_STATUS_IDLE",
                "lastModifiedTime": "2026-11-24T12:06:00Z",
                "trajectoryMetadata": {
                    "projectId": "proj-demo",
                    "parentConversationId": "11111111-1111-1111-1111-111111111111",
                },
            },
        }
        self.steps = {
            "11111111-1111-1111-1111-111111111111": [
                {"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "status": "CORTEX_STEP_STATUS_DONE", "plannerResponse": {"response": "OK"}},
            ]
        }


def _make_handler(state: _FakeLSState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_POST(self) -> None:
            if self.headers.get("x-codeium-csrf-token") != state.csrf:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"code":"unauthenticated"}')
                return
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            method = self.path.rsplit("/", 1)[-1]
            state.calls.append((method, payload))

            code = 200
            resp: dict = {}
            if method == "RetrieveUserQuotaSummary":
                resp = {
                    "response": {
                        "groups": [
                            {
                                "displayName": "Gemini Models",
                                "buckets": [
                                    {"bucketId": "gemini-weekly", "remainingFraction": state.quota_frac, "resetTime": "2026-12-01T00:00:00Z"},
                                    {"bucketId": "gemini-5h", "remainingFraction": 0.90, "resetTime": "2026-11-24T18:00:00Z"},
                                ],
                            }
                        ]
                    }
                }
            elif method == "ListProjects":
                resp = {"projectIds": [p["id"] for p in state.projects]}
            elif method == "ReadProjects":
                resp = {"projects": state.projects}
            elif method == "GetAllCascadeTrajectories":
                resp = {"trajectorySummaries": state.trajectories}
            elif method == "GetUserStatus":
                resp = {
                    "userStatus": {
                        "cascadeModelConfigData": {
                            "clientModelConfigs": [
                                {"label": "Gemini 4 Hydrogen (Low)", "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M527"}},
                            ]
                        }
                    }
                }
            elif method == "GetCascadeTrajectoryGeneratorMetadata":
                resp = {"generatorMetadata": [{"stepIndices": [1], "chatModel": {"model": "MODEL_PLACEHOLDER_M527"}}]}
            elif method == "GetCascadeTrajectory":
                cid = payload.get("cascadeId", "")
                resp = {"trajectory": {"steps": state.steps.get(cid, [])}}
            elif method == "StartCascade":
                if payload.get("workspaceUris") and payload.get("projectEnvConfig"):
                    code = 400
                    resp = {"code": "invalid_argument", "message": "cannot specify both workspace URIs and project environment config"}
                else:
                    cid = "33333333-3333-3333-3333-333333333333"
                    pid = (payload.get("projectEnvConfig") or {}).get("projectId", "")
                    state.trajectories[cid] = {
                        "summary": "",
                        "stepCount": 0,
                        "status": "CASCADE_RUN_STATUS_IDLE",
                        "lastModifiedTime": "2026-11-24T12:10:00Z",
                        "trajectoryMetadata": {"projectId": pid} if pid else {},
                    }
                    state.steps[cid] = []
                    resp = {"cascadeId": cid}
            elif method == "UpdateConversationAnnotations":
                cid = payload["cascadeId"]
                cur = dict(state.trajectories[cid].get("annotations") or {})
                cur.update(payload.get("annotations") or {})
                state.trajectories[cid]["annotations"] = cur
            elif method == "SendUserCascadeMessage":
                cid = payload["cascadeId"]
                state.trajectories[cid]["stepCount"] += 2
                state.trajectories[cid]["status"] = "CASCADE_RUN_STATUS_IDLE"
                state.steps.setdefault(cid, []).append(
                    {"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "status": "CORTEX_STEP_STATUS_DONE", "plannerResponse": {"response": "DONE"}}
                )
            elif method == "DeleteCascadeTrajectory":
                state.trajectories.pop(payload["cascadeId"], None)

            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))

    return Handler


class TestAgyOrchestrationPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.state = _FakeLSState()
        self.server = HTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.daemon_dir = Path(self.tmp.name) / "daemon"
        self.daemon_dir.mkdir(parents=True, exist_ok=True)
        (self.daemon_dir / f"hub_{self.port}.json").write_text(
            json.dumps({"httpPort": self.port, "csrfToken": self.state.csrf}),
            encoding="utf-8",
        )
        self.state_dir = Path(self.tmp.name) / "state"
        self.reg_path = Path(self.tmp.name) / "sessions.json"
        self.brain_dir = Path(self.tmp.name) / "brain"
        self._old_env = {k: os.environ.get(k) for k in (
            "ANTIGRAVITY_DAEMON_DIR", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN",
            "AGY_SESSIONS_FILE", "AGY_WAKE_STATE_DIR", "AGY_BRAIN_DIR", "WORKSPACE_ROOT",
        )}
        os.environ["ANTIGRAVITY_DAEMON_DIR"] = str(self.daemon_dir)
        os.environ["AGY_SESSIONS_FILE"] = str(self.reg_path)
        os.environ["AGY_WAKE_STATE_DIR"] = str(self.state_dir)
        os.environ["AGY_BRAIN_DIR"] = str(self.brain_dir)
        os.environ["WORKSPACE_ROOT"] = str(Path(self.tmp.name) / "demo-app")
        os.environ.pop("ANTIGRAVITY_LS_ADDRESS", None)
        os.environ.pop("ANTIGRAVITY_CSRF_TOKEN", None)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_plugin_manifest_structure(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "agy-orchestration")
        self.assertEqual(len(manifest["suggestedPrompts"]), 3)
        self.assertTrue((PLUGIN_ROOT / manifest["logo"]).is_file())
        self.assertTrue((PLUGIN_ROOT / "LICENSE").is_file())
        self.assertTrue((PLUGIN_ROOT / "rules/AGENTS.md").is_file())
        self.assertTrue((PLUGIN_ROOT / "skills/agy-orchestration/SKILL.md").is_file())

    def test_discover_send_queue_spawn_and_delete(self) -> None:
        self.assertEqual(agy_wake.main(["discover", "--from-titles", "--sync", "--yes"]), 0)
        reg = json.loads(self.reg_path.read_text(encoding="utf-8"))
        self.assertIn("backend-api", reg["roles"])
        self.assertIn("qa", reg["roles"])

        body_f = Path(self.tmp.name) / "msg.md"
        body_f.write_text("Проверь PR #42", encoding="utf-8")
        self.assertEqual(agy_wake.main(["send", "--to", "qa", "--from", "backend-api", "--queue", "--body-file", str(body_f), "--yes"]), 0)
        send_calls = [p for m, p in self.state.calls if m == "SendUserCascadeMessage"]
        self.assertEqual(send_calls[-1].get("deliveryStrategy"), "MESSAGE_DELIVERY_STRATEGY_WHEN_IDLE")
        self.assertNotIn("cascadeConfig", send_calls[-1])

        self.assertEqual(agy_wake.main(["spawn", "--title", "[review] Код-ревью", "--slug", "review", "--preset", "turbo", "--text", "Готово", "--yes"]), 0)
        self.assertEqual(agy_wake.main(["delete", "--to", "33333333-3333-3333-3333-333333333333", "--yes"]), 0)

    def test_quota_and_sandbox_guards(self) -> None:
        self.state.quota_frac = 0.05
        self.assertEqual(agy_wake.main(["send", "--to", "11111111-1111-1111-1111-111111111111", "--text", "hi", "--yes"]), 2)
        with patch.object(mc, "post_ls_rpc_strict", return_value=(0, "<urlopen error [Errno 1] Operation not permitted>")):
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = agy_wake.main(["list"])
            self.assertEqual(rc, 2)
            self.assertIn("TURBO", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
