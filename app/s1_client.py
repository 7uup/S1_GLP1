"""SentinelOne API 客户端 — 分页拉取所有 Agent"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import SentinelOneConfig
from app.models import S1Agent

logger = logging.getLogger(__name__)


class S1Client:
    """异步 SentinelOne API 客户端"""

    def __init__(self, config: SentinelOneConfig) -> None:
        self.name = config.name          # 控制台标识 (HK / SZ 等)
        self.base_url = config.base_url.rstrip("/")
        self.api_token = config.api_token
        self.page_size = config.page_size
        self._client: httpx.AsyncClient | None = None

    async def init(self) -> None:
        await self._init_client()

    async def _init_client(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"ApiToken {self.api_token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        logger.info("S1Client [%s] initialized: %s", self.name, self.base_url)

    async def reload_token(self, new_token: str) -> None:
        """热重载 token，无需重启服务"""
        old_client = self._client
        self.api_token = new_token
        await self._init_client()
        if old_client:
            await old_client.aclose()
        logger.info("S1Client [%s] token hot-reloaded", self.name)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── 核心方法 ────────────────────────────────────────────

    async def get_agents(self) -> list[S1Agent]:
        """拉取所有 Agent（自动分页），标记 console_name"""
        assert self._client, "S1Client not initialized. Call init() first."

        all_agents: list[S1Agent] = []
        cursor = None

        while True:
            params: dict[str, Any] = {
                "limit": self.page_size,
            }
            if cursor is not None:
                params["cursor"] = cursor

            resp = await self._client.get(
                "/web/api/v2.1/agents", params=params
            )
            resp.raise_for_status()
            data = resp.json()

            agents_data = data.get("data", [])
            for item in agents_data:
                all_agents.append(S1Agent.from_api(item, console_name=self.name))

            # 分页处理
            pagination = data.get("pagination", {})
            next_cursor = pagination.get("nextCursor")

            if not next_cursor or not agents_data:
                break

            cursor = next_cursor
            logger.debug(
                "[%s] Fetched page, total so far: %d, nextCursor: %s",
                self.name, len(all_agents), cursor,
            )

        logger.info("[%s] Total agents fetched: %d", self.name, len(all_agents))
        return all_agents

    async def get_agent_by_id(self, agent_id: str) -> S1Agent | None:
        """获取单个 Agent 详情"""
        assert self._client
        resp = await self._client.get(
            f"/web/api/v2.1/agents/{agent_id}"
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return S1Agent.from_api(data, console_name=self.name) if data else None

    async def set_external_id(self, agent_id: str, external_id: str) -> bool:
        """设置 Agent 的 externalId（双向同步：GLPI contact → S1 externalId）"""
        assert self._client
        resp = await self._client.post(
            "/web/api/v2.1/agents/actions/set-external-id",
            json={
                "filter": {"ids": [agent_id]},
                "data": {"externalId": external_id},
            },
        )
        resp.raise_for_status()
        result = resp.json()
        affected = result.get("data", {}).get("affected", 0)
        ok = affected > 0
        if ok:
            logger.info(
                "[%s] Set externalId for agent %s: %s (affected=%d)",
                self.name, agent_id, external_id, affected,
            )
        else:
            logger.warning(
                "[%s] Set externalId for agent %s returned affected=0",
                self.name, agent_id,
            )
        return ok
