"""飞书/Lark Webhook 通知模块"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import LarkConfig
from app.models import AssetChange, BidirectionalSyncResult, ChangeType

logger = logging.getLogger(__name__)


class LarkNotifier:
    """飞书自定义机器人 Webhook 通知"""

    def __init__(self, config: LarkConfig) -> None:
        self.webhook_url = config.webhook_url
        self.enabled = config.enabled
        self.notify_types = set(config.notify_types)
        self._client: httpx.AsyncClient | None = None

    async def init(self) -> None:
        self._client = httpx.AsyncClient(timeout=10.0)
        logger.info(
            "LarkNotifier initialized (enabled=%s, url=%s...)",
            self.enabled,
            self.webhook_url[:50] if self.webhook_url else "N/A",
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def notify(self, changes: list[AssetChange]) -> None:
        """发送变更通知"""
        if not self.enabled or not self.webhook_url or not changes:
            return

        # 过滤掉不在 notify_types 里的类型
        filtered = [c for c in changes if c.change_type.value in self.notify_types]
        if not filtered:
            return

        # 分类型汇总
        new_assets = [c for c in filtered if c.is_new]
        changed_assets = [c for c in filtered if not c.is_new]

        # 发送汇总消息
        if new_assets:
            await self._send_card(
                title="🆕 SentinelOne 新增资产",
                items=new_assets,
                color="green",
            )

        if changed_assets:
            await self._send_card(
                title="🔄 SentinelOne 资产变更",
                items=changed_assets,
                color="orange",
            )

    async def send_text(self, text: str) -> bool:
        """发送简单文本消息"""
        if not self.enabled or not self.webhook_url or not self._client:
            return False

        try:
            resp = await self._client.post(
                self.webhook_url,
                json={
                    "msg_type": "text",
                    "content": {"text": text},
                },
            )
            resp.raise_for_status()
            return resp.json().get("code", -1) == 0
        except Exception as e:
            logger.error("Lark text notification failed: %s", e)
            return False

    async def notify_bidirectional(self, result: BidirectionalSyncResult) -> None:
        """发送双向同步结果通知"""
        if not self.enabled or not self.webhook_url or not result.has_any:
            return
        if "bidirectional_sync" not in self.notify_types:
            return

        # 读取 dry_run 状态
        from app.config import get_config
        bi_cfg = get_config().bidirectional_sync
        dry_tag = " [DRY-RUN - 未实际写入]" if bi_cfg.dry_run else ""
        overwrite_tag = " [允许覆盖]" if bi_cfg.overwrite_existing else " [仅空字段]"

        lines: list[str] = []
        if result.uuid_synced:
            lines.append(f"**S1 uuid → GLPI uuid ({len(result.uuid_synced)} 台):**")
            for item in result.uuid_synced[:10]:
                lines.append(f"• [{item.console_name}] {item.computer_name}")
                lines.append(f"  GLPI ID={item.glpi_id}, uuid={item.s1_uuid[:16]}...")
            if len(result.uuid_synced) > 10:
                lines.append(f"  ... 还有 {len(result.uuid_synced) - 10} 台")

        if result.uuid_failed:
            lines.append(f"\n**uuid 同步失败 ({len(result.uuid_failed)} 台):**")
            for item in result.uuid_failed[:5]:
                lines.append(f"• [{item.console_name}] {item.computer_name}: {item.error}")

        if result.contact_synced:
            lines.append(f"\n**GLPI contact → S1 externalId ({len(result.contact_synced)} 台):**")
            for item in result.contact_synced[:10]:
                lines.append(f"• [{item.console_name}] {item.computer_name}")
                lines.append(f"  externalId={item.glpi_contact}")
            if len(result.contact_synced) > 10:
                lines.append(f"  ... 还有 {len(result.contact_synced) - 10} 台")

        if result.contact_failed:
            lines.append(f"\n**externalId 同步失败 ({len(result.contact_failed)} 台):**")
            for item in result.contact_failed[:5]:
                lines.append(f"• [{item.console_name}] {item.computer_name}: {item.error}")

        text = f"双向同步完成: UUID={len(result.uuid_synced)}/{len(result.uuid_failed)} fail, Contact={len(result.contact_synced)}/{len(result.contact_failed)} fail"
        lines.insert(0, text + "\n")

        await self._send_simple_card(
            title=f"S1 ↔ GLPI 双向同步{dry_tag}",
            content="\n".join(lines),
            color="blue",
            note=f"UUID={len(result.uuid_synced)}/{len(result.uuid_synced)+len(result.uuid_failed)}  Contact={len(result.contact_synced)}/{len(result.contact_synced)+len(result.contact_failed)}{overwrite_tag}",
        )

    async def _send_simple_card(
        self, title: str, content: str, color: str = "blue", note: str = "",
    ) -> None:
        """发送简单卡片消息"""
        if not self._client:
            return
        elements = [{"tag": "markdown", "content": content}]
        if note:
            elements.append({
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": note}],
            })

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
                "elements": elements,
            },
        }

        try:
            resp = await self._client.post(self.webhook_url, json=card)
            resp.raise_for_status()
            logger.info("Lark bidirectional sync card sent: %s", title)
        except Exception as e:
            logger.error("Lark bidirectional sync card failed: %s", e)

    async def _send_card(
        self,
        title: str,
        items: list[AssetChange],
        color: str = "blue",
    ) -> None:
        """发送飞书卡片消息（包含控制台来源标识）"""
        if not self._client:
            return

        content_lines: list[str] = []
        for item in items[:20]:  # 最多展示 20 条
            # 带控制台来源标签
            tag = f"[{item.console_name}]" if item.console_name else ""
            if item.is_new:
                content_lines.append(
                    f"• **{tag}{item.computer_name}** (ID: {item.agent_id})"
                )
            else:
                change_parts = [
                    f"{k}: {v[0]} → {v[1]}" for k, v in item.changes.items()
                ]
                content_lines.append(
                    f"• **{tag}{item.computer_name}** (ID: {item.agent_id})\n"
                    + "\n".join(f"  - {p}" for p in change_parts)
                )

        if len(items) > 20:
            content_lines.append(f"\n... 还有 {len(items) - 20} 条未显示")

        card: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": color,
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "\n".join(content_lines),
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": f"共 {len(items)} 条 | SentinelOne → GLPI 同步服务",
                            }
                        ],
                    },
                ],
            },
        }

        try:
            resp = await self._client.post(self.webhook_url, json=card)
            resp.raise_for_status()
            logger.info("Lark card sent: %s (%d items)", title, len(items))
        except Exception as e:
            logger.error("Lark card notification failed: %s", e)
