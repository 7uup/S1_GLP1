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
    memory: str = ""             # 机器内存，如 32 GB
    cpu_count: str = ""          # CPU 数量
    cpu_type: str = ""           # CPU 型号
    core_count: str = ""         # CPU 核心数
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
            memory=cls._first_str(
                data, "memory", "Memory", "totalMemory", "memorySize", "ramSize",
            ),
            cpu_count=cls._first_str(
                data, "cpuCount", "CPU Count", "cpu_count", "numberOfProcessors",
            ),
            cpu_type=cls._first_str(
                data, "cpuId", "cpuType", "CPU Type", "cpuModel", "processorType", "processorName",
            ),
            core_count=cls._first_str(
                data, "coreCount", "Core Count", "core_count", "numberOfCores",
            ),
            is_active=data.get("isActive", True),
            external_id=(data.get("externalId") or ""),
            uuid=(data.get("uuid") or ""),
            raw=data,
        )

    @staticmethod
    def _first_str(data: dict[str, Any], *keys: str) -> str:
        """按多个可能的 S1 字段名取第一个非空值"""
        for key in keys:
            value = data.get(key)
            if value is None or value == "":
                continue
            return str(value).strip()
        return ""

    def machine_config_comment(self) -> str:
        """生成写入 GLPI 备注的机器配置内容"""
        if not any([self.cpu_type, self.cpu_count, self.core_count, self.memory]):
            return ""

        cpu = self.cpu_type.strip()
        details: list[str] = []
        if self.cpu_count:
            details.append(f"{self.cpu_count} CPU")
        if self.core_count:
            details.append(f"{self.core_count} 核")

        if details:
            cpu = f"{cpu} ({' / '.join(details)})" if cpu else " / ".join(details)

        return f"CPU: {cpu or '-'}\n内存：{self._format_memory(self.memory) or '-'}"

    @staticmethod
    def _format_memory(memory: str) -> str:
        """S1 API 的 totalMemory 通常是 MB，备注中统一展示为 GB"""
        memory = (memory or "").strip()
        if not memory:
            return ""
        if any(unit in memory.lower() for unit in ["gb", "mb", "tb"]):
            return memory
        try:
            mb = float(memory)
        except ValueError:
            return memory
        if mb <= 0:
            return memory
        if mb >= 1024:
            return f"{round(mb / 1024)} GB"
        return f"{round(mb)} MB"

    def historical_user_source(self, glpi_contact: str = "") -> tuple[str, str]:
        """返回历史使用者及来源：优先 S1 externalId，缺失时用 GLPI contact"""
        external_id = self.external_id.strip()
        if external_id:
            return external_id, "S1 externalId"

        contact = glpi_contact.strip()
        if contact:
            return contact, "GLPI contact"

        return "", ""

    def historical_user(self, glpi_contact: str = "") -> str:
        """返回历史使用者基准值"""
        user, _ = self.historical_user_source(glpi_contact)
        return user

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
            "memory": self.memory,
            "cpu_count": self.cpu_count,
            "cpu_type": self.cpu_type,
            "core_count": self.core_count,
            "machine_config": self.machine_config_comment(),
            "historical_user": self.historical_user(),
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
    machine_config: str = ""   # 写入 GLPI comment 的机器配置
    historical_user: str = ""   # 写入 GLPI Notepad 的历史使用者
    historical_user_source: str = ""  # 历史使用者来源
    success: bool = True
    error: str = ""


@dataclass
class BidirectionalSyncResult:
    """双向同步结果汇总"""
    uuid_synced: list[BidirectionalSyncItem] = field(default_factory=list)
    uuid_failed: list[BidirectionalSyncItem] = field(default_factory=list)
    contact_synced: list[BidirectionalSyncItem] = field(default_factory=list)
    contact_failed: list[BidirectionalSyncItem] = field(default_factory=list)
    config_synced: list[BidirectionalSyncItem] = field(default_factory=list)
    config_failed: list[BidirectionalSyncItem] = field(default_factory=list)
    historical_user_synced: list[BidirectionalSyncItem] = field(default_factory=list)
    historical_user_failed: list[BidirectionalSyncItem] = field(default_factory=list)

    @property
    def total_ok(self) -> int:
        return (
            len(self.uuid_synced)
            + len(self.contact_synced)
            + len(self.config_synced)
            + len(self.historical_user_synced)
        )

    @property
    def total_fail(self) -> int:
        return (
            len(self.uuid_failed)
            + len(self.contact_failed)
            + len(self.config_failed)
            + len(self.historical_user_failed)
        )

    @property
    def has_any(self) -> bool:
        return self.total_ok > 0 or self.total_fail > 0
