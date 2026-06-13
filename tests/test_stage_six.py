from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage

from agents import (
    AgentModelClient,
    AgentResult,
    ConflictAnalysisAgent,
    ConnectionDiagnosticAgent,
    SecurityAuditAgent,
    create_chat_model,
)
from audit import AuditStore
from discovery import DiscoveredDevice
from security import (
    PERMISSION_WRITE,
    SecurityStore,
    TLSPolicy,
    ensure_tls_identity,
)
from security.pairing import pair_with_device
from sync import FileEntry, FileIndex
from sync.index_exchange import request_file_index
from sync.service import SyncService
from transfer import TCPFileServer, TransferError, send_file


class StaticDiscovery:
    def __init__(self, devices=None) -> None:
        self.devices = list(devices or [])

    def list_devices(self):
        return list(self.devices)


def discovered(
    device_id: str,
    name: str,
    port: int,
    fingerprint: str,
) -> DiscoveredDevice:
    return DiscoveredDevice(
        device_id=device_id,
        device_name=name,
        ip="127.0.0.1",
        tcp_port=port,
        status="online",
        last_seen=time.time(),
        tls_enabled=True,
        certificate_fingerprint=fingerprint,
    )


class TLSIdentityTests(unittest.TestCase):
    def test_identity_is_persistent_and_private_key_is_restricted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = ensure_tls_identity(tmp, "A")
            second = ensure_tls_identity(tmp, "A")

            self.assertEqual(first.fingerprint, second.fingerprint)
            self.assertEqual(
                first.private_key_path.stat().st_mode & 0o777,
                0o600,
            )
            self.assertGreater(first.expires_at.timestamp(), time.time())

    def test_tls_file_transfer_and_fingerprint_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            received = root / "received"
            identity = ensure_tls_identity(received, "B")
            source = root / "secret.txt"
            source.write_text("encrypted content", encoding="utf-8")
            server = TCPFileServer(
                "127.0.0.1",
                0,
                received,
                tls_identity=identity,
            )
            server.start()
            try:
                result = send_file(
                    "127.0.0.1",
                    server.bound_port,
                    source,
                    tls_policy=TLSPolicy(
                        enabled=True,
                        expected_fingerprint=identity.fingerprint,
                    ),
                )
                with self.assertRaises(TransferError) as wrong:
                    send_file(
                        "127.0.0.1",
                        server.bound_port,
                        source,
                        tls_policy=TLSPolicy(
                            enabled=True,
                            expected_fingerprint="0" * 64,
                        ),
                    )
            finally:
                server.stop()

            self.assertEqual(result["status"], "success")
            self.assertEqual(wrong.exception.code, "TLS_IDENTITY_FAILED")
            self.assertEqual(
                (received / "secret.txt").read_text(encoding="utf-8"),
                "encrypted content",
            )


