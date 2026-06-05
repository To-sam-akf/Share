from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "device_name": "PC-A",
    "udp_port": 9000,
    "tcp_port": 9001,
    "broadcast_ip": "255.255.255.255",
    "shared_folder": "./shared_folder",
    "chunk_size": 1024 * 1024,
    "enable_tls": False,
}


@dataclass(frozen=True)
class AppConfig:
    device_name: str
    udp_port: int
    tcp_port: int
    broadcast_ip: str
    shared_folder: Path
    chunk_size: int
    enable_tls: bool

    @classmethod
    def from_mapping(cls, values: dict[str, Any], base_dir: Path) -> "AppConfig":
        merged = DEFAULT_CONFIG | values
        shared_folder = Path(str(merged["shared_folder"]))
        if not shared_folder.is_absolute():
            shared_folder = base_dir / shared_folder

        return cls(
            device_name=str(merged["device_name"]),
            udp_port=int(merged["udp_port"]),
            tcp_port=int(merged["tcp_port"]),
            broadcast_ip=str(merged["broadcast_ip"]),
            shared_folder=shared_folder,
            chunk_size=int(merged["chunk_size"]),
            enable_tls=bool(merged["enable_tls"]),
        )


def ensure_default_config(path: str | Path = "config.json") -> Path:
    config_path = Path(path)
    if not config_path.exists():
        config_path.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return config_path


def load_config(path: str | Path = "config.json") -> AppConfig:
    config_path = ensure_default_config(path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    config = AppConfig.from_mapping(data, config_path.parent)
    config.shared_folder.mkdir(parents=True, exist_ok=True)
    return config
