"""Carga de la configuración de hosts a monitorear (YAML)."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from app.monitoring.collector import LocalCollector
from app.monitoring.remote import SshCollector


class ThresholdConfig(BaseModel):
    warning: float
    critical: float


class HostConfig(BaseModel):
    name: str
    type: Literal["local", "ssh"] = "local"
    address: str | None = None
    port: int = 22
    username: str | None = None
    key_file: str | None = None
    strict_host_keys: bool = True
    services: list[str] = Field(default_factory=list)
    disks: list[str] | None = None
    # Overrides por host de los umbrales globales: {"cpu": {...}, "memory": {...}, "disk": {...}}
    thresholds: dict[str, ThresholdConfig] = Field(default_factory=dict)


class HostsFile(BaseModel):
    hosts: list[HostConfig]


def load_hosts(path: Path) -> list[HostConfig]:
    """Lee el YAML de hosts. Si no existe, monitorea solo el host local."""
    if not path.exists():
        return [HostConfig(name=socket.gethostname(), type="local")]
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return HostsFile.model_validate(data).hosts


def build_collector(cfg: HostConfig) -> LocalCollector | SshCollector:
    if cfg.type == "local":
        return LocalCollector(cfg.name, services=cfg.services, disk_paths=cfg.disks)
    if not cfg.address or not cfg.username:
        raise ValueError(f"El host SSH '{cfg.name}' necesita address y username")
    return SshCollector(
        cfg.name,
        cfg.address,
        cfg.username,
        port=cfg.port,
        key_file=cfg.key_file,
        services=cfg.services,
        disks=cfg.disks,
        strict_host_keys=cfg.strict_host_keys,
    )
