"""FastAPI HTTP 接口 — 健康检查、手动触发、同步状态"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.scheduler import SyncScheduler

logger = logging.getLogger(__name__)

app = FastAPI(
    title="S1-GLPI Sync Service",
    version="1.0.0",
    description="SentinelOne → GLPI 资产同步服务",
)

# 全局调度器实例
_scheduler: SyncScheduler | None = None


def set_scheduler(scheduler: SyncScheduler) -> None:
    global _scheduler
    _scheduler = scheduler


class SyncResponse(BaseModel):
    status: str
    total_agents: int = 0
    new_assets: int = 0
    changed_assets: int = 0
    glpi_synced: int = 0
    glpi_failed: int = 0
    error: str = ""


# ── 路由 ────────────────────────────────────────────────────


@app.get("/health", tags=["监控"])
async def health():
    """健康检查"""
    return {"status": "ok"}


@app.post("/sync", response_model=SyncResponse, tags=["同步"])
async def trigger_sync():
    """手动触发一次同步"""
    if not _scheduler:
        return SyncResponse(status="error", error="scheduler not initialized")

    result = await _scheduler.trigger_manual()
    return SyncResponse(**result)


@app.get("/status", tags=["监控"])
async def get_status() -> dict[str, Any]:
    """获取服务状态"""
    if not _scheduler:
        return {"status": "not_initialized"}

    next_run = _scheduler.get_next_run_time()

    # Token 过期信息
    from app.token_manager import TokenManager
    from app.config import get_config
    cfg = get_config()
    tokens_info = []
    for s1_cfg in cfg.sentinelone_instances:
        exp, payload = TokenManager.decode_jwt(s1_cfg.api_token)
        remaining = TokenManager.remaining_days(exp)
        tokens_info.append({
            "console": s1_cfg.name,
            "remaining_days": remaining,
            "sub": payload.get("sub", ""),
        })

    return {
        "status": "running" if _scheduler._started else "stopped",
        "next_sync": next_run.isoformat() if next_run else None,
        "interval_seconds": _scheduler.config.scheduler.interval_seconds,
        "tokens": tokens_info,
    }


@app.post("/refresh-tokens", tags=["同步"])
async def refresh_tokens():
    """手动触发 S1 API Token 刷新检查"""
    if not _scheduler:
        return {"status": "error", "error": "scheduler not initialized"}

    results = await _scheduler.trigger_token_refresh()
    return {"status": "ok", "results": results}






