from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


DEFAULT_CONFIG: dict[str, Any] = {
    "device_name": "PC-A",
    "udp_port": 9000,
    "tcp_port": 9001,
    "broadcast_ip": "255.255.255.255",
    "shared_folder": "./shared_folder",
    "chunk_size": 1024 * 1024,
    "enable_tls": True,
    "sync_enabled": True,
    "sync_interval_seconds": 10.0,
    "agent_api_url": "https://api.deepseek.com",
    "agent_provider": "deepseek",
    "agent_model": "deepseek-v4-flash",
    "agent_timeout_seconds": 20.0,
    "security_audit_interval_seconds": 600.0,
    "audit_log_retention_days": 30,
    "shared_size_risk_bytes": 10 * 1024 * 1024 * 1024,
    "shared_file_count_risk": 10_000,
    "web_port": 8765,
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
    sync_enabled: bool
    sync_interval_seconds: float
    agent_api_url: str
    agent_provider: str
    agent_model: str
    agent_timeout_seconds: float
    security_audit_interval_seconds: float
    audit_log_retention_days: int
    shared_size_risk_bytes: int
    shared_file_count_risk: int
    web_port: int

    @classmethod
    def from_mapping(cls, values: dict[str, Any], base_dir: Path) -> "AppConfig":
        merged = DEFAULT_CONFIG | values
        shared_folder = Path(str(merged["shared_folder"]))
        if not shared_folder.is_absolute():
            shared_folder = base_dir / shared_folder

        sync_interval_seconds = float(merged["sync_interval_seconds"])
        if sync_interval_seconds <= 0:
            raise ValueError("sync_interval_seconds must be positive")
        agent_timeout_seconds = float(merged["agent_timeout_seconds"])
        audit_interval = float(merged["security_audit_interval_seconds"])
        retention_days = int(merged["audit_log_retention_days"])
        size_risk_bytes = int(merged["shared_size_risk_bytes"])
        file_count_risk = int(merged["shared_file_count_risk"])
        web_port = int(merged["web_port"])
        if agent_timeout_seconds <= 0 or audit_interval <= 0:
            raise ValueError("agent and audit intervals must be positive")
        if retention_days <= 0 or size_risk_bytes <= 0 or file_count_risk <= 0:
            raise ValueError("audit retention and risk thresholds must be positive")
        if not 1 <= web_port <= 65535:
            raise ValueError("web_port must be between 1 and 65535")
        agent_provider = str(merged["agent_provider"]).lower()
        if agent_provider not in {"deepseek", "openai"}:
            raise ValueError("agent_provider must be deepseek or openai")

        return cls(
            device_name=str(merged["device_name"]),
            udp_port=int(merged["udp_port"]),
            tcp_port=int(merged["tcp_port"]),
            broadcast_ip=str(merged["broadcast_ip"]),
            shared_folder=shared_folder,
            chunk_size=int(merged["chunk_size"]),
            enable_tls=bool(merged["enable_tls"]),
            sync_enabled=bool(merged["sync_enabled"]),
            sync_interval_seconds=sync_interval_seconds,
            agent_api_url=str(merged["agent_api_url"]),
            agent_provider=agent_provider,
            agent_model=str(merged["agent_model"]),
            agent_timeout_seconds=agent_timeout_seconds,
            security_audit_interval_seconds=audit_interval,
            audit_log_retention_days=retention_days,
            shared_size_risk_bytes=size_risk_bytes,
            shared_file_count_risk=file_count_risk,
            web_port=web_port,
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
    load_dotenv(config_path.parent / ".env", override=False)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    config = AppConfig.from_mapping(data, config_path.parent)
    config.shared_folder.mkdir(parents=True, exist_ok=True)
    return config