class TLSPairingAndSyncTests(unittest.TestCase):
    def test_tls_pairing_binds_both_certificate_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder_a = root / "a"
            folder_b = root / "b"
            identity_a = ensure_tls_identity(folder_a, "A")
            identity_b = ensure_tls_identity(folder_b, "B")
            security_a = SecurityStore(folder_a)
            security_b = SecurityStore(folder_b)
            index_b = FileIndex(folder_b, "device-b", "B")
            (folder_b / "indexed.txt").write_text("index", encoding="utf-8")
            index_b.scan()
            server_b = TCPFileServer(
                "127.0.0.1",
                0,
                folder_b,
                file_index=index_b,
                security_store=security_b,
                device_id="device-b",
                device_name="B",
                tls_identity=identity_b,
            )
            server_b.start()
            try:
                pair_with_device(
                    "127.0.0.1",
                    server_b.bound_port,
                    pair_code=security_b.pair_code,
                    local_device_id="device-a",
                    local_device_name="A",
                    security_store=security_a,
                    local_tls_identity=identity_a,
                    tls_policy=TLSPolicy(
                        enabled=True,
                        expected_fingerprint=identity_b.fingerprint,
                    ),
                )
                credential = security_a.credential_for("device-b", "device-a")
                entries = request_file_index(
                    "127.0.0.1",
                    server_b.bound_port,
                    credential=credential,
                    tls_policy=TLSPolicy(
                        enabled=True,
                        expected_fingerprint=identity_b.fingerprint,
                    ),
                )
            finally:
                server_b.stop()

            self.assertEqual(
                security_a.get_device("device-b").certificate_fingerprint,
                identity_b.fingerprint,
            )
            self.assertEqual(
                security_b.get_device("device-a").certificate_fingerprint,
                identity_a.fingerprint,
            )
            self.assertEqual(len(entries), 1)

    def test_tls_sync_uses_bound_identity_and_old_pairing_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder_a = root / "a"
            folder_b = root / "b"
            identity_a = ensure_tls_identity(folder_a, "A")
            identity_b = ensure_tls_identity(folder_b, "B")
            security_a = SecurityStore(folder_a)
            security_b = SecurityStore(folder_b)
            token = SecurityStore.generate_token()
            security_a.authorize_device(
                "device-b",
                "B",
                token,
                PERMISSION_WRITE,
                identity_b.fingerprint,
            )
            security_b.authorize_device(
                "device-a",
                "A",
                token,
                PERMISSION_WRITE,
                identity_a.fingerprint,
            )
            index_a = FileIndex(folder_a, "device-a", "A")
            index_b = FileIndex(folder_b, "device-b", "B")
            server_b = TCPFileServer(
                "127.0.0.1",
                0,
                folder_b,
                file_index=index_b,
                security_store=security_b,
                device_id="device-b",
                device_name="B",
                tls_identity=identity_b,
            )
            server_b.start()
            try:
                device_b = discovered(
                    "device-b",
                    "B",
                    server_b.bound_port,
                    identity_b.fingerprint,
                )
                (folder_a / "sync.txt").write_text("over tls", encoding="utf-8")
                SyncService(
                    index_a,
                    StaticDiscovery([device_b]),
                    chunk_size=8,
                    security_store=security_a,
                    tls_enabled=True,
                ).sync_once()

                old_security = SecurityStore(root / "old")
                old_security.authorize_device(
                    "device-b",
                    "B",
                    token,
                    PERMISSION_WRITE,
                )
                old_credential = old_security.credential_for(
                    "device-b",
                    "old-device",
                )
                with self.assertRaises(ConnectionError):
                    request_file_index(
                        "127.0.0.1",
                        server_b.bound_port,
                        credential=old_credential,
                        tls_policy=TLSPolicy(enabled=True),
                    )
            finally:
                server_b.stop()

            self.assertEqual(
                (folder_b / "sync.txt").read_text(encoding="utf-8"),
                "over tls",
            )


class AuditTests(unittest.TestCase):
    def test_audit_redacts_secrets_and_detects_failure_burst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(tmp, alert_cooldown_seconds=600)
            for _ in range(5):
                audit.record_event(
                    "authentication_failed",
                    severity="warning",
                    source_ip="192.0.2.10",
                    outcome="rejected",
                    details={
                        "auth_token": "secret-token",
                        "pair_code": "123456",
                        "error_code": "AUTH_FAILED",
                    },
                )

            event = audit.recent_events(1)[0]
            alerts = audit.recent_alerts(10)
            self.assertEqual(event.details["auth_token"], "[REDACTED]")
            self.assertEqual(event.details["pair_code"], "[REDACTED]")
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].rule_code, "AUTH_FAILURE_BURST")
            self.assertTrue(audit.log_path.is_file())


