from __future__ import annotations

import json
import mimetypes
import os
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from agents import (
    AgentResult,
    AgentModelClient,
    ConflictAnalysisAgent,
    ConnectionDiagnosticAgent,
    ReActSyncAgent,
    SecurityAuditAgent,
    SecurityAuditService,
)
from audit import AuditStore
from config import AppConfig, load_config
from discovery import DiscoveryService, make_device_id
from discovery.udp_discovery import get_local_ip
from security import TLSPolicy, SecurityStore, ensure_tls_identity
from security.pairing import pair_with_device
from sync import STATUS_ACTIVE, STATUS_DELETED, ConflictRecord, FileEntry, FileIndex
from sync.paths import destination_for, normalize_relative_path
from sync.service import SyncService
from transfer import TCPFileServer, TransferError, send_file


INLINE_IMAGE_TYPES = {
    "image/avif",
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
UNSAFE_INLINE_TEXT_TYPES = {
    "application/xhtml+xml",
    "application/xml",
    "text/html",
    "text/xml",
}


def file_preview_kind(file_name: str) -> str | None:
    media_type, _ = mimetypes.guess_type(file_name)
    if media_type == "application/pdf":
        return "pdf"
    if media_type in INLINE_IMAGE_TYPES:
        return "image"
    if media_type == "application/json":
        return "text"
    if (
        media_type
        and media_type.startswith("text/")
        and media_type not in UNSAFE_INLINE_TEXT_TYPES
    ):
        return "text"
    return None


RESTART_FIELDS = {
    "device_name",
    "udp_port",
    "tcp_port",
    "broadcast_ip",
    "shared_folder",
    "chunk_size",
    "enable_tls",
    "web_port",
}
IMMEDIATE_FIELDS = {
    "sync_enabled",
    "sync_interval_seconds",
    "agent_api_url",
    "agent_provider",
    "agent_model",
    "agent_timeout_seconds",
    "security_audit_interval_seconds",
    "audit_log_retention_days",
    "shared_size_risk_bytes",
    "shared_file_count_risk",
}


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        event = {
            "type": str(event_type),
            "created_at_ns": time.time_ns(),
            "payload": payload or {},
        }
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)


@dataclass
class TransferTask:
    task_id: str
    kind: str
    status: str
    file_name: str
    device_id: str
    device_name: str
    total_bytes: int
    transferred_bytes: int
    created_at_ns: int
    updated_at_ns: int
    error_code: str = ""
    error_message: str = ""
    result: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["progress"] = (
            100.0
            if self.total_bytes == 0 and self.status == "success"
            else (
                round(self.transferred_bytes * 100 / self.total_bytes, 2)
                if self.total_bytes
                else 0.0
            )
        )
        return payload


