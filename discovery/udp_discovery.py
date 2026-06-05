from __future__ import annotations

import json
import logging
import socket
import threading
import time
import uuid
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredDevice:
    device_id: str
    device_name: str
    ip: str
    tcp_port: int
    status: str
    last_seen: float


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def make_device_id(device_name: str, tcp_port: int) -> str:
    basis = f"{socket.gethostname()}:{device_name}:{tcp_port}:{uuid.getnode()}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, basis))


class DiscoveryService:
    def __init__(
        self,
        device_id: str,
        device_name: str,
        udp_port: int,
        tcp_port: int,
        broadcast_ip: str = "255.255.255.255",
        broadcast_interval: float = 3.0,
        stale_after: float = 15.0,
    ) -> None:
        self.device_id = device_id
        self.device_name = device_name
        self.udp_port = int(udp_port)
        self.tcp_port = int(tcp_port)
        self.broadcast_ip = broadcast_ip
        self.broadcast_interval = broadcast_interval
        self.stale_after = stale_after
        self._devices: dict[str, DiscoveredDevice] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._listen_socket: socket.socket | None = None

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop_event.clear()
        self._threads = [
            threading.Thread(target=self._listen_loop, daemon=True),
            threading.Thread(target=self._broadcast_loop, daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        logger.info("UDP discovery started on port %s", self.udp_port)

    def stop(self) -> None:
        self._stop_event.set()
        if self._listen_socket is not None:
            try:
                self._listen_socket.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2.0)

    def list_devices(self) -> list[DiscoveredDevice]:
        self._purge_stale_devices()
        with self._lock:
            return sorted(
                self._devices.values(),
                key=lambda device: (device.device_name.lower(), device.ip, device.tcp_port),
            )

    def _broadcast_loop(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            while not self._stop_event.is_set():
                payload = {
                    "type": "DISCOVERY",
                    "device_id": self.device_id,
                    "device_name": self.device_name,
                    "ip": get_local_ip(),
                    "tcp_port": self.tcp_port,
                    "status": "online",
                }
                try:
                    sock.sendto(
                        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        (self.broadcast_ip, self.udp_port),
                    )
                except OSError as exc:
                    logger.warning("failed to send UDP discovery broadcast: %s", exc)
                self._stop_event.wait(self.broadcast_interval)

    def _listen_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._listen_socket = sock
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", self.udp_port))
            sock.settimeout(1.0)
            while not self._stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(8192)
                except socket.timeout:
                    self._purge_stale_devices()
                    continue
                except OSError:
                    break
                self._handle_message(data, addr)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _handle_message(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            message = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

        if not isinstance(message, dict) or message.get("type") != "DISCOVERY":
            return

        device_id = str(message.get("device_id", ""))
        if not device_id or device_id == self.device_id:
            return

        try:
            tcp_port = int(message["tcp_port"])
        except (KeyError, TypeError, ValueError):
            return

        device = DiscoveredDevice(
            device_id=device_id,
            device_name=str(message.get("device_name", "Unknown")),
            ip=str(message.get("ip") or addr[0]),
            tcp_port=tcp_port,
            status=str(message.get("status", "online")),
            last_seen=time.time(),
        )
        with self._lock:
            self._devices[device.device_id] = device

    def _purge_stale_devices(self) -> None:
        cutoff = time.time() - self.stale_after
        with self._lock:
            stale_ids = [
                device_id
                for device_id, device in self._devices.items()
                if device.last_seen < cutoff
            ]
            for device_id in stale_ids:
                del self._devices[device_id]
