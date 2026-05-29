"""S1 API Token 自动刷新模块

原理：
- S1 token 是 JWT，解码 payload 可得 exp（过期时间戳）
- 刷新接口：POST /web/api/v2.1/users/generate-api-token
- 成功则原子更新 config.yaml（备份原文件）
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.config import TokenRefreshConfig, load_config, get_config

logger = logging.getLogger(__name__)


class TokenManager:
    """S1 API Token 过期检测 + 自动刷新 + 飞书告警"""

    def __init__(
        self,
        config: TokenRefreshConfig,
        config_path: str = "config.yaml",
    ) -> None:
        self.cfg = config
        self.config_path = Path(config_path)

    # ── JWT 解码 ──────────────────────────────────────────

    @staticmethod
    def decode_jwt(token: str) -> tuple[int | None, dict[str, Any]]:
        """解码 JWT，返回 (exp_timestamp, payload)"""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None, {}
            pad = "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
            exp = payload.get("exp")
            return (int(exp) if exp else None), payload
        except Exception as e:
            logger.warning("Failed to decode JWT: %s", e)
            return None, {}

    @staticmethod
    def remaining_days(exp_timestamp: int | None) -> int | None:
        """返回剩余天数，None 表示无法解码"""
        if exp_timestamp is None:
            return None
        now = int(datetime.now(tz=timezone.utc).timestamp())
        return (exp_timestamp - now) // 86400

    # ── 刷新 ──────────────────────────────────────────────

    async def refresh_one(
        self, name: str, base_url: str, current_token: str,
    ) -> dict[str, Any]:
        """刷新单个控制台的 token，返回 {name, success, new_token?, error?}"""
        result = {"name": name, "success": False}

        # 1. 解码当前 token
        exp, payload = self.decode_jwt(current_token)
        remaining = self.remaining_days(exp)
        exp_str = (
            datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
            if exp else "unknown"
        )
        logger.info(
            "[%s] token: sub=%s, exp=%s, remaining=%sd",
            name, payload.get("sub", "?"), exp_str, remaining,
        )
        result["remaining_days"] = remaining

        # 2. 判断是否需要刷新
        if remaining is not None and remaining >= self.cfg.threshold_days:
            logger.info(
                "[%s] token OK (%sd >= threshold %sd), skip refresh",
                name, remaining, self.cfg.threshold_days,
            )
            result["skipped"] = True
            result["success"] = True
            return result

        # 3. 调用 generate-api-token
        url = base_url.rstrip("/") + "/web/api/v2.1/users/generate-api-token"
        logger.info("[%s] refreshing token via %s ...", name, url)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url,
                    json={"data": {}},
                    headers={
                        "Authorization": f"ApiToken {current_token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as e:
            error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
            logger.error("[%s] generate-api-token failed: %s", name, error)
            result["error"] = error
            return result
        except Exception as e:
            logger.error("[%s] generate-api-token network error: %s", name, e)
            result["error"] = str(e)
            return result

        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        new_token = data.get("token") or data.get("apiToken") or ""
        if not new_token or new_token == current_token:
            error = f"API did not return a new token: {json.dumps(body)[:300]}"
            logger.error("[%s] %s", name, error)
            result["error"] = error
            return result

        # 4. 验证新 token
        new_exp, _ = self.decode_jwt(new_token)
        new_remaining = self.remaining_days(new_exp)
        new_exp_str = (
            datetime.fromtimestamp(new_exp, tz=timezone.utc).isoformat()
            if new_exp else "unknown"
        )
        logger.info(
            "[%s] new token: exp=%s, remaining=%sd",
            name, new_exp_str, new_remaining,
        )

        # 5. 原子写入 config.yaml
        try:
            self._atomic_update_config(name, new_token)
        except Exception as e:
            logger.error("[%s] failed to update config.yaml: %s", name, e)
            result["error"] = f"config write failed: {e}"
            return result

        result["success"] = True
        result["new_token"] = "***"  # 不返回完整 token
        result["new_remaining_days"] = new_remaining
        result["new_exp"] = new_exp_str
        logger.info(
            "[%s] token refreshed OK, config.yaml updated, new exp=%s",
            name, new_exp_str,
        )
        return result

    async def refresh_all(self) -> list[dict[str, Any]]:
        """刷新所有控制台 token，返回每个的结果列表"""
        config = get_config()
        results: list[dict[str, Any]] = []

        for s1_cfg in config.sentinelone_instances:
            try:
                r = await self.refresh_one(
                    s1_cfg.name, s1_cfg.base_url, s1_cfg.api_token,
                )
            except Exception as e:
                r = {"name": s1_cfg.name, "success": False, "error": str(e)}
                logger.error("[%s] unexpected refresh error: %s", s1_cfg.name, e)
            results.append(r)

        return results

    # ── 原子配置更新 ──────────────────────────────────────

    def _atomic_update_config(self, instance_name: str, new_token: str) -> None:
        """原子更新 config.yaml 中指定控制台的 api_token，自动备份"""
        config_path = self.config_path.resolve()

        if not config_path.exists():
            raise FileNotFoundError(f"config not found: {config_path}")

        original = config_path.read_text(encoding="utf-8")

        # 备份
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = config_path.with_name(f"config.backup-{ts}.yaml")
        shutil.copy2(config_path, backup_path)
        logger.info("config backup: %s", backup_path.name)

        # 定位并替换 token（在 YAML 列表中匹配 name）
        lines = original.splitlines(keepends=True)
        replaced = False
        in_target = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            # 检测进入目标控制台配置块
            if stripped.startswith("- name:") and instance_name in stripped:
                in_target = True
                continue
            if in_target and stripped.startswith("api_token:") and "api_token:" in stripped:
                indent = line[: len(line) - len(line.lstrip())]
                # 判断是否需要引号（JWT 不含空格/YAML特殊字符，可直接写）
                lines[i] = f'{indent}api_token: "{new_token}"\n'
                replaced = True
                break
            if in_target and stripped.startswith("- name:"):
                in_target = False  # 进入下一个控制台，停止

        if not replaced:
            raise RuntimeError(
                f"Cannot find api_token for [{instance_name}] in {config_path}"
            )

        # 原子写入
        new_content = "".join(lines)
        tmp_path = config_path.with_suffix(".tmp")
        tmp_path.write_text(new_content, encoding="utf-8", newline="")
        os.replace(tmp_path, config_path)

        # 重载配置使新 token 生效
        load_config(config_path)
        logger.info("config.yaml updated + reloaded for [%s]", instance_name)
