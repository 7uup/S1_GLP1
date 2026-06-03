"""
一次性脚本：将 S1 externalId / GLPI contact 写入 GLPI Notepad 作为历史使用者记录

用法：
    python sync_historical_user.py              # 全部同步
    python sync_historical_user.py --dry-run    # 只看不写
    python sync_historical_user.py --id 10      # 只同步指定 GLPI Computer ID
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.config import AppConfig, load_config
from app.glpi_client import GLPIClient
from app.models import S1Agent
from app.s1_client import S1Client


async def fetch_s1_agents(cfg: AppConfig) -> list[S1Agent]:
    """从所有 S1 控制台拉取 Agent，单个控制台失败不影响其它控制台"""
    agents: list[S1Agent] = []
    for s1_cfg in cfg.sentinelone_instances:
        client = S1Client(s1_cfg)
        await client.init()
        try:
            console_agents = await client.get_agents()
            agents.extend(console_agents)
            print(f"S1 [{s1_cfg.name}] agents={len(console_agents)}")
        except Exception as e:
            print(f"S1 [{s1_cfg.name}] 拉取失败: {e}")
        finally:
            await client.close()
    return agents


def has_same_historical_user(notes: list[dict], content: str) -> bool:
    """只按完整内容去重，用户变化时允许新增历史记录"""
    return any(content == (note.get("content") or "").strip() for note in notes)


async def main() -> None:
    parser = argparse.ArgumentParser(description="同步 S1 externalId / GLPI contact 到 GLPI Notepad")
    parser.add_argument("--dry-run", action="store_true", help="只检测，不写入")
    parser.add_argument("--id", type=int, default=0, help="只处理指定 GLPI Computer ID")
    args = parser.parse_args()

    cfg = load_config()
    stats: Counter[str] = Counter()
    planned: set[tuple[int, str]] = set()

    agents = await fetch_s1_agents(cfg)

    glpi = GLPIClient(cfg.glpi)
    await glpi.init()
    try:
        glpi_computers = await glpi.get_all_computers_full()
        glpi_by_serial = {
            (comp.get("serial") or "").strip(): comp
            for comp in glpi_computers
            if (comp.get("serial") or "").strip()
        }

        for agent in agents:
            stats["total_agents"] += 1
            serial = agent.serial_number.strip()
            if not serial:
                stats["missing_serial"] += 1
                continue

            comp = glpi_by_serial.get(serial)
            if not comp:
                stats["no_glpi_match"] += 1
                continue

            historical_user, historical_user_source = agent.historical_user_source(
                comp.get("contact") or ""
            )
            if not historical_user:
                stats["missing_user"] += 1
                continue

            cid = int(comp["id"])
            if args.id and cid != args.id:
                stats["filtered"] += 1
                continue

            content = f"历史使用者：{historical_user}"
            key = (cid, content)
            if key in planned:
                stats["skip_duplicate"] += 1
                continue

            try:
                notes = await glpi.get_computer_notepads(cid)
                if has_same_historical_user(notes, content):
                    stats["skip_existing"] += 1
                    continue

                planned.add(key)
                if args.dry_run:
                    print(
                        f"[DRY-RUN] GLPI {cid:>4}  "
                        f"{comp.get('name', '')[:20]}  <- {content} "
                        f"({historical_user_source})"
                    )
                    stats["write"] += 1
                    continue

                await glpi.add_computer_notepad(cid, content)
                print(
                    f"OK  GLPI {cid:>4}  {comp.get('name', '')[:20]}  <- {content} "
                    f"({historical_user_source})"
                )
                stats["write"] += 1
            except Exception as e:
                print(f"FAIL  GLPI {cid}  {comp.get('name', '')[:20]}  {e}")
                stats["fail"] += 1
    finally:
        await glpi.close()

    tag = "[DRY-RUN]" if args.dry_run else ""
    print(
        f"\n{tag} 写入: {stats['write']}  "
        f"跳过(已存在): {stats['skip_existing']}  "
        f"跳过(本轮重复): {stats['skip_duplicate']}  "
        f"按 ID 过滤: {stats['filtered']}  "
        f"无 serial: {stats['missing_serial']}  "
        f"无 S1 用户: {stats['missing_user']}  "
        f"未匹配 GLPI: {stats['no_glpi_match']}  "
        f"失败: {stats['fail']}  "
        f"S1 总数: {stats['total_agents']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
