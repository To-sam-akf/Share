from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from unittest import mock
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from runtime import AppRuntime, EventBus
from webapp import CSRF_HEADER, create_app


async def run_sync_inline_for_test(
    func,
    *args,
    **_kwargs,
):
    return func(*args)


def write_config(root: Path) -> Path:
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "device_name": "Test Node",
                "udp_port": 0,
                "tcp_port": 0,
                "broadcast_ip": "127.0.0.1",
                "shared_folder": "./shared",
                "chunk_size": 4096,
                "enable_tls": False,
                "sync_enabled": False,
                "sync_interval_seconds": 10,
                "security_audit_interval_seconds": 600,
                "audit_log_retention_days": 30,
                "shared_size_risk_bytes": 1024,
                "shared_file_count_risk": 100,
                "web_port": 8765,
            }
        ),
        encoding="utf-8",
    )
    return config_path


@dataclass
class ASGIResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


async def asgi_request(
    app,
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> ASGIResponse:
    request_messages = [
        {"type": "http.request", "body": body, "more_body": False}
    ]
    response_messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if request_messages:
            return request_messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        response_messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": urlencode(query or {}).encode("ascii"),
        "root_path": "",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8765),
    }
    await app(scope, receive, send)
    start = next(
        item
        for item in response_messages
        if item["type"] == "http.response.start"
    )
    response_headers = {
        key.decode("latin-1"): value.decode("latin-1")
        for key, value in start["headers"]
    }
    response_body = b"".join(
        item.get("body", b"")
        for item in response_messages
        if item["type"] == "http.response.body"
    )
    return ASGIResponse(
        status_code=int(start["status"]),
        headers=response_headers,
        body=response_body,
    )


class WebConsoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = AppRuntime(write_config(self.root))
        self.app = create_app(self.runtime, manage_runtime=False)
        session = self.request("GET", "/api/session")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.headers["cache-control"], "no-store")
        self.assertEqual(session.headers["x-frame-options"], "DENY")
        self.csrf = session.json()["csrf_token"]
        cookie = SimpleCookie()
        cookie.load(session.headers["set-cookie"])
        self.session_cookie = (
            f"lan_sync_session={cookie['lan_sync_session'].value}"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> ASGIResponse:
        request_headers = dict(headers or {})
        if hasattr(self, "session_cookie"):
            request_headers.setdefault("Cookie", self.session_cookie)
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        return asyncio.run(
            asgi_request(
                self.app,
                method,
                path,
                query=query,
                headers=request_headers,
                body=body,
            )
        )

    def test_csrf_and_settings_restart_marker(self) -> None:
        rejected = self.request(
            "PATCH",
            "/api/settings",
            json_body={"sync_interval_seconds": 12},
        )
        self.assertEqual(rejected.status_code, 403)

        immediate = self.request(
            "PATCH",
            "/api/settings",
            headers={CSRF_HEADER: self.csrf},
            json_body={"sync_interval_seconds": 12},
        )
        self.assertEqual(immediate.status_code, 200)
        self.assertFalse(immediate.json()["pending_restart"])
        self.assertEqual(immediate.json()["active"]["sync_interval_seconds"], 12)

        restart = self.request(
            "PATCH",
            "/api/settings",
            headers={CSRF_HEADER: self.csrf},
            json_body={"device_name": "Renamed Node"},
        )
        self.assertEqual(restart.status_code, 200)
        self.assertTrue(restart.json()["pending_restart"])
        self.assertEqual(
            restart.json()["configured"]["device_name"],
            "Renamed Node",
        )
        self.assertEqual(
            restart.json()["active"]["device_name"],
            "Test Node",
        )

    def test_failed_upload_is_cleaned_up(self) -> None:
        response = self.request(
            "POST",
            "/api/transfers/upload",
            query={"device_id": "missing"},
            headers={
                CSRF_HEADER: self.csrf,
                "X-File-Name": "example.txt",
                "Content-Type": "application/octet-stream",
            },
            body=b"payload",
        )
        self.assertEqual(response.status_code, 404)
        upload_dir = (
            self.runtime.config.shared_folder / ".lan-sync" / "uploads"
        )
        self.assertEqual(list(upload_dir.iterdir()), [])

    def test_shared_files_can_be_previewed_and_downloaded(self) -> None:
        shared = self.runtime.config.shared_folder
        folder = shared / "收到的文件"
        folder.mkdir(parents=True)
        samples = {
            "报告 2026.pdf": (b"%PDF-1.7\nsample", "pdf", "inline"),
            "photo.png": (b"\x89PNG\r\n\x1a\n", "image", "inline"),
            "notes.txt": ("共享文本".encode(), "text", "inline"),
            "page.html": (b"<script>alert(1)</script>", None, "attachment"),
            "vector.svg": (b"<svg xmlns='http://www.w3.org/2000/svg'/>", None, "attachment"),
        }
        for name, (content, _, _) in samples.items():
            (folder / name).write_bytes(content)

        payload = self.request("GET", "/api/files").json()
        by_name = {item["file_name"]: item for item in payload["items"]}
        for name, (_, preview_kind, _) in samples.items():
            self.assertEqual(by_name[name]["preview_kind"], preview_kind)

        with mock.patch(
            "anyio.to_thread.run_sync",
            new=run_sync_inline_for_test,
        ):
            for name, (content, _, expected_disposition) in samples.items():
                with self.subTest(name=name):
                    response = self.request(
                        "GET",
                        "/api/files/content",
                        query={
                            "path": f"收到的文件/{name}",
                            "disposition": "inline",
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.body, content)
                    self.assertIn(
                        expected_disposition,
                        response.headers["content-disposition"],
                    )

            download = self.request(
                "GET",
                "/api/files/content",
                query={
                    "path": "收到的文件/报告 2026.pdf",
                    "disposition": "attachment",
                },
            )
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["content-disposition"])

    def test_shared_file_content_requires_session_and_safe_active_path(self) -> None:
        shared = self.runtime.config.shared_folder
        shared.mkdir(parents=True, exist_ok=True)
        active = shared / "active.txt"
        active.write_text("0123456789", encoding="utf-8")
        self.runtime.file_index.scan()

        unauthenticated = asyncio.run(
            asgi_request(
                self.app,
                "GET",
                "/api/files/content",
                query={"path": "active.txt"},
            )
        )
        self.assertEqual(unauthenticated.status_code, 401)

        for invalid_path in (
            "../active.txt",
            "/etc/passwd",
            ".lan-sync/index.sqlite3",
        ):
            with self.subTest(path=invalid_path):
                response = self.request(
                    "GET",
                    "/api/files/content",
                    query={"path": invalid_path},
                )
                self.assertEqual(response.status_code, 400)

        invalid_disposition = self.request(
            "GET",
            "/api/files/content",
            query={"path": "active.txt", "disposition": "open"},
        )
        self.assertEqual(invalid_disposition.status_code, 422)

        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = shared / "link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pass
        else:
            linked = self.request(
                "GET",
                "/api/files/content",
                query={"path": "link.txt"},
            )
            self.assertEqual(linked.status_code, 404)

        active.unlink()
        deleted = self.request(
            "GET",
            "/api/files/content",
            query={"path": "active.txt"},
        )
        self.assertEqual(deleted.status_code, 404)
        entry = self.runtime.file_index.get("active.txt")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.status, "deleted")

    def test_shared_file_content_supports_range_requests(self) -> None:
        shared = self.runtime.config.shared_folder
        shared.mkdir(parents=True, exist_ok=True)
        (shared / "large.bin").write_bytes(b"0123456789")
        self.runtime.file_index.scan()

        with mock.patch(
            "anyio.to_thread.run_sync",
            new=run_sync_inline_for_test,
        ):
            response = self.request(
                "GET",
                "/api/files/content",
                query={"path": "large.bin", "disposition": "attachment"},
                headers={"Range": "bytes=2-5"},
            )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.body, b"2345")
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")

    def test_transfer_observer(self) -> None:
        common = {
            "kind": "sync",
            "device_id": "peer",
            "relative_path": "docs/report.txt",
            "total_bytes": 10,
        }
        self.runtime.transfers.observe_event("transfer_queued", common)
        self.runtime.transfers.observe_event(
            "transfer_progress",
            {**common, "transferred_bytes": 5},
        )
        self.runtime.transfers.observe_event("transfer_completed", common)
        task = self.runtime.transfers.list_tasks(1)[0]
        self.assertEqual(task["status"], "success")
        self.assertEqual(task["progress"], 100.0)

    def test_agent_run_api_validates_requests_and_missing_runs(self) -> None:
        empty = self.request(
            "POST",
            "/api/agent/runs",
            headers={CSRF_HEADER: self.csrf},
            json_body={"message": ""},
        )
        self.assertEqual(empty.status_code, 422)

        missing = self.request("GET", "/api/agent/runs/missing")
        self.assertEqual(missing.status_code, 404)

        invalid_decision = self.request(
            "POST",
            "/api/agent/runs/missing/decision",
            headers={CSRF_HEADER: self.csrf},
            json_body={"approved": "yes"},
        )
        self.assertEqual(invalid_decision.status_code, 422)

    def test_agent_thread_api_lists_reads_renames_and_deletes(self) -> None:
        store = self.runtime.react_agent.store
        store.create_run("run-api", "thread-api", "Initial title")
        store.update_run("run-api", status="completed", report="Done")

        listed = self.request("GET", "/api/agent/threads", query={"limit": 10})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["items"][0]["thread_id"], "thread-api")

        detail = self.request("GET", "/api/agent/threads/thread-api")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.json()["messages"]), 2)

        renamed = self.request(
            "PATCH",
            "/api/agent/threads/thread-api",
            headers={CSRF_HEADER: self.csrf},
            json_body={"title": "Renamed"},
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["title"], "Renamed")

        deleted = self.request(
            "DELETE",
            "/api/agent/threads/thread-api",
            headers={CSRF_HEADER: self.csrf},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(
            self.request("GET", "/api/agent/threads/thread-api").status_code,
            404,
        )

    def test_agent_thread_api_rejects_active_and_unknown_sessions(self) -> None:
        store = self.runtime.react_agent.store
        store.create_run("run-active", "thread-active", "Active")

        conflict = self.request(
            "POST",
            "/api/agent/runs",
            headers={CSRF_HEADER: self.csrf},
            json_body={"message": "Second", "thread_id": "thread-active"},
        )
        self.assertEqual(conflict.status_code, 409)
        missing = self.request(
            "POST",
            "/api/agent/runs",
            headers={CSRF_HEADER: self.csrf},
            json_body={"message": "Missing", "thread_id": "missing"},
        )
        self.assertEqual(missing.status_code, 404)
        blocked_delete = self.request(
            "DELETE",
            "/api/agent/threads/thread-active",
            headers={CSRF_HEADER: self.csrf},
        )
        self.assertEqual(blocked_delete.status_code, 409)

    def test_log_filters_accept_time_range(self) -> None:
        first = self.runtime.audit_store.record_event(
            "pairing_succeeded",
            device_id="peer-a",
        )
        second = self.runtime.audit_store.record_event(
            "file_sent",
            device_id="peer-b",
        )
        response = self.request(
            "GET",
            "/api/logs",
            query={
                "device_id": "peer-b",
                "started_at_ns": first.created_at_ns + 1,
                "ended_at_ns": second.created_at_ns,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["items"][0]["event_type"], "file_sent")

    def test_alert_can_be_marked_read_by_id(self) -> None:
        self.runtime.audit_store._create_alert(
            "TEST_ALERT",
            "127.0.0.1",
            "测试告警",
            1,
        )
        alert = self.runtime.audit_store.recent_alerts(1)[0]
        response = self.request(
            "POST",
            f"/api/security/alerts/{alert.alert_id}/read",
            headers={CSRF_HEADER: self.csrf},
        )
        self.assertEqual(response.status_code, 200)
        refreshed = self.runtime.audit_store.recent_alerts(1)[0]
        self.assertTrue(refreshed.read)


class EventBusTests(unittest.TestCase):
    def test_event_bus_delivers_to_subscribers(self) -> None:
        bus = EventBus()
        subscriber = bus.subscribe()
        bus.publish("runtime_started", {"running": True})
        self.assertEqual(subscriber.get_nowait()["payload"], {"running": True})
        bus.unsubscribe(subscriber)


if __name__ == "__main__":
    unittest.main()
