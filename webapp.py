from __future__ import annotations

import asyncio
import mimetypes
import queue
import secrets
import threading
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from runtime import AppRuntime, file_preview_kind
from agents.store import AgentThreadActiveError
from security import VALID_PERMISSIONS


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SESSION_COOKIE = "lan_sync_session"
CSRF_HEADER = "x-csrf-token"


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}
        self._lock = threading.Lock()

    def create(self) -> tuple[str, str]:
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[session_id] = csrf_token
        return session_id, csrf_token

    def csrf_for(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        with self._lock:
            return self._sessions.get(session_id)


class LocalSecurityMiddleware:
    def __init__(self, app: ASGIApp, sessions: SessionStore) -> None:
        self.app = app
        self.sessions = sessions

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if path.startswith("/api/") and method not in SAFE_METHODS:
            cookies = SimpleCookie()
            cookies.load(headers.get("cookie", ""))
            session = cookies.get(SESSION_COOKIE)
            expected = self.sessions.csrf_for(
                session.value if session is not None else None
            )
            supplied = headers.get(CSRF_HEADER)
            if expected is None or not secrets.compare_digest(
                expected,
                supplied or "",
            ):
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF validation failed"},
                )
                await response(scope, receive, self._secure_send(send))
                return

        await self.app(scope, receive, self._secure_send(send))

    @staticmethod
    def _secure_send(send: Send):
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        return wrapped