class AgentTests(unittest.TestCase):
    def test_connection_agent_confirms_tls_diagnostic_ping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = ensure_tls_identity(root, "B")
            security = SecurityStore(root)
            security.authorize_device(
                "device-b",
                "B",
                "token",
                PERMISSION_WRITE,
                identity.fingerprint,
            )
            server = TCPFileServer(
                "127.0.0.1",
                0,
                root,
                tls_identity=identity,
            )
            server.start()
            try:
                target = discovered(
                    "device-b",
                    "B",
                    server.bound_port,
                    identity.fingerprint,
                )
                result = ConnectionDiagnosticAgent(
                    discovery=StaticDiscovery([target]),
                    tcp_server=server,
                    udp_port=9000,
                    tls_enabled=True,
                    security_store=security,
                ).analyze(target)
            finally:
                server.stop()

        self.assertTrue(result.facts["tcp_reachable"])
        self.assertTrue(result.facts["tls_ok"])

    def test_connection_agent_explains_missing_devices_and_services(self) -> None:
        class StoppedDiscovery:
            _listen_socket = None

            class StopEvent:
                @staticmethod
                def is_set():
                    return True

            _stop_event = StopEvent()

            @staticmethod
            def list_devices():
                return []

        class StoppedServer:
            _thread = None

        with tempfile.TemporaryDirectory() as tmp:
            result = ConnectionDiagnosticAgent(
                discovery=StoppedDiscovery(),
                tcp_server=StoppedServer(),
                udp_port=9000,
                tls_enabled=True,
                security_store=SecurityStore(tmp),
            ).analyze()

        self.assertIn("TARGET_NOT_DISCOVERED", result.facts["cause_codes"])
        self.assertIn("UDP_LISTENER_DOWN", result.facts["cause_codes"])
        self.assertIn("TCP_SERVER_DOWN", result.facts["cause_codes"])

    def test_conflict_and_security_agents_report_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared"
            audit = AuditStore(shared)
            security = SecurityStore(shared)
            index = FileIndex(shared, "device-a", "A")
            local = FileEntry(
                "same.txt",
                "same.txt",
                10,
                100,
                "a" * 64,
                2,
                "device-a",
                "active",
                100,
                "A",
            )
            remote = FileEntry(
                "same.txt",
                "same.txt",
                20,
                200,
                "b" * 64,
                3,
                "device-b",
                "active",
                200,
                "B",
            )
            index.record_conflict(
                relative_path="same.txt",
                local_entry=local,
                remote_entry=remote,
                remote_device_id="device-b",
                winner_device_id="device-b",
                conflict_copy_path="same_A_conflict.txt",
                reason_code="BOTH_MODIFIED",
            )

            conflict_result = ConflictAnalysisAgent(index).analyze()
            security_result = SecurityAuditAgent(
                shared_folder=shared,
                security_store=security,
                audit_store=audit,
                discovery=StaticDiscovery(),
                tls_enabled=False,
                tls_identity=None,
                size_risk_bytes=1,
                file_count_risk=1,
            ).analyze()

            self.assertIn("双方都偏离", conflict_result.causes[0])
            self.assertEqual(conflict_result.facts["reason_code"], "BOTH_MODIFIED")
            self.assertIn("TLS", " ".join(security_result.causes))
            self.assertEqual(security_result.severity, "error")

    def test_deepseek_enhancement_and_offline_fallback(self) -> None:
        result = AgentResult(
            agent="security",
            summary="local",
            severity="warning",
            evidence=("local path must not be sent",),
            causes=("local cause",),
            recommendations=("local recommendation",),
            facts={
                "alert_count": 2,
                "cause_codes": ("UNREAD_ALERTS",),
                "private_path": "/secret/path",
            },
        )
        client = AgentModelClient(
            SimpleNamespace(
                agent_provider="deepseek",
                agent_api_url="https://api.deepseek.com",
                agent_model="deepseek-chat",
                agent_timeout_seconds=20,
            )
        )
        with patch.dict(os.environ, {}, clear=True):
            fallback = client.enhance(result)
        self.assertFalse(fallback.enhanced)
        self.assertIn("未设置", fallback.enhancement_note)

        class FakeModel:
            def __init__(self):
                self.messages = []

            def invoke(self, messages):
                self.messages = messages
                return AIMessage(
                    content=json.dumps(
                        {
                            "summary": "enhanced",
                            "causes": ["cause"],
                            "recommendations": ["fix"],
                        }
                    )
                )

        fake = FakeModel()
        with patch(
            "agents.model_factory.create_chat_model",
            return_value=fake,
        ):
            enhanced = client.enhance(result)

        user_payload = json.loads(fake.messages[1].content)
        self.assertNotIn("private_path", user_payload["facts"])
        self.assertTrue(enhanced.enhanced)
        self.assertEqual(enhanced.summary, "enhanced")

    def test_chat_model_uses_provider_specific_api_key(self) -> None:
        values = {
            "OPENAI_API_KEY": "openai-key",
            "DEEPSEEK_API_KEY": "deepseek-key",
        }
        common = {
            "agent_api_url": "",
            "agent_model": "test-model",
            "agent_timeout_seconds": 20,
        }
        with (
            patch.dict(os.environ, values, clear=True),
            patch("agents.model_factory.ChatOpenAI") as openai,
            patch("agents.model_factory.ChatDeepSeek") as deepseek,
        ):
            create_chat_model(SimpleNamespace(agent_provider="openai", **common))
            create_chat_model(SimpleNamespace(agent_provider="deepseek", **common))

        self.assertEqual(openai.call_args.kwargs["api_key"], "openai-key")
        self.assertEqual(deepseek.call_args.kwargs["api_key"], "deepseek-key")


if __name__ == "__main__":
    unittest.main()
