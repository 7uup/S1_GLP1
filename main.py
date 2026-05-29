#!/usr/bin/env python3
"""
SentinelOne → GLPI 资产同步服务 — 入口

启动方式：
    python main.py
    # 或
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from app.api import app, set_scheduler
from app.config import load_config
from app.scheduler import SyncScheduler


def setup_logging() -> None:
    """配置日志"""
    config = load_config()
    log_cfg = config.logging
    logging.basicConfig(
        level=getattr(logging, log_cfg.level.upper(), logging.INFO),
        format=log_cfg.format,
        stream=sys.stdout,
    )
    # 降低第三方库日志级别
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# 全局调度器
scheduler: SyncScheduler | None = None


@app.on_event("startup")
async def startup():
    global scheduler
    config = load_config()
    scheduler = SyncScheduler()
    set_scheduler(scheduler)
    await scheduler.start()


@app.on_event("shutdown")
async def shutdown():
    global scheduler
    if scheduler:
        await scheduler.stop()


def main():
    setup_logging()
    config = load_config()
    uvicorn.run(
        "main:app",
        host=config.server.host,
        port=config.server.port,
        log_level=config.logging.level.lower(),
        # 单 worker 即可，单机部署
        workers=1,
    )


if __name__ == "__main__":
    main()
