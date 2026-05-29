"""定时调度 — 使用 APScheduler 定期触发同步"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_config
from app.sync import SyncService
from app.token_manager import TokenManager

logger = logging.getLogger(__name__)


class SyncScheduler:
    """同步任务调度器（含 token 刷新检查）"""

    def __init__(self) -> None:
        self.config = get_config()
        self.service = SyncService()
        self._token_manager = TokenManager(
            self.config.token_refresh,
            config_path="config.yaml",
        )
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._started = False

    async def start(self) -> None:
        """启动调度器和同步服务"""
        await self.service.start()

        # 同步任务
        interval = self.config.scheduler.interval_seconds
        self._scheduler.add_job(
            self._run_sync,
            trigger=IntervalTrigger(seconds=interval),
            id="sync_job",
            name="S1 → GLPI Sync",
            max_instances=1,
            replace_existing=True,
        )

        # Token 过期检查任务
        if self.config.token_refresh.enabled:
            check_hours = self.config.token_refresh.check_interval_hours
            self._scheduler.add_job(
                self._run_token_check,
                trigger=IntervalTrigger(hours=check_hours),
                id="token_check_job",
                name="S1 Token Refresh Check",
                max_instances=1,
                replace_existing=True,
            )
            logger.info(
                "Token refresh check scheduled every %dh, threshold=%dd",
                check_hours, self.config.token_refresh.threshold_days,
            )

        self._scheduler.start()
        self._started = True
        logger.info(
            "Scheduler started, sync interval: %ds, next run: %s",
            interval,
            datetime.utcnow().isoformat(),
        )

        # 启动时检查
        if self.config.scheduler.run_on_start:
            logger.info("Run-on-start triggered")
            asyncio.create_task(self._run_sync())
        # 启动时检查 token（延迟一点，等 sync 先初始化）
        if self.config.token_refresh.enabled:
            asyncio.create_task(self._delayed_token_check())

    async def _delayed_token_check(self) -> None:
        """延迟 10 秒后检查 token（等 sync service 初始化完毕）"""
        await asyncio.sleep(10)
        await self._run_token_check()

    async def stop(self) -> None:
        """停止调度器"""
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False
        await self.service.stop()
        logger.info("Scheduler stopped")

    async def _run_sync(self) -> None:
        """执行同步（包装异常防止调度器崩溃）"""
        try:
            result = await self.service.run()
            logger.info("Scheduled sync result: %s", result)
        except Exception as e:
            logger.exception("Scheduled sync error: %s", e)

    async def _run_token_check(self) -> None:
        """检查并刷新 S1 token，成功后热重载客户端"""
        try:
            results = await self._token_manager.refresh_all()
            refreshed = [r for r in results if r.get("success") and not r.get("skipped")]
            skipped = [r for r in results if r.get("skipped")]
            failed = [r for r in results if not r.get("success")]

            if refreshed:
                # 热重载 S1Client token（无需重启）
                for r in refreshed:
                    name = r["name"]
                    for client in self.service.s1_clients:
                        if client.name == name:
                            # 从重载后的 config 取新 token
                            new_cfg = get_config()
                            for s1_cfg in new_cfg.sentinelone_instances:
                                if s1_cfg.name == name:
                                    await client.reload_token(s1_cfg.api_token)
                                    break
                            break

                # 发送飞书通知
                names = ", ".join(r["name"] for r in refreshed)
                text = (
                    f"S1 API Token 已自动刷新: {names}\n"
                    + "\n".join(
                        f"  [{r['name']}] 新过期: {r.get('new_exp', '?')}"
                        for r in refreshed
                    )
                )
                logger.info(text)
                try:
                    await self.service.lark.send_text(f"[Token] {text}")
                except Exception:
                    pass

            if failed:
                names = ", ".join(r["name"] for r in failed)
                text = (
                    f"\u26a0\ufe0f S1 API Token 刷新失败: {names}\n"
                    + "\n".join(
                        f"  [{r['name']}] {r.get('error', '?')}"
                        for r in failed
                    )
                )
                logger.error(text)
                try:
                    await self.service.lark.send_text(f"[Token] {text}")
                except Exception:
                    pass

            if not refreshed and not failed:
                logger.debug("Token check: all tokens OK")

        except Exception as e:
            logger.exception("Token check error: %s", e)

    def get_next_run_time(self) -> datetime | None:
        job = self._scheduler.get_job("sync_job")
        return job.next_run_time if job else None

    async def trigger_manual(self) -> dict:
        """手动触发一次同步"""
        return await self.service.run()

    async def trigger_token_refresh(self) -> list[dict]:
        """手动触发 token 刷新"""
        return await self._token_manager.refresh_all()