def create_app(
    runtime: AppRuntime | None = None,
    *,
    manage_runtime: bool = True,
) -> FastAPI:
    runtime = runtime or AppRuntime()
    sessions = SessionStore()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if manage_runtime:
            runtime.start()
        try:
            yield
        finally:
            if manage_runtime:
                runtime.stop()

    app = FastAPI(
        title="LANSync Agent",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.runtime = runtime
    app.state.sessions = sessions
    app.add_middleware(LocalSecurityMiddleware, sessions=sessions)

    @app.get("/api/session")
    async def create_session(request: Request, response: Response):
        session_id = request.cookies.get(SESSION_COOKIE)
        csrf_token = sessions.csrf_for(session_id)
        if csrf_token is None:
            session_id, csrf_token = sessions.create()
            response.set_cookie(
                SESSION_COOKIE,
                session_id,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
        return {"csrf_token": csrf_token}

    @app.get("/api/runtime")
    async def get_runtime():
        return runtime.runtime_status()

    @app.get("/api/dashboard")
    async def get_dashboard():
        return runtime.dashboard()

    @app.get("/api/devices")
    async def get_devices():
        return {"items": runtime.devices_payload()}

    @app.post("/api/devices/{device_id}/pair")
    async def pair_device(device_id: str, body: dict[str, Any]):
        pair_code = str(body.get("pair_code", "")).strip()
        if len(pair_code) != 6 or not pair_code.isdigit():
            raise HTTPException(422, "配对码必须是六位数字。")
        try:
            return runtime.pair_device(device_id, pair_code)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.patch("/api/devices/{device_id}/permission")
    async def set_device_permission(device_id: str, body: dict[str, Any]):
        permission = str(body.get("permission", "")).lower()
        if permission not in VALID_PERMISSIONS:
            raise HTTPException(422, "权限值无效。")
        try:
            runtime.security_store.set_permission(device_id, permission)
        except KeyError as exc:
            raise HTTPException(404, "设备未授权。") from exc
        runtime.events.publish(
            "permission_changed",
            {"device_id": device_id, "permission": permission},
        )
        return {"status": "success"}

    @app.delete("/api/devices/{device_id}/authorization")
    async def revoke_device(device_id: str):
        if not runtime.security_store.revoke(device_id):
            raise HTTPException(404, "设备未授权。")
        runtime.events.publish("authorization_revoked", {"device_id": device_id})
        return {"status": "success"}

    @app.post("/api/devices/{device_id}/diagnose")
    async def diagnose_device(device_id: str):
        if runtime.find_online_device(device_id) is None:
            raise HTTPException(404, "设备不在线。")
        return runtime.run_agent("connection", device_id=device_id)

    @app.get("/api/files")
    async def get_files(
        limit: int = 100,
        offset: int = 0,
        query: str = "",
        status: str = "",
    ):
        return runtime.files_payload(
            limit=limit,
            offset=offset,
            query=query,
            status=status,
        )

    @app.get("/api/files/content")
    async def get_file_content(
        request: Request,
        path: str,
        disposition: Literal["inline", "attachment"] = "inline",
    ):
        session_id = request.cookies.get(SESSION_COOKIE)
        if sessions.csrf_for(session_id) is None:
            raise HTTPException(401, "需要有效的本地控制台会话。")
        try:
            entry, file_path = runtime.shared_file_path(path)
        except ValueError as exc:
            raise HTTPException(400, "文件路径无效。") from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, "文件不存在或不可访问。") from exc

        preview_kind = file_preview_kind(entry.file_name)
        response_disposition = (
            "inline"
            if disposition == "inline" and preview_kind is not None
            else "attachment"
        )
        guessed_type, _ = mimetypes.guess_type(entry.file_name)
        media_type = (
            "text/plain; charset=utf-8"
            if response_disposition == "inline" and preview_kind == "text"
            else guessed_type or "application/octet-stream"
        )
        return FileResponse(
            file_path,
            media_type=media_type,
            filename=entry.file_name,
            content_disposition_type=response_disposition,
        )

    @app.post("/api/sync/run")
    async def run_sync():
        if not runtime.config.sync_enabled:
            raise HTTPException(409, "自动同步当前未启用。")
        runtime.trigger_sync()
        return {"status": "queued"}

    @app.get("/api/transfers")
    async def get_transfers(limit: int = 100):
        return {"items": runtime.transfers.list_tasks(limit)}

    @app.post("/api/transfers/upload")
    async def upload_and_send(request: Request, device_id: str):
        raw_name = request.headers.get("x-file-name", "")
        file_name = Path(urllib.parse.unquote(raw_name)).name.strip()
        if not file_name or file_name in {".", ".."}:
            raise HTTPException(422, "缺少有效文件名。")
        upload_dir = runtime.config.shared_folder / ".lan-sync" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_path = upload_dir / f"{uuid.uuid4().hex}-{file_name}"
        try:
            with upload_path.open("wb") as output:
                async for chunk in request.stream():
                    output.write(chunk)
            return runtime.transfers.submit_manual_send(
                upload_path,
                file_name,
                device_id,
            )
        except KeyError as exc:
            upload_path.unlink(missing_ok=True)
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            upload_path.unlink(missing_ok=True)
            raise HTTPException(403, str(exc)) from exc
        except Exception:
            upload_path.unlink(missing_ok=True)
            raise

    @app.get("/api/conflicts")
    async def get_conflicts(resolved: str = "all"):
        resolved_filter: bool | None = None
        if resolved == "resolved":
            resolved_filter = True
        elif resolved == "unresolved":
            resolved_filter = False
        return {"items": runtime.conflicts_payload(resolved_filter)}

    @app.post("/api/conflicts/{conflict_id}/resolve")
    async def resolve_conflict(conflict_id: int, body: dict[str, Any]):
        if not runtime.resolve_conflict(conflict_id, str(body.get("note", ""))):
            raise HTTPException(404, "冲突不存在或已经处理。")
        return {"status": "success"}

    @app.post("/api/conflicts/{conflict_id}/analyze")
    async def analyze_conflict(conflict_id: int):
        if runtime.file_index.get_conflict(conflict_id) is None:
            raise HTTPException(404, "冲突不存在。")
        return runtime.run_agent("conflict", conflict_id=conflict_id)

    @app.get("/api/conflicts/{conflict_id}/file/{variant}")
    async def get_conflict_file(conflict_id: int, variant: str):
        if variant not in {"main", "copy"}:
            raise HTTPException(404, "文件版本无效。")
        try:
            path = runtime.conflict_path(conflict_id, variant)
        except KeyError as exc:
            raise HTTPException(404, "冲突不存在。") from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, "文件不存在。") from exc
        media_type, _ = mimetypes.guess_type(path.name)
        inline = bool(
            media_type
            and (
                media_type.startswith("text/")
                or media_type.startswith("image/")
                or media_type == "application/pdf"
            )
        )
        return FileResponse(
            path,
            media_type=media_type or "application/octet-stream",
            filename=path.name,
            content_disposition_type="inline" if inline else "attachment",
        )

    @app.get("/api/security/summary")
    async def security_summary():
        return runtime.run_agent("security", enhance=False)

    @app.get("/api/security/pair-code")
    async def get_pair_code():
        return {"pair_code": runtime.security_store.pair_code}

    @app.post("/api/security/pair-code/regenerate")
    async def regenerate_pair_code():
        pair_code = runtime.security_store.regenerate_pair_code()
        runtime.events.publish("pair_code_regenerated", {})
        return {"pair_code": pair_code}

    @app.get("/api/security/alerts")
    async def get_alerts(limit: int = 100, unread_only: bool = False):
        return {
            "items": [
                vars(alert)
                for alert in runtime.audit_store.recent_alerts(
                    limit,
                    unread_only=unread_only,
                )
            ]
        }

    @app.post("/api/security/alerts/{alert_id}/read")
    async def mark_alert_read(alert_id: int):
        if not runtime.audit_store.mark_alert_read(alert_id):
            raise HTTPException(404, "告警不存在。")
        runtime.events.publish("alert_read", {"alert_id": alert_id})
        return {"status": "success"}

    @app.post("/api/security/audit")
    async def run_security_audit():
        return runtime.run_agent("security")

    @app.post("/api/agent/runs")
    async def create_agent_run(body: dict[str, Any]):
        try:
            return runtime.react_agent.create_run(
                str(body.get("message", "")),
                str(body.get("thread_id", "")),
            )
        except KeyError as exc:
            raise HTTPException(404, "Agent 会话不存在。") from exc
        except AgentThreadActiveError as exc:
            raise HTTPException(409, "当前会话仍有任务正在处理。") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/agent/runs/{run_id}")
    async def get_agent_run(run_id: str):
        payload = runtime.react_agent.get_run(run_id)
        if payload is None:
            raise HTTPException(404, "Agent 任务不存在。")
        return payload

    @app.post("/api/agent/runs/{run_id}/decision")
    async def decide_agent_run(run_id: str, body: dict[str, Any]):
        approved = body.get("approved")
        device_id = str(body.get("device_id", "")).strip()
        if approved is not None and type(approved) is not bool:
            raise HTTPException(422, "approved 必须是布尔值。")
        if approved is None and not device_id:
            raise HTTPException(422, "需要 approved 或 device_id。")
        try:
            return runtime.react_agent.decide(
                run_id,
                approved=approved,
                device_id=device_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "Agent 任务不存在。") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/agent/threads")
    async def get_agent_threads(limit: int = 50, cursor: str = ""):
        try:
            return runtime.react_agent.list_threads(limit, cursor)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/agent/threads/{thread_id}")
    async def get_agent_thread(
        thread_id: str,
        limit: int = 50,
        cursor: str = "",
    ):
        try:
            payload = runtime.react_agent.get_thread(
                thread_id,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if payload is None:
            raise HTTPException(404, "Agent 会话不存在。")
        return payload

    @app.patch("/api/agent/threads/{thread_id}")
    async def rename_agent_thread(thread_id: str, body: dict[str, Any]):
        try:
            return runtime.react_agent.rename_thread(
                thread_id,
                str(body.get("title", "")),
            )
        except KeyError as exc:
            raise HTTPException(404, "Agent 会话不存在。") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/agent/threads/{thread_id}")
    async def delete_agent_thread(thread_id: str):
        try:
            await runtime.react_agent.delete_thread(thread_id)
        except KeyError as exc:
            raise HTTPException(404, "Agent 会话不存在。") from exc
        except AgentThreadActiveError as exc:
            raise HTTPException(409, "活动中的 Agent 会话不能删除。") from exc
        return {"status": "success", "thread_id": thread_id}

    @app.post("/api/agents/{agent_name}")
    async def run_agent(agent_name: str, body: dict[str, Any] | None = None):
        body = body or {}
        try:
            return runtime.run_agent(
                agent_name,
                device_id=str(body.get("device_id", "")),
                conflict_id=(
                    int(body["conflict_id"])
                    if body.get("conflict_id") is not None
                    else None
                ),
                enhance=bool(body.get("enhance", True)),
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/logs")
    async def get_logs(
        limit: int = 50,
        offset: int = 0,
        device_id: str = "",
        event_type: str = "",
        severity: str = "",
        outcome: str = "",
        started_at_ns: int | None = None,
        ended_at_ns: int | None = None,
    ):
        items, total = runtime.audit_store.query_events(
            limit=limit,
            offset=offset,
            device_id=device_id,
            event_type=event_type,
            severity=severity,
            outcome=outcome,
            started_at_ns=started_at_ns,
            ended_at_ns=ended_at_ns,
        )
        return {"items": [vars(item) for item in items], "total": total}

    @app.get("/api/settings")
    async def get_settings():
        return runtime.settings_payload()

    @app.patch("/api/settings")
    async def update_settings(body: dict[str, Any]):
        try:
            return runtime.update_settings(body)
        except (TypeError, ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.websocket("/api/events")
    async def events_socket(websocket: WebSocket):
        session_id = websocket.cookies.get(SESSION_COOKIE)
        if sessions.csrf_for(session_id) is None:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        subscriber = runtime.events.subscribe()
        try:
            await websocket.send_json(
                {
                    "type": "connected",
                    "created_at_ns": 0,
                    "payload": runtime.runtime_status(),
                }
            )
            while True:
                try:
                    event = await asyncio.to_thread(
                        subscriber.get,
                        True,
                        15,
                    )
                    await websocket.send_json(event)
                except queue.Empty:
                    await websocket.send_json(
                        {
                            "type": "heartbeat",
                            "created_at_ns": 0,
                            "payload": {},
                        }
                    )
        except Exception:
            pass
        finally:
            runtime.events.unsubscribe(subscriber)

    frontend_dist = Path(__file__).parent / "frontend" / "dist"
    assets = frontend_dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}")
    async def frontend(path: str):
        root = frontend_dist.resolve()
        requested = (frontend_dist / path).resolve()
        if path and requested.is_file() and requested.is_relative_to(root):
            return FileResponse(requested)
        index = frontend_dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "前端尚未构建，请运行 cd frontend && npm ci && npm run build"
            },
        )

    return app
