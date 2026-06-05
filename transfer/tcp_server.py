from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import recv_json_message, send_json_message


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceivedFile:
    path: Path
    bytes_received: int
    peer: tuple[str, int]


def safe_file_name(file_name: str) -> str:
    normalized = str(file_name).replace("\\", "/")
    name = Path(normalized).name.strip()
    return name or "received_file"


def next_available_path(folder: Path, file_name: str) -> Path:
    target = folder / safe_file_name(file_name)
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    counter = 1
    while True:
        candidate = target.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


class TCPFileServer:
    def __init__(
        self,
        host: str,
        port: int,
        shared_folder: str | Path,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        self.host = host
        self.port = port
        self.shared_folder = Path(shared_folder)
        self.chunk_size = chunk_size
        self._stop_event = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._client_threads: list[threading.Thread] = []

    @property
    def bound_port(self) -> int:
        if self._socket is None:
            return self.port
        return int(self._socket.getsockname()[1])

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self.shared_folder.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()

        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.host, self.port))
        server_socket.listen()
        server_socket.settimeout(1.0)
        self._socket = server_socket

        self._thread = threading.Thread(target=self._serve_forever, daemon=True)
        self._thread.start()
        logger.info("TCP server listening on %s:%s", self.host, self.bound_port)

    def stop(self) -> None:
        self._stop_event.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        for thread in list(self._client_threads):
            thread.join(timeout=2.0)

    def _serve_forever(self) -> None:
        assert self._socket is not None
        while not self._stop_event.is_set():
            try:
                conn, addr = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            thread = threading.Thread(
                target=self._handle_client,
                args=(conn, addr),
                daemon=True,
            )
            self._client_threads.append(thread)
            thread.start()

    def _handle_client(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        with conn:
            conn.settimeout(60.0)
            try:
                received = self.receive_file(conn, addr)
                send_json_message(
                    conn,
                    {
                        "type": "FILE_RECEIVED",
                        "status": "success",
                        "file_name": received.path.name,
                        "bytes_received": received.bytes_received,
                    },
                )
                logger.info("received %s from %s:%s", received.path, *addr)
            except Exception as exc:
                logger.exception("failed to receive file from %s:%s", *addr)
                try:
                    send_json_message(
                        conn,
                        {
                            "type": "FILE_RECEIVED",
                            "status": "error",
                            "message": str(exc),
                        },
                    )
                except OSError:
                    pass

    def receive_file(self, conn: socket.socket, addr: tuple[str, int]) -> ReceivedFile:
        metadata = recv_json_message(conn)
        self._validate_metadata(metadata)

        file_name = safe_file_name(str(metadata["file_name"]))
        file_size = int(metadata["file_size"])
        recv_chunk_size = max(1, int(metadata.get("chunk_size", self.chunk_size)))
        destination = next_available_path(self.shared_folder, file_name)

        bytes_received = 0
        try:
            with destination.open("wb") as output:
                while bytes_received < file_size:
                    remaining = file_size - bytes_received
                    chunk = conn.recv(min(recv_chunk_size, remaining))
                    if not chunk:
                        raise ConnectionError("connection closed before file was complete")
                    output.write(chunk)
                    bytes_received += len(chunk)
        except Exception:
            if destination.exists():
                destination.unlink()
            raise

        return ReceivedFile(path=destination, bytes_received=bytes_received, peer=addr)

    @staticmethod
    def _validate_metadata(metadata: dict[str, Any]) -> None:
        if metadata.get("type") != "FILE_SEND":
            raise ValueError("unsupported request type")
        if "file_name" not in metadata:
            raise ValueError("missing file_name")
        if "file_size" not in metadata:
            raise ValueError("missing file_size")
        file_size = int(metadata["file_size"])
        if file_size < 0:
            raise ValueError("file_size must be non-negative")
