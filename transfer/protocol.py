from __future__ import annotations

import json
import socket
import struct
from typing import Any


HEADER_SIZE = 4
MAX_METADATA_BYTES = 64 * 1024


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("connection closed before enough data was received")
        data.extend(chunk)
    return bytes(data)


def send_json_message(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_METADATA_BYTES:
        raise ValueError("metadata is too large")
    sock.sendall(struct.pack("!I", len(data)))
    sock.sendall(data)


def recv_json_message(sock: socket.socket) -> dict[str, Any]:
    raw_size = recv_exact(sock, HEADER_SIZE)
    size = struct.unpack("!I", raw_size)[0]
    if size <= 0 or size > MAX_METADATA_BYTES:
        raise ValueError(f"invalid metadata size: {size}")
    data = recv_exact(sock, size)
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("metadata must be a JSON object")
    return payload
