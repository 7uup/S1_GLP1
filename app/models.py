"""数据模型 — S1 Agent / GLPI Computer / 变更记录"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ChangeType(str, Enum):
    NEW = "new_asset"
    CHANGED = "changed_asset"


@dataclass
class S1Agent:
    """SentinelOne Agent 核心字段"""
    agent_id: str
    computer_name: str
    console_name: str = ""       # 所属 S1 控制台标识（HK / SZ 等）
    os_name: str = ""
    domain: str = ""
    model_name: str = ""
    serial_number: str = ""
    ip: str = ""
    agent_version: str = ""
    is_active: bool = True
    external_id: str = ""        # S1 externalId（双向同步：GLPI contact → 此处）
    uuid: str = ""               # S1 agent uuid（双向同步：此处 → GLPI uuid）
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any], console_name: str = "") -> S1Agent:
        """从 S1 API 响应构建对象"""
        network_info = data.get("networkInterfaces") or []
        primary_ip = ""
        if network_info:
            primary_ip = network_info[0].get("inet", [])

        if isinstance(primary_ip, list):
            primary_ip = primary_ip[0] if primary_ip else ""

        return cls(
            agent_id=str(data.get("id") or ""),
            computer_name=data.get("computerName") or "",
            console_name=console_name,
            os_name=data.get("osName") or "",
            domain=data.get("domain") or "",
            model_name=data.get("modelName") or "",
            serial_number=data.get("serialNumber") or "",
            ip=primary_ip,
            agent_version=data.get("agentVersion") or "",
            is_active=data.get("isActive", True),
            external_id=(data.get("externalId") or ""),
            uuid=(data.get("uuid") or ""),
            raw=data,
        )

    def to_cache_dict(self) -> dict[str, Any]:
        """序列化为缓存存储格式"""
        return {
            "agent_id": self.agent_id,
            "computer_name": self.computer_name,
            "console_name": self.console_name,
            "os_name": self.os_name,
            "domain": self.domain,
            "model_name": self.model_name,
            "serial_number": self.serial_number,
            "ip": self.ip,
            "agent_version": self.agent_version,
            "is_active": self.is_active,
            "external_id": self.external_id,
            "uuid": self.uuid,
        }


@dataclass
class AssetChange:
    """资产变更记录"""
    agent_id: str
    computer_name: str
    console_name: str = ""       # 所属 S1 控制台标识
    change_type: ChangeType = ChangeType.NEW
    changes: dict[str, tuple[str, str]] = field(default_factory=dict)
    # changes: {"field": ("old_value", "new_value")}

    @property
    def is_new(self) -> bool:
        return self.change_type == ChangeType.NEW

    def summary(self) -> str:
        source = f"[{self.console_name}] " if self.console_name else ""
        if self.is_new:
            return f"{source}新增资产: {self.computer_name} (ID: {self.agent_id})"
        parts = [f"{k}: {v[0]} → {v[1]}" for k, v in self.changes.items()]
        return f"{source}资产变更: {self.computer_name} (ID: {self.agent_id})\n  " + "\n  ".join(parts)


@dataclass
class BidirectionalSyncItem:
    """单条双向同步记录"""
    serial: str
    computer_name: str
    console_name: str         # 所属 S1 控制台
    glpi_id: int
    s1_uuid: str = ""          # S1 agent uuid
    glpi_contact: str = ""     # GLPI contact（使用者）
    success: bool = True
    error: str = ""


@dataclass
class BidirectionalSyncResult:
    """双向同步结果汇总"""
    uuid_synced: list[BidirectionalSyncItem] = field(default_factory=list)
    uuid_failed: list[BidirectionalSyncItem] = field(default_factory=list)
    contact_synced: list[BidirectionalSyncItem] = field(default_factory=list)
    contact_failed: list[BidirectionalSyncItem] = field(default_factory=list)

    @property
    def total_ok(self) -> int:
        return len(self.uuid_synced) + len(self.contact_synced)

    @property
    def total_fail(self) -> int:
        return len(self.uuid_failed) + len(self.contact_failed)

    @property
    def has_any(self) -> bool:
        return self.total_ok > 0 or self.total_fail > 0
