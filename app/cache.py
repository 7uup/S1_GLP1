"""SQLite 缓存层 — 存储上次同步快照，用于检测新增和变更"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from app.models import S1Agent

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id        TEXT PRIMARY KEY,
    computer_name   TEXT NOT NULL DEFAULT '',
    console_name    TEXT NOT NULL DEFAULT '',
    os_name         TEXT NOT NULL DEFAULT '',
    domain          TEXT NOT NULL DEFAULT '',
    model_name      TEXT NOT NULL DEFAULT '',
    serial_number   TEXT NOT NULL DEFAULT '',
    ip              TEXT NOT NULL DEFAULT '',
    agent_version   TEXT NOT NULL DEFAULT '',
    is_active       INTEGER NOT NULL DEFAULT 1,
    snapshot        TEXT NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    new_count   INTEGER DEFAULT 0,
    changed_count INTEGER DEFAULT 0,
    total_count INTEGER DEFAULT 0,
    detail      TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bi_sync_log (
    serial          TEXT PRIMARY KEY,
    contact         TEXT NOT NULL DEFAULT '',
    uuid            TEXT NOT NULL DEFAULT '',
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# 从旧表迁移：添加 console_name 列
_MIGRATIONS = [
    "ALTER TABLE agents ADD COLUMN console_name TEXT NOT NULL DEFAULT ''",
]


class CacheDB:
    """异步 SQLite 缓存"""

    def __init__(self, db_path: str = "./data/cache.db") -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        # 执行增量迁移
        for sql in _MIGRATIONS:
            try:
                await self._conn.execute(sql)
                await self._conn.commit()
            except aiosqlite.OperationalError:
                pass  # 列已存在，忽略
        logger.info("CacheDB initialized: %s", self.db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ── Agent 快照 ──────────────────────────────────────────

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        assert self._conn
        cur = await self._conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return dict(row)

    async def get_all_agents(self) -> dict[str, dict[str, Any]]:
        """返回 {agent_id: snapshot_dict} 的完整快照"""
        assert self._conn
        cur = await self._conn.execute("SELECT agent_id, snapshot FROM agents")
        rows = await cur.fetchall()
        result = {}
        for row in rows:
            snapshot = json.loads(row["snapshot"])
            result[row["agent_id"]] = snapshot
        return result

    async def upsert_agent(self, agent: S1Agent) -> None:
        assert self._conn
        snapshot = agent.to_cache_dict()
        await self._conn.execute(
            """
            INSERT INTO agents (agent_id, computer_name, console_name, os_name, domain, model_name,
                                serial_number, ip, agent_version, is_active, snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                computer_name=excluded.computer_name,
                console_name=excluded.console_name,
                os_name=excluded.os_name,
                domain=excluded.domain,
                model_name=excluded.model_name,
                serial_number=excluded.serial_number,
                ip=excluded.ip,
                agent_version=excluded.agent_version,
                is_active=excluded.is_active,
                snapshot=excluded.snapshot,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                agent.agent_id,
                agent.computer_name,
                agent.console_name,
                agent.os_name,
                agent.domain,
                agent.model_name,
                agent.serial_number,
                agent.ip,
                agent.agent_version,
                int(agent.is_active),
                json.dumps(snapshot, ensure_ascii=False),
            ),
        )
        await self._conn.commit()

    async def bulk_upsert(self, agents: list[S1Agent]) -> None:
        """批量写入，减少事务开销"""
        assert self._conn
        await self._conn.execute("BEGIN")
        try:
            for agent in agents:
                snapshot = agent.to_cache_dict()
                await self._conn.execute(
                    """
                    INSERT INTO agents (agent_id, computer_name, console_name, os_name, domain, model_name,
                                        serial_number, ip, agent_version, is_active, snapshot)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        computer_name=excluded.computer_name,
                        console_name=excluded.console_name,
                        os_name=excluded.os_name,
                        domain=excluded.domain,
                        model_name=excluded.model_name,
                        serial_number=excluded.serial_number,
                        ip=excluded.ip,
                        agent_version=excluded.agent_version,
                        is_active=excluded.is_active,
                        snapshot=excluded.snapshot,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        agent.agent_id,
                        agent.computer_name,
                        agent.console_name,
                        agent.os_name,
                        agent.domain,
                        agent.model_name,
                        agent.serial_number,
                        agent.ip,
                        agent.agent_version,
                        int(agent.is_active),
                        json.dumps(snapshot, ensure_ascii=False),
                    ),
                )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    # ── 同步日志 ────────────────────────────────────────────

    async def log_sync(self, new_count: int, changed_count: int,
                       total_count: int, detail: str = "") -> None:
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO sync_log (new_count, changed_count, total_count, detail)
            VALUES (?, ?, ?, ?)
            """,
            (new_count, changed_count, total_count, detail),
        )
        await self._conn.commit()

    async def get_last_sync(self) -> dict[str, Any] | None:
        assert self._conn
        cur = await self._conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # ── 双向同步追踪 ──────────────────────────────────────

    async def get_bi_synced_contacts(self) -> set[str]:
        """返回已同步过 contact 的 serial 集合"""
        assert self._conn
        cur = await self._conn.execute(
            "SELECT serial FROM bi_sync_log WHERE contact != ''"
        )
        return {row["serial"] async for row in cur}

    async def get_bi_synced_uuids(self) -> set[str]:
        """返回已同步过 uuid 的 serial 集合"""
        assert self._conn
        cur = await self._conn.execute(
            "SELECT serial FROM bi_sync_log WHERE uuid != ''"
        )
        return {row["serial"] async for row in cur}

    async def set_bi_synced_contact(self, serial: str, contact: str) -> None:
        """记录某 serial 的 contact 已同步"""
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO bi_sync_log (serial, contact) VALUES (?, ?)
            ON CONFLICT(serial) DO UPDATE SET contact=excluded.contact, updated_at=CURRENT_TIMESTAMP
            """,
            (serial, contact),
        )
        await self._conn.commit()

    async def set_bi_synced_uuid(self, serial: str, uuid_val: str) -> None:
        """记录某 serial 的 uuid 已同步"""
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO bi_sync_log (serial, uuid) VALUES (?, ?)
            ON CONFLICT(serial) DO UPDATE SET uuid=excluded.uuid, updated_at=CURRENT_TIMESTAMP
            """,
            (serial, uuid_val),
        )
        await self._conn.commit()