class TransferTaskManager:
    def __init__(self, runtime: "AppRuntime") -> None:
        self.runtime = runtime
        self._tasks: dict[str, TransferTask] = {}
        self._observed_tasks: dict[tuple[str, str, str], str] = {}
        self._lock = threading.RLock()

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            tasks = sorted(
                self._tasks.values(),
                key=lambda item: item.created_at_ns,
                reverse=True,
            )
        return [task.to_payload() for task in tasks[: max(1, min(limit, 200))]]

    def submit_manual_send(
        self,
        upload_path: Path,
        original_name: str,
        device_id: str,
    ) -> dict[str, Any]:
        device = self.runtime.find_online_device(device_id)
        if device is None:
            raise KeyError("device is not online")
        credential = self.runtime.security_store.credential_for(
            device.device_id,
            self.runtime.device_id,
        )
        if credential is None:
            raise PermissionError("device is not paired or is blocked")
        now = time.time_ns()
        task = TransferTask(
            task_id=uuid.uuid4().hex,
            kind="manual",
            status="queued",
            file_name=original_name,
            device_id=device.device_id,
            device_name=device.device_name,
            total_bytes=upload_path.stat().st_size,
            transferred_bytes=0,
            created_at_ns=now,
            updated_at_ns=now,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        self._publish(task)
        threading.Thread(
            target=self._run_manual_send,
            args=(task.task_id, upload_path),
            name=f"web-transfer-{task.task_id[:8]}",
            daemon=True,
        ).start()
        return task.to_payload()

    def _run_manual_send(self, task_id: str, upload_path: Path) -> None:
        try:
            task = self._update(task_id, status="running")
            device = self.runtime.find_online_device(task.device_id)
            if device is None:
                raise TransferError("DEVICE_OFFLINE", "目标设备已离线。")
            credential = self.runtime.security_store.credential_for(
                device.device_id,
                self.runtime.device_id,
            )
            if credential is None:
                raise TransferError("AUTH_REQUIRED", "设备尚未配对或已被阻止。")
            authorized = self.runtime.security_store.get_device(device.device_id)
            policy = TLSPolicy(
                enabled=self.runtime.config.enable_tls,
                expected_fingerprint=(
                    authorized.certificate_fingerprint
                    if authorized is not None
                    else None
                ),
            )
            result = send_file(
                device.ip,
                device.tcp_port,
                upload_path,
                chunk_size=self.runtime.config.chunk_size,
                credential=credential,
                tls_policy=policy,
                progress_callback=lambda transferred, total: self._update(
                    task_id,
                    transferred_bytes=transferred,
                    total_bytes=total,
                ),
            )
            completed = self._update(
                task_id,
                status="success",
                transferred_bytes=int(result.get("bytes_received", task.total_bytes)),
                result=result,
            )
            self.runtime.audit_store.record_event(
                "file_sent",
                source_ip=device.ip,
                device_id=device.device_id,
                request_type="manual",
                bytes_count=completed.transferred_bytes,
                details={"task_id": task_id},
            )
        except TransferError as exc:
            self._update(
                task_id,
                status="failed",
                error_code=exc.code,
                error_message=exc.message,
            )
        except Exception as exc:
            self._update(
                task_id,
                status="failed",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
        finally:
            try:
                upload_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _update(self, task_id: str, **changes: Any) -> TransferTask:
        with self._lock:
            task = self._tasks[task_id]
            for key, value in changes.items():
                setattr(task, key, value)
            task.updated_at_ns = time.time_ns()
            payload = task.to_payload()
        self.runtime.events.publish("transfer_task", payload)
        return task

    def _publish(self, task: TransferTask) -> None:
        self.runtime.events.publish("transfer_task", task.to_payload())

    def observe_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type not in {
            "transfer_queued",
            "transfer_progress",
            "transfer_completed",
            "transfer_failed",
        }:
            return
        kind = str(payload.get("kind", "sync"))
        if kind == "manual":
            return
        relative_path = str(
            payload.get("relative_path") or payload.get("file_name") or ""
        )
        peer_key = str(payload.get("device_id") or payload.get("source_ip") or "")
        key = (kind, peer_key, relative_path)
        now = time.time_ns()
        with self._lock:
            task_id = self._observed_tasks.get(key)
            if task_id is None:
                device = (
                    self.runtime.find_online_device(peer_key)
                    if payload.get("device_id")
                    else None
                )
                trusted = (
                    self.runtime.security_store.get_device(peer_key)
                    if payload.get("device_id")
                    else None
                )
                task_id = uuid.uuid4().hex
                task = TransferTask(
                    task_id=task_id,
                    kind=kind,
                    status="queued",
                    file_name=Path(relative_path).name or "未知文件",
                    device_id=str(payload.get("device_id", "")),
                    device_name=(
                        device.device_name
                        if device is not None
                        else (
                            trusted.device_name
                            if trusted is not None
                            else str(payload.get("source_ip") or "远端设备")
                        )
                    ),
                    total_bytes=max(0, int(payload.get("total_bytes", 0))),
                    transferred_bytes=0,
                    created_at_ns=now,
                    updated_at_ns=now,
                )
                self._tasks[task_id] = task
                self._observed_tasks[key] = task_id
            task = self._tasks[task_id]
            if event_type == "transfer_queued":
                task.status = "queued"
            elif event_type == "transfer_progress":
                task.status = "running"
                task.transferred_bytes = max(
                    0,
                    int(payload.get("transferred_bytes", task.transferred_bytes)),
                )
            elif event_type == "transfer_completed":
                task.status = "success"
                task.transferred_bytes = max(
                    task.transferred_bytes,
                    int(payload.get("total_bytes", task.total_bytes)),
                )
            else:
                task.status = "failed"
                task.error_code = str(
                    payload.get("error_code", "TRANSFER_FAILED")
                )
                task.error_message = str(payload.get("error", "传输失败"))
            task.total_bytes = max(
                task.total_bytes,
                int(payload.get("total_bytes", task.total_bytes)),
            )
            task.updated_at_ns = now
            task_payload = task.to_payload()
            if event_type in {"transfer_completed", "transfer_failed"}:
                self._observed_tasks.pop(key, None)
            self._prune_completed()
        self.runtime.events.publish("transfer_task", task_payload)

    def _prune_completed(self) -> None:
        if len(self._tasks) <= 200:
            return
        completed = sorted(
            (
                task
                for task in self._tasks.values()
                if task.status in {"success", "failed"}
            ),
            key=lambda item: item.updated_at_ns,
        )
        for task in completed[: len(self._tasks) - 200]:
            self._tasks.pop(task.task_id, None)


class AppRuntime:
    def __init__(self, config_path: str | Path = "config.json") -> None:
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)
        self.configured_config = self.config
        self.pending_restart = False
        self.events = EventBus()
        self.started_at_ns: int | None = None
        self._started = False
        self._lifecycle_lock = threading.RLock()

        self.device_id = make_device_id(
            self.config.device_name,
            self.config.tcp_port,
        )
        self.audit_store = AuditStore(
            self.config.shared_folder,
            retention_days=self.config.audit_log_retention_days,
        )
        self.security_store = SecurityStore(
            self.config.shared_folder,
            audit_store=self.audit_store,
        )
        self.tls_identity = (
            ensure_tls_identity(
                self.config.shared_folder,
                self.config.device_name,
            )
            if self.config.enable_tls
            else None
        )
        self.file_index = FileIndex(
            self.config.shared_folder,
            self.device_id,
            self.config.device_name,
        )
        self.file_index.scan()
        self.tcp_server = TCPFileServer(
            host="0.0.0.0",
            port=self.config.tcp_port,
            shared_folder=self.config.shared_folder,
            chunk_size=self.config.chunk_size,
            file_index=self.file_index if self.config.sync_enabled else None,
            security_store=self.security_store,
            device_id=self.device_id,
            device_name=self.config.device_name,
            tls_identity=self.tls_identity,
            audit_store=self.audit_store,
            event_callback=self._publish_event,
        )
        self.discovery = DiscoveryService(
            device_id=self.device_id,
            device_name=self.config.device_name,
            udp_port=self.config.udp_port,
            tcp_port=self.config.tcp_port,
            broadcast_ip=self.config.broadcast_ip,
            tls_enabled=self.config.enable_tls,
            certificate_fingerprint=(
                self.tls_identity.fingerprint
                if self.tls_identity is not None
                else None
            ),
            audit_store=self.audit_store,
            event_callback=self._publish_event,
        )
        self.sync_service = SyncService(
            file_index=self.file_index,
            discovery=self.discovery,
            chunk_size=self.config.chunk_size,
            interval_seconds=self.config.sync_interval_seconds,
            security_store=self.security_store,
            tls_enabled=self.config.enable_tls,
            audit_store=self.audit_store,
            event_callback=self._publish_event,
        )
        self.agent_model = AgentModelClient(self.config)
        self.deepseek = self.agent_model
        self.connection_agent = ConnectionDiagnosticAgent(
            discovery=self.discovery,
            tcp_server=self.tcp_server,
            udp_port=self.config.udp_port,
            tls_enabled=self.config.enable_tls,
            security_store=self.security_store,
        )
        self.conflict_agent = ConflictAnalysisAgent(self.file_index)
        self.security_agent = SecurityAuditAgent(
            shared_folder=self.config.shared_folder,
            security_store=self.security_store,
            audit_store=self.audit_store,
            discovery=self.discovery,
            tls_enabled=self.config.enable_tls,
            tls_identity=self.tls_identity,
            size_risk_bytes=self.config.shared_size_risk_bytes,
            file_count_risk=self.config.shared_file_count_risk,
        )
        self.security_audit_service = SecurityAuditService(
            self.security_agent,
            self.audit_store,
            interval_seconds=self.config.security_audit_interval_seconds,
        )
        self.transfers = TransferTaskManager(self)
        self.react_agent = ReActSyncAgent(self)

    def _publish_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event_payload = payload or {}
        transfers = getattr(self, "transfers", None)
        if transfers is not None:
            transfers.observe_event(event_type, event_payload)
        self.events.publish(event_type, event_payload)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            self.tcp_server.start()
            self.discovery.start()
            if self.config.sync_enabled:
                self.sync_service.start()
            self.security_audit_service.start()
            self.started_at_ns = time.time_ns()
            self._started = True
        self.events.publish("runtime_started", self.runtime_status())

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return
            self.security_audit_service.stop()
            self.sync_service.stop()
            self.discovery.stop()
            self.tcp_server.stop()
            self._started = False
        self.events.publish("runtime_stopped", {})

    def runtime_status(self) -> dict[str, Any]:
        return {
            "running": self._started,
            "started_at_ns": self.started_at_ns,
            "device_id": self.device_id,
            "device_name": self.config.device_name,
            "local_ip": get_local_ip(),
            "udp_port": self.config.udp_port,
            "tcp_port": self.tcp_server.bound_port,
            "web_port": self.config.web_port,
            "shared_folder": str(self.config.shared_folder),
            "tls_enabled": self.config.enable_tls,
            "certificate_fingerprint": (
                self.tls_identity.fingerprint
                if self.tls_identity is not None
                else None
            ),
            "sync_enabled": self.config.sync_enabled,
            "sync_interval_seconds": self.config.sync_interval_seconds,
            "pending_restart": self.pending_restart,
        }

    def find_online_device(self, device_id: str):
        return next(
            (
                device
                for device in self.discovery.list_devices()
                if device.device_id == str(device_id)
            ),
            None,
        )

    def devices_payload(self) -> list[dict[str, Any]]:
        online = {
            device.device_id: device
            for device in self.discovery.list_devices()
        }
        authorized = {
            device.device_id: device
            for device in self.security_store.list_devices()
        }
        rows: list[dict[str, Any]] = []
        for device_id in sorted(
            set(online) | set(authorized),
            key=lambda item: (
                (online.get(item) or authorized.get(item)).device_name.lower(),
                item,
            ),
        ):
            found = online.get(device_id)
            trusted = authorized.get(device_id)
            rows.append(
                {
                    "device_id": device_id,
                    "device_name": (
                        found.device_name if found is not None else trusted.device_name
                    ),
                    "online": found is not None,
                    "status": found.status if found is not None else "offline",
                    "ip": found.ip if found is not None else "",
                    "tcp_port": found.tcp_port if found is not None else None,
                    "last_seen": found.last_seen if found is not None else None,
                    "tls_enabled": (
                        found.tls_enabled
                        if found is not None
                        else bool(trusted and trusted.certificate_fingerprint)
                    ),
                    "paired": trusted is not None,
                    "permission": (
                        trusted.permission if trusted is not None else "unpaired"
                    ),
                    "paired_at_ns": (
                        trusted.paired_at_ns if trusted is not None else None
                    ),
                    "last_authenticated_at_ns": (
                        trusted.last_authenticated_at_ns
                        if trusted is not None
                        else None
                    ),
                    "certificate_fingerprint": (
                        trusted.certificate_fingerprint
                        if trusted is not None
                        else (
                            found.certificate_fingerprint
                            if found is not None
                            else None
                        )
                    ),
                    "last_sync_at_ns": self.file_index.get_last_sync_time(device_id),
                    "stranger": found is not None and trusted is None,
                }
            )
        return rows

    def files_payload(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        query: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        self.file_index.scan()
        conflicts = {
            conflict.relative_path
            for conflict in self.file_index.list_conflicts(resolved=False)
        }
        peers = [
            device
            for device in self.security_store.list_devices()
            if device.permission not in {"blocked", "read"}
        ]
        rows = []
        for entry in self.file_index.snapshot():
            sync_status = self._file_sync_status(entry, peers, conflicts)
            if query and query.lower() not in entry.relative_path.lower():
                continue
            if status and sync_status != status:
                continue
            rows.append(
                {
                    **asdict(entry),
                    "sync_status": sync_status,
                    "preview_kind": (
                        file_preview_kind(entry.file_name)
                        if entry.status == STATUS_ACTIVE
                        else None
                    ),
                }
            )
        total = len(rows)
        page = rows[max(0, offset) : max(0, offset) + max(1, min(limit, 200))]
        return {"items": page, "total": total}

    def shared_file_path(self, relative_path: str) -> tuple[FileEntry, Path]:
        normalized = normalize_relative_path(relative_path)
        try:
            destination_for(self.config.shared_folder, normalized)
        except ValueError as exc:
            raise FileNotFoundError(normalized) from exc

        entry = self.file_index.refresh_path(normalized)
        if entry is None or entry.status != STATUS_ACTIVE:
            raise FileNotFoundError(normalized)

        try:
            path = destination_for(self.config.shared_folder, normalized)
        except ValueError as exc:
            raise FileNotFoundError(normalized) from exc
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(normalized)
        return entry, path

    def _file_sync_status(
        self,
        entry: FileEntry,
        peers: list,
        conflicts: set[str],
    ) -> str:
        if entry.relative_path in conflicts:
            return "conflict"
        if entry.status == STATUS_DELETED:
            return "deleted_record"
        if not peers:
            return "local_only"
        for peer in peers:
            state = self.file_index.get_sync_state(
                peer.device_id,
                entry.relative_path,
            )
            if (
                state is None
                or state.baseline_hash != entry.file_hash
                or state.baseline_status != entry.status
            ):
                return "pending"
        return "synced"

    def dashboard(self) -> dict[str, Any]:
        devices = self.devices_payload()
        files = self.files_payload(limit=1)
        conflicts = self.file_index.list_conflicts(resolved=False)
        alerts = self.audit_store.recent_alerts(10, unread_only=True)
        security = self.security_agent.analyze()
        recent = self.audit_store.recent_events(12)
        return {
            "runtime": self.runtime_status(),
            "counts": {
                "online_devices": sum(item["online"] for item in devices),
                "paired_devices": sum(item["paired"] for item in devices),
                "files": files["total"],
                "unresolved_conflicts": len(conflicts),
                "risk_alerts": len(alerts),
            },
            "security": agent_payload(security),
            "recent_events": [asdict(event) for event in recent],
            "transfers": self.transfers.list_tasks(8),
        }

    def pair_device(self, device_id: str, pair_code: str) -> dict[str, Any]:
        device = self.find_online_device(device_id)
        if device is None:
            raise KeyError("device is not online")
        response = pair_with_device(
            device.ip,
            device.tcp_port,
            pair_code=pair_code,
            local_device_id=self.device_id,
            local_device_name=self.config.device_name,
            security_store=self.security_store,
            tls_policy=TLSPolicy(
                enabled=self.config.enable_tls,
                expected_fingerprint=device.certificate_fingerprint,
                allow_untrusted=self.config.enable_tls,
            ),
            local_tls_identity=self.tls_identity,
        )
        self.audit_store.record_event(
            "pairing_succeeded",
            source_ip=device.ip,
            device_id=device.device_id,
            details={"tls": self.config.enable_tls},
        )
        self.events.publish("device_paired", {"device_id": device.device_id})
        return response

    def trigger_sync(self) -> None:
        def run() -> None:
            try:
                self.sync_service.sync_once()
            except Exception as exc:
                self.events.publish("sync_failed", {"error": str(exc)})

        threading.Thread(target=run, name="web-sync-now", daemon=True).start()

    def conflicts_payload(self, resolved: bool | None = None) -> list[dict[str, Any]]:
        return [
            asdict(conflict)
            for conflict in self.file_index.list_conflicts(resolved=resolved)
        ]

    def resolve_conflict(self, conflict_id: int, note: str = "") -> bool:
        resolved = self.file_index.resolve_conflict(conflict_id, note)
        if resolved:
            self.events.publish(
                "conflict_resolved",
                {"conflict_id": int(conflict_id)},
            )
        return resolved

    def conflict_path(self, conflict_id: int, variant: str) -> Path:
        conflict = self.file_index.get_conflict(conflict_id)
        if conflict is None:
            raise KeyError(conflict_id)
        relative_path = (
            conflict.relative_path
            if variant == "main"
            else conflict.conflict_copy_path
        )
        if not relative_path:
            raise FileNotFoundError("conflict copy is not available")
        path = destination_for(self.config.shared_folder, relative_path)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(relative_path)
        return path

    def run_agent(
        self,
        agent_name: str,
        *,
        device_id: str = "",
        conflict_id: int | None = None,
        enhance: bool = True,
    ) -> dict[str, Any]:
        if agent_name == "connection":
            device = self.find_online_device(device_id) if device_id else None
            result = self.connection_agent.analyze(device)
        elif agent_name == "conflict":
            conflict = (
                self.file_index.get_conflict(conflict_id)
                if conflict_id is not None
                else None
            )
            result = self.conflict_agent.analyze(conflict)
        elif agent_name == "security":
            result = self.security_agent.analyze()
        else:
            raise ValueError("unknown agent")
        if enhance:
            result = self.agent_model.enhance(result)
        return agent_payload(result)

    def settings_payload(self) -> dict[str, Any]:
        return {
            "active": config_payload(self.config),
            "configured": config_payload(self.configured_config),
            "pending_restart": self.pending_restart,
            "restart_fields": sorted(RESTART_FIELDS),
            "immediate_fields": sorted(IMMEDIATE_FIELDS),
            "deepseek_api_key_configured": bool(
                os.environ.get("DEEPSEEK_API_KEY", "").strip()
            ),
            "openai_api_key_configured": bool(
                os.environ.get("OPENAI_API_KEY", "").strip()
            ),
        }

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        unknown = set(changes) - (RESTART_FIELDS | IMMEDIATE_FIELDS)
        if unknown:
            raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
        current_raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        candidate = {**current_raw, **changes}
        validated = AppConfig.from_mapping(candidate, self.config_path.parent)
        self.config_path.write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.configured_config = validated
        restart_changed = any(
            key in changes
            and getattr(self.config, key) != getattr(validated, key)
            for key in RESTART_FIELDS
        )
        self.pending_restart = self.pending_restart or restart_changed
        immediate = {
            key: getattr(validated, key)
            for key in IMMEDIATE_FIELDS
            if key in changes
        }
        if immediate:
            self._apply_immediate_settings(immediate)
            self.config = replace(self.config, **immediate)
        self.events.publish(
            "settings_updated",
            {
                "pending_restart": self.pending_restart,
                "changed_fields": sorted(changes),
            },
        )
        return self.settings_payload()

    def _apply_immediate_settings(self, changes: dict[str, Any]) -> None:
        if "sync_interval_seconds" in changes:
            self.sync_service.interval_seconds = float(
                changes["sync_interval_seconds"]
            )
        if "sync_enabled" in changes:
            enabled = bool(changes["sync_enabled"])
            self.tcp_server.file_index = self.file_index if enabled else None
            if enabled:
                self.sync_service.start()
            else:
                self.sync_service.stop()
        if "security_audit_interval_seconds" in changes:
            self.security_audit_service.interval_seconds = float(
                changes["security_audit_interval_seconds"]
            )
        if "audit_log_retention_days" in changes:
            self.audit_store.retention_days = int(
                changes["audit_log_retention_days"]
            )
            self.audit_store.prune()
        if "shared_size_risk_bytes" in changes:
            self.security_agent.size_risk_bytes = int(
                changes["shared_size_risk_bytes"]
            )
        if "shared_file_count_risk" in changes:
            self.security_agent.file_count_risk = int(
                changes["shared_file_count_risk"]
            )
        if {
            "agent_api_url",
            "agent_provider",
            "agent_model",
            "agent_timeout_seconds",
        } & changes.keys():
            self.agent_model.config = replace(self.config, **changes)


def config_payload(config: AppConfig) -> dict[str, Any]:
    payload = asdict(config)
    payload["shared_folder"] = str(config.shared_folder)
    return payload


def agent_payload(result: AgentResult) -> dict[str, Any]:
    return {
        "agent": result.agent,
        "summary": result.summary,
        "severity": result.severity,
        "evidence": list(result.evidence),
        "causes": list(result.causes),
        "recommendations": list(result.recommendations),
        "facts": result.facts,
        "enhanced": result.enhanced,
        "enhancement_note": result.enhancement_note,
        "source": "deepseek" if result.enhanced else "local",
    }
