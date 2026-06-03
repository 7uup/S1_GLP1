"""GLPI API 客户端 — 通过 REST API 同步资产到 GLPI"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import GLPIConfig
from app.models import S1Agent

logger = logging.getLogger(__name__)


class GLPIClient:
    """异步 GLPI API 客户端（使用 UserToken 认证）"""

    def __init__(self, config: GLPIConfig) -> None:
        self.base_url = config.base_url.rstrip("/")
        self.app_token = config.app_token
        self.user_token = config.user_token
        self.field_mapping = config.field_mapping
        self._client: httpx.AsyncClient | None = None
        self._session_token: str | None = None

    async def init(self) -> None:
        # GLPI 反向代理兼容：需使用 AsyncHTTPTransport 避免 502
        transport = httpx.AsyncHTTPTransport(retries=1)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "App-Token": self.app_token,
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            },
            transport=transport,
            timeout=30.0,
        )
        await self._init_session()
        logger.info("GLPIClient initialized: %s", self.base_url)

    async def close(self) -> None:
        if self._client and self._session_token:
            try:
                await self._client.get(
                    "/killSession",
                    headers={"Session-Token": self._session_token},
                )
            except Exception:
                pass
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _init_session(self) -> None:
        """获取 GLPI Session Token"""
        assert self._client
        resp = await self._client.get(
            "/initSession",
            headers={"Authorization": f"user_token {self.user_token}"},
        )
        resp.raise_for_status()
        self._session_token = resp.json().get("session_token")
        if not self._session_token:
            raise RuntimeError("Failed to obtain GLPI session token")
        logger.debug("GLPI session token obtained")

    def _headers(self) -> dict[str, str]:
        return {"Session-Token": self._session_token or ""}

    # ── 批量查询（双向同步用） ──────────────────────────────

    async def get_all_computers_full(self) -> list[dict]:
        """获取所有 Computer 的 ID、serial、uuid、contact、comment（用于同步匹配）"""
        assert self._client

        all_items: list[dict] = []
        start = 0
        limit = 200

        while True:
            resp = await self._client.get(
                "/Computer",
                params={
                    "expand_dropdowns": "false",
                    "range": f"{start}-{start + limit - 1}",
                },
                headers=self._headers(),
            )
            if resp.status_code == 401:
                await self._init_session()
                continue
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_items.extend(batch)
            if len(batch) < limit:
                break
            start += limit

        logger.info("Fetched %d GLPI computers for bidirectional sync", len(all_items))
        return all_items

    async def set_computer_uuid(self, computer_id: int, uuid_val: str) -> bool:
        """设置 GLPI Computer 的 uuid 字段（双向同步：S1 uuid → GLPI uuid）"""
        assert self._client
        resp = await self._client.put(
            f"/Computer/{computer_id}",
            json={"input": {"uuid": uuid_val}},
            headers=self._headers(),
        )
        if resp.status_code == 401:
            await self._init_session()
            resp = await self._client.put(
                f"/Computer/{computer_id}",
                json={"input": {"uuid": uuid_val}},
                headers=self._headers(),
            )
        resp.raise_for_status()
        logger.info("Set uuid for GLPI Computer %d: %s", computer_id, uuid_val)
        return True

    async def set_computer_comment(self, computer_id: int, comment: str) -> bool:
        """设置 GLPI Computer 的 comment 字段（S1 机器配置 → GLPI 备注）"""
        assert self._client
        resp = await self._client.put(
            f"/Computer/{computer_id}",
            json={"input": {"comment": comment}},
            headers=self._headers(),
        )
        if resp.status_code == 401:
            await self._init_session()
            resp = await self._client.put(
                f"/Computer/{computer_id}",
                json={"input": {"comment": comment}},
                headers=self._headers(),
            )
        resp.raise_for_status()
        logger.info("Set comment for GLPI Computer %d", computer_id)
        return True

    async def get_computer_notepads(self, computer_id: int) -> list[dict]:
        """获取 GLPI Computer 的 Notepad 记录"""
        assert self._client
        resp = await self._client.get(
            f"/Computer/{computer_id}/Notepad",
            headers=self._headers(),
        )
        if resp.status_code == 401:
            await self._init_session()
            resp = await self._client.get(
                f"/Computer/{computer_id}/Notepad",
                headers=self._headers(),
            )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    async def add_computer_notepad(self, computer_id: int, content: str) -> bool:
        """新增 GLPI Computer 的 Notepad 记录"""
        assert self._client
        payload = {
            "input": {
                "itemtype": "Computer",
                "items_id": computer_id,
                "content": content,
            }
        }
        resp = await self._client.post(
            f"/Computer/{computer_id}/Notepad",
            json=payload,
            headers=self._headers(),
        )
        if resp.status_code == 401:
            await self._init_session()
            resp = await self._client.post(
                f"/Computer/{computer_id}/Notepad",
                json=payload,
                headers=self._headers(),
            )
        resp.raise_for_status()
        logger.info("Added Notepad for GLPI Computer %d", computer_id)
        return True

    # ── CRUD ────────────────────────────────────────────────

    async def search_computer(self, serial: str) -> int | None:
        """通过序列号搜索 GLPI Computer，返回 ID 或 None"""
        assert self._client
        resp = await self._client.get(
            "/search/Computer",
            params={
                "criteria[0][field]": "serial",
                "criteria[0][searchtype]": "equals",
                "criteria[0][value]": serial,
                "forcedisplay[0]": "2",  # id
            },
            headers=self._headers(),
        )
        if resp.status_code == 401:
            await self._init_session()
            resp = await self._client.get(
                "/search/Computer",
                params={
                    "criteria[0][field]": "serial",
                    "criteria[0][searchtype]": "equals",
                    "criteria[0][value]": serial,
                    "forcedisplay[0]": "2",
                },
                headers=self._headers(),
            )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("data", [])
        if rows:
            return int(rows[0].get("2", 0))  # "2" 是 id 字段的显示索引
        return None

    async def create_computer(self, agent: S1Agent) -> int | None:
        """在 GLPI 创建 Computer，返回新 ID"""
        assert self._client
        payload = self._build_payload(agent)
        resp = await self._client.post(
            "/Computer",
            json={"input": payload},
            headers=self._headers(),
        )
        if resp.status_code == 401:
            await self._init_session()
            resp = await self._client.post(
                "/Computer",
                json={"input": payload},
                headers=self._headers(),
            )
        resp.raise_for_status()
        result = resp.json()
        new_id = result.get("id")
        logger.info("Created GLPI Computer: %s (ID: %s)", agent.computer_name, new_id)
        return new_id

    async def update_computer(self, computer_id: int, agent: S1Agent) -> bool:
        """更新 GLPI Computer"""
        assert self._client
        payload = self._build_payload(agent)
        payload["id"] = computer_id
        resp = await self._client.put(
            "/Computer",
            json={"input": payload},
            headers=self._headers(),
        )
        if resp.status_code == 401:
            await self._init_session()
            resp = await self._client.put(
                "/Computer",
                json={"input": payload},
                headers=self._headers(),
            )
        resp.raise_for_status()
        logger.info("Updated GLPI Computer ID %d: %s", computer_id, agent.computer_name)
        return True

    # ── 辅助 ────────────────────────────────────────────────

    def _build_payload(self, agent: S1Agent) -> dict[str, Any]:
        """根据 field_mapping 构建 GLPI payload"""
        agent_dict = agent.to_cache_dict()
        payload: dict[str, Any] = {}

        for s1_field, glpi_field in self.field_mapping.items():
            # 将 s1 字段名转为 agent_dict 的 key 格式
            dict_key = self._camel_to_snake(s1_field)
            if dict_key in agent_dict:
                payload[glpi_field] = agent_dict[dict_key]

        return payload

    @staticmethod
    def _camel_to_snake(name: str) -> str:
        """camelCase → snake_case"""
        import re
        s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
        return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()
