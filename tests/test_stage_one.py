from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import load_config
from transfer import TCPFileServer, send_file


class ConfigTests(unittest.TestCase):
    def test_load_config_resolves_shared_folder_relative_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "device_name": "Lab-PC",
                        "udp_port": 9100,
                        "tcp_port": 9101,
                        "broadcast_ip": "255.255.255.255",
                        "shared_folder": "received",
                        "chunk_size": 16,
                        "enable_tls": False,
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.device_name, "Lab-PC")
            self.assertEqual(config.udp_port, 9100)
            self.assertEqual(config.tcp_port, 9101)
            self.assertEqual(config.shared_folder, Path(tmp) / "received")
            self.assertTrue(config.sync_enabled)
            self.assertEqual(config.sync_interval_seconds, 10.0)
            self.assertTrue(config.shared_folder.is_dir())

    def test_load_config_reads_dotenv_without_overriding_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            (root / ".env").write_text(
                "DEEPSEEK_API_KEY=from-dotenv\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                load_config(config_path)
                self.assertEqual(
                    os.environ.get("DEEPSEEK_API_KEY"),
                    "from-dotenv",
                )

            with patch.dict(
                os.environ,
                {"DEEPSEEK_API_KEY": "from-environment"},
                clear=True,
            ):
                load_config(config_path)
                self.assertEqual(
                    os.environ.get("DEEPSEEK_API_KEY"),
                    "from-environment",
                )


class TCPTransferTests(unittest.TestCase):
    def test_send_file_to_loopback_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "message.txt"
            received_dir = root / "received"
            source.write_text("hello from stage one\n", encoding="utf-8")

            server = TCPFileServer("127.0.0.1", 0, received_dir, chunk_size=8)
            server.start()
            try:
                result = send_file("127.0.0.1", server.bound_port, source, chunk_size=8)
            finally:
                server.stop()

            received = received_dir / "message.txt"
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["file_name"], "message.txt")
            self.assertEqual(received.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))

    def test_existing_file_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            received_dir = root / "received"
            source_dir.mkdir()
            received_dir.mkdir()

            source = source_dir / "message.txt"
            source.write_text("new text\n", encoding="utf-8")
            existing = received_dir / "message.txt"
            existing.write_text("old text\n", encoding="utf-8")

            server = TCPFileServer("127.0.0.1", 0, received_dir, chunk_size=8)
            server.start()
            try:
                result = send_file("127.0.0.1", server.bound_port, source, chunk_size=8)
            finally:
                server.stop()

            self.assertEqual(result["status"], "success")
            self.assertEqual(existing.read_text(encoding="utf-8"), "old text\n")
            self.assertEqual((received_dir / "message_1.txt").read_text(encoding="utf-8"), "new text\n")


if __name__ == "__main__":
    unittest.main()
