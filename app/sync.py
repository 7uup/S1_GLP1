"""核心同步逻辑 — S1 uuid ↔ GLPI uuid / GLPI contact ↔ S1 externalId / 机器配置和历史使用者同步"""

from __future__ import annotations

import logging
from typing import Any

from app.cache import CacheDB
from app.config import get_config
from app.glpi_client import GLPIClient
from app.lark_notify import LarkNotifier
from app.models import BidirectionalSyncItem, BidirectionalSyncResult, S1Agent
from app.s1_client import S1Client

logger = logging.getLogger(__name__)


class SyncService:
    """资产同步服务 — 支持多 S1 控制台"""

    def __init__(self) -> None:
        config = get_config()
        self.s1_clients: list[S1Client] = [
            S1Client(cfg) for cfg in config.sentinelone_instances
        ]
        self.glpi = GLPIClient(config.glpi)
        self.lark = LarkNotifier(config.lark)
        self.cache = CacheDB(config.cache.db_path)
        self._running = False

    async def start(self) -> None:
        """初始化所有客户端"""
        await self.cache.init()
        for client in self.s1_clients:
            await client.init()
        await self.glpi.init()
        await self.lark.init()
        logger.info(
            "SyncService started with %d S1 console(s): %s",
            len(self.s1_clients),
            ", ".join(c.name for c in self.s1_clients),
        )

    async def stop(self) -> None:
        """关闭所有客户端"""
        for client in self.s1_clients:
            await client.close()
        await self.glpi.close()
        await self.lark.close()
        await self.cache.close()
        logger.info("SyncService stopped")

    async def run(self) -> dict[str, Any]:
        """执行一次完整同步，返回结果摘要"""
        if self._running:
            logger.warning("Sync already in progress, skipping")
            return {"status": "skipped", "reason": "already_running"}

        self._running = True
        try:
            return await self._do_sync()
        except Exception as e:
            logger.exception("Sync failed: %s", e)
            return {"status": "error", "error": str(e)}
        finally:
            self._running = False

    async def _do_sync(self) -> dict[str, Any]:
        """同步流程：S1 ↔ GLPI 字段回写和机器配置备注同步"""
        logger.info("=== Sync started ===")

        # 1. 从所有 S1 控制台拉取 Agent
        agents: list[S1Agent] = []
        for client in self.s1_clients:
            try:
                console_agents = await client.get_agents()
                agents.extend(console_agents)
                logger.info(
                    "Fetched %d agents from console [%s]",
                    len(console_agents), client.name,
                )
            except Exception as e:
                logger.error(
                    "Failed to fetch from console [%s]: %s",
                    client.name, e,
                )

        logger.info("Total agents from all consoles: %d", len(agents))

        # 2. 双向回写: S1 uuid → GLPI, GLPI contact → S1 externalId
        bi_result = None
        if get_config().bidirectional_sync.enabled:
            bi_result = await self._bidirectional_sync(agents)

        # 3. 记录日志
        if bi_result:
            detail = (
                f"consoles={len(self.s1_clients)}, "
                f"agents={len(agents)}, "
                f"uuid_ok={len(bi_result.uuid_synced)}, "
                f"uuid_fail={len(bi_result.uuid_failed)}, "
                f"contact_ok={len(bi_result.contact_synced)}, "
                f"contact_fail={len(bi_result.contact_failed)}, "
                f"config_ok={len(bi_result.config_synced)}, "
                f"config_fail={len(bi_result.config_failed)}, "
                f"history_ok={len(bi_result.historical_user_synced)}, "
                f"history_fail={len(bi_result.historical_user_failed)}"
            )
            await self.cache.log_sync(
                bi_result.total_ok,
                bi_result.total_fail,
                len(agents), detail,
            )

        result = {
            "status": "ok",
            "total_agents": len(agents),
        }
        if bi_result:
            result["bidirectional_sync"] = {
                "uuid_ok": len(bi_result.uuid_synced),
                "uuid_fail": len(bi_result.uuid_failed),
                "contact_ok": len(bi_result.contact_synced),
                "contact_fail": len(bi_result.contact_failed),
                "config_ok": len(bi_result.config_synced),
                "config_fail": len(bi_result.config_failed),
                "history_ok": len(bi_result.historical_user_synced),
                "history_fail": len(bi_result.historical_user_failed),
            }
        logger.info("=== Sync completed: %s ===", result)
        return result

    async def _bidirectional_sync(self, agents: list[S1Agent]) -> BidirectionalSyncResult:
        """双向回写：
        A. S1 agent uuid → GLPI Computer uuid
        B. GLPI contact → S1 agent externalId
        C. S1 CPU/内存 → GLPI Computer comment
        D. S1 externalId / GLPI contact → GLPI Computer Notepad

        安全机制：
        - 本地 SQLite 记录已同步的 serial，不依赖 S1 API 返回 externalId
        - dry_run=True:  只检测匹配关系，不执行 API 写入
        - overwrite_existing=False: 只写入空字段（默认安全策略）
        """
        config = get_config()
        bi_cfg = config.bidirectional_sync
        result = BidirectionalSyncResult()

        mode_tag = "[DRY-RUN]" if bi_cfg.dry_run else "[LIVE]"
        overwrite_tag = "[OVERWRITE]" if bi_cfg.overwrite_existing else "[empty-only]"
        logger.info("=== Bidirectional sync started %s %s ===", mode_tag, overwrite_tag)

        # 读取本地已同步记录（关键：不依赖 S1 API 返回 externalId）
        synced_contacts = await self.cache.get_bi_synced_contacts()
        synced_uuids = await self.cache.get_bi_synced_uuids()
        logger.info(
            "  Synced records in cache: contacts=%d, uuids=%d",
            len(synced_contacts), len(synced_uuids),
        )

        # 获取所有 GLPI Computer（id, serial, uuid, contact）
        glpi_computers = await self.glpi.get_all_computers_full()

        # 构建 serial → GLPI 信息映射
        glpi_by_serial: dict[str, dict] = {}
        for comp in glpi_computers:
            serial = (comp.get("serial") or "").strip()
            if serial:
                glpi_by_serial[serial] = {
                    "id": comp.get("id"),
                    "name": comp.get("name", ""),
                    "uuid": (comp.get("uuid") or "").strip(),
                    "contact": (comp.get("contact") or "").strip(),
                    "comment": (comp.get("comment") or "").strip(),
                }

        # 构建 agent_id → S1Client 映射（用于后续写入 externalId）
        agent_to_client: dict[str, int] = {}  # agent_id -> s1_client index
        for i, client in enumerate(self.s1_clients):
            for a in agents:
                if a.console_name == client.name:
                    agent_to_client[a.agent_id] = i

        # ── A. S1 uuid → GLPI uuid ──────────────────────────
        if bi_cfg.uuid_to_glpi:
            logger.info("  A) Syncing S1 uuid → GLPI uuid %s...", mode_tag)
            for agent in agents:
                if not agent.uuid or not agent.serial_number:
                    continue
                serial = agent.serial_number.strip()
                glpi_info = glpi_by_serial.get(serial)
                if not glpi_info:
                    continue

                # 本地缓存已记录，跳过
                if serial in synced_uuids and not bi_cfg.overwrite_existing:
                    continue

                # GLPI 已有值且不允许覆盖，跳过
                if glpi_info["uuid"] and not bi_cfg.overwrite_existing:
                    # 也记入缓存（GLPI 手动填的也算已同步）
                    await self.cache.set_bi_synced_uuid(serial, agent.uuid)
                    synced_uuids.add(serial)
                    continue

                item = BidirectionalSyncItem(
                    serial=serial,
                    computer_name=agent.computer_name,
                    console_name=agent.console_name,
                    glpi_id=glpi_info["id"],
                    s1_uuid=agent.uuid,
                )

                # dry_run: 只记录，不写入
                if bi_cfg.dry_run:
                    logger.info(
                        "[%s]%s GLPI computer %d (%s) uuid: %s",
                        agent.console_name, mode_tag,
                        glpi_info["id"], agent.computer_name, agent.uuid,
                    )
                    result.uuid_synced.append(item)
                    continue

                # 真正写入
                try:
                    await self.glpi.set_computer_uuid(glpi_info["id"], agent.uuid)
                    await self.cache.set_bi_synced_uuid(serial, agent.uuid)
                    synced_uuids.add(serial)
                    result.uuid_synced.append(item)
                except Exception as e:
                    item.success = False
                    item.error = str(e)
                    result.uuid_failed.append(item)
                    logger.error(
                        "[%s] Failed to set uuid for GLPI %d (%s): %s",
                        agent.console_name, glpi_info["id"],
                        agent.computer_name, e,
                    )

        # ── B. GLPI contact → S1 externalId ──────────────────
        if bi_cfg.contact_to_s1:
            logger.info("  B) Syncing GLPI contact → S1 externalId %s...", mode_tag)
            for agent in agents:
                if not agent.serial_number:
                    continue
                serial = agent.serial_number.strip()
                glpi_info = glpi_by_serial.get(serial)
                if not glpi_info:
                    continue
                contact = glpi_info["contact"]
                if not contact:
                    continue

                # 本地缓存已记录，跳过（关键修复：不依赖 S1 API 返回 externalId）
                if serial in synced_contacts and not bi_cfg.overwrite_existing:
                    continue

                # S1 侧已有值且不允许覆盖，也记入缓存
                if agent.external_id and not bi_cfg.overwrite_existing:
                    await self.cache.set_bi_synced_contact(serial, agent.external_id)
                    synced_contacts.add(serial)
                    continue

                item = BidirectionalSyncItem(
                    serial=serial,
                    computer_name=agent.computer_name,
                    console_name=agent.console_name,
                    glpi_id=glpi_info["id"],
                    glpi_contact=contact,
                )

                # 找到对应的 S1 客户端
                client_idx = agent_to_client.get(agent.agent_id)
                if client_idx is None:
                    item.success = False
                    item.error = f"No S1 client found for console {agent.console_name}"
                    result.contact_failed.append(item)
                    continue

                # dry_run: 只记录，不写入
                if bi_cfg.dry_run:
                    logger.info(
                        "[%s]%s S1 agent %s externalId <- %s",
                        agent.console_name, mode_tag,
                        agent.computer_name, contact,
                    )
                    result.contact_synced.append(item)
                    continue

                # 真正写入
                try:
                    await self.s1_clients[client_idx].set_external_id(
                        agent.agent_id, contact,
                    )
                    await self.cache.set_bi_synced_contact(serial, contact)
                    synced_contacts.add(serial)
                    result.contact_synced.append(item)
                except Exception as e:
                    item.success = False
                    item.error = str(e)
                    result.contact_failed.append(item)
                    logger.error(
                        "[%s] Failed to set externalId for agent %s: %s",
                        agent.console_name, agent.computer_name, e,
                    )

        # ── C. S1 机器配置 → GLPI 备注 ───────────────────────
        if bi_cfg.machine_config_to_glpi:
            logger.info("  C) Syncing S1 machine config → GLPI comment %s...", mode_tag)
            for agent in agents:
                if not agent.serial_number:
                    continue
                machine_config = agent.machine_config_comment()
                if not machine_config:
                    continue

                serial = agent.serial_number.strip()
                glpi_info = glpi_by_serial.get(serial)
                if not glpi_info:
                    continue

                current_comment = glpi_info["comment"]
                new_comment = self._merge_machine_config_comment(
                    current_comment, machine_config,
                )
                if new_comment == current_comment:
                    continue

                item = BidirectionalSyncItem(
                    serial=serial,
                    computer_name=agent.computer_name,
                    console_name=agent.console_name,
                    glpi_id=glpi_info["id"],
                    machine_config=machine_config,
                )

                # dry_run: 只记录，不写入
                if bi_cfg.dry_run:
                    logger.info(
                        "[%s]%s GLPI computer %d (%s) comment <- %s",
                        agent.console_name, mode_tag,
                        glpi_info["id"], agent.computer_name,
                        machine_config.replace("\n", " | "),
                    )
                    result.config_synced.append(item)
                    continue

                # 真正写入
                try:
                    await self.glpi.set_computer_comment(glpi_info["id"], new_comment)
                    glpi_info["comment"] = new_comment
                    result.config_synced.append(item)
                except Exception as e:
                    item.success = False
                    item.error = str(e)
                    result.config_failed.append(item)
                    logger.error(
                        "[%s] Failed to set comment for GLPI %d (%s): %s",
                        agent.console_name, glpi_info["id"],
                        agent.computer_name, e,
                    )

        # ── D. S1 externalId / GLPI contact → GLPI Notepad ─
        if bi_cfg.historical_user_to_glpi:
            logger.info("  D) Syncing historical user → GLPI Notepad %s...", mode_tag)
            for agent in agents:
                if not agent.serial_number:
                    continue

                serial = agent.serial_number.strip()
                glpi_info = glpi_by_serial.get(serial)
                if not glpi_info:
                    continue

                historical_user, historical_user_source = agent.historical_user_source(
                    glpi_info["contact"]
                )
                if not historical_user:
                    continue

                content = f"历史使用者：{historical_user}"
                item = BidirectionalSyncItem(
                    serial=serial,
                    computer_name=agent.computer_name,
                    console_name=agent.console_name,
                    glpi_id=glpi_info["id"],
                    historical_user=historical_user,
                    historical_user_source=historical_user_source,
                )

                try:
                    notes = await self.glpi.get_computer_notepads(glpi_info["id"])
                    if any(content == (n.get("content") or "").strip() for n in notes):
                        continue

                    # dry_run: 只记录，不写入
                    if bi_cfg.dry_run:
                        logger.info(
                            "[%s]%s GLPI computer %d (%s) notepad <- %s (%s)",
                            agent.console_name, mode_tag,
                            glpi_info["id"], agent.computer_name,
                            content, historical_user_source,
                        )
                        result.historical_user_synced.append(item)
                        continue

                    await self.glpi.add_computer_notepad(glpi_info["id"], content)
                    result.historical_user_synced.append(item)
                except Exception as e:
                    item.success = False
                    item.error = str(e)
                    result.historical_user_failed.append(item)
                    logger.error(
                        "[%s] Failed to add historical user for GLPI %d (%s): %s",
                        agent.console_name, glpi_info["id"],
                        agent.computer_name, e,
                    )

        # ── 通知 ─────────────────────────────────────────────
        await self.lark.notify_bidirectional(result)

        logger.info(
            "=== Sync done %s: uuid_ok=%d/uuid_fail=%d, contact_ok=%d/contact_fail=%d, config_ok=%d/config_fail=%d, history_ok=%d/history_fail=%d ===",
            mode_tag,
            len(result.uuid_synced), len(result.uuid_failed),
            len(result.contact_synced), len(result.contact_failed),
            len(result.config_synced), len(result.config_failed),
            len(result.historical_user_synced), len(result.historical_user_failed),
        )
        return result

    @staticmethod
    def _merge_machine_config_comment(existing_comment: str, machine_config: str) -> str:
        """替换已有 CPU/内存行，同时保留其它备注内容"""
        machine_config = machine_config.strip()
        existing_comment = (existing_comment or "").replace("\r\n", "\n").strip()
        if not existing_comment:
            return machine_config

        preserved_lines: list[str] = []
        for line in existing_comment.splitlines():
            stripped = line.strip()
            if stripped.startswith(("CPU:", "CPU：", "内存:", "内存：")):
                continue
            preserved_lines.append(line)

        preserved = "\n".join(preserved_lines).strip()
        return f"{machine_config}\n\n{preserved}" if preserved else machine_config
