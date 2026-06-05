from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from .protocol import recv_json_message, send_json_message


def send_file(
    target_ip: str,
    target_port: int,
    file_path: str | Path,
    chunk_size: int = 1024 * 1024,
    timeout: float = 60.0,
) -> dict[str, Any]:
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {path}")

    file_size = path.stat().st_size
    metadata = {
        "type": "FILE_SEND",
        "file_name": path.name,
        "file_size": file_size,
        "chunk_size": chunk_size,
    }

    with socket.create_connection((target_ip, int(target_port)), timeout=timeout) as sock:
        sock.settimeout(timeout)
        send_json_message(sock, metadata)
        with path.open("rb") as source:
            while True:
                chunk = source.read(chunk_size)
                if not chunk:
                    break
                sock.sendall(chunk)
        return recv_json_message(sock)
