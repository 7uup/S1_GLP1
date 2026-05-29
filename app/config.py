"""配置加载模块 — 读取 config.yaml 并提供全局配置对象"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# ── 子配置模型 ──────────────────────────────────────────────

class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class SchedulerConfig(BaseModel):
    interval_seconds: int = 300
    run_on_start: bool = True


class SentinelOneConfig(BaseModel):
    """单个 S1 控制台配置"""
    name: str = "default"          # 控制台标识，如 HK / SZ
    base_url: str = "https://usea1.sentinelone.net"
    api_token: str = ""
    page_size: int = 500


class GLPIConfig(BaseModel):
    base_url: str = ""
    app_token: str = ""
    user_token: str = ""
    field_mapping: dict[str, str] = Field(default_factory=lambda: {
        "computerName": "name",
        "osName": "operatingsystems_id",
        "domain": "domains_id",
        "modelName": "computermodels_id",
        "serialNumber": "serial",
        "ip": "ip_address",
        "agentVersion": "comment",
    })


class LarkConfig(BaseModel):
    webhook_url: str = ""
    enabled: bool = False
    notify_types: list[str] = Field(default_factory=lambda: ["new_asset", "changed_asset", "bidirectional_sync"])


class BidirectionalSyncConfig(BaseModel):
    """双向回写配置：S1 uuid → GLPI, GLPI contact → S1 externalId"""
    enabled: bool = False         # 总开关
    dry_run: bool = True          # True=只检测不写入，False=真正执行
    overwrite_existing: bool = False  # True=覆盖已有值，False=只写空字段（安全默认）
    uuid_to_glpi: bool = True     # S1 agent uuid → GLPI Computer uuid
    contact_to_s1: bool = True    # GLPI contact → S1 agent externalId


class TokenRefreshConfig(BaseModel):
    """S1 API Token 自动刷新配置"""
    enabled: bool = True           # 是否启用自动刷新
    threshold_days: int = 7        # 剩余天数 < 此值时触发刷新
    check_interval_hours: int = 24 # 检查间隔（小时）


class CacheConfig(BaseModel):
    db_path: str = "./data/cache.db"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    # 多 S1 控制台，默认至少一个
    sentinelone_instances: list[SentinelOneConfig] = Field(
        default_factory=lambda: [SentinelOneConfig()]
    )
    glpi: GLPIConfig = Field(default_factory=GLPIConfig)
    lark: LarkConfig = Field(default_factory=LarkConfig)
    bidirectional_sync: BidirectionalSyncConfig = Field(default_factory=BidirectionalSyncConfig)
    token_refresh: TokenRefreshConfig = Field(default_factory=TokenRefreshConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# ── 加载逻辑 ────────────────────────────────────────────────

_CONFIG: AppConfig | None = None


def load_config(path: str | Path | None = None) -> AppConfig:
    """加载 YAML 配置文件并返回 AppConfig 对象"""
    global _CONFIG

    if path is None:
        path = os.getenv("SYNC_CONFIG", "config.yaml")

    path = Path(path)
    raw: dict[str, Any] = {}

    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    # 向后兼容：旧配置用 sentinelone（单对象），自动转为 sentinelone_instances（列表）
    if "sentinelone" in raw and "sentinelone_instances" not in raw:
        raw["sentinelone_instances"] = [raw.pop("sentinelone")]

    _CONFIG = AppConfig(**raw)
    return _CONFIG


def get_config() -> AppConfig:
    """获取当前配置，如果未加载则先加载"""
    if _CONFIG is None:
        return load_config()
    return _CONFIG
