"""
Draft Session Cleaner — 会话级草稿目录定时清理
==============================================

后台 asyncio 任务，定期扫描配置的草稿文件存储根目录，删除过期 session 目录。

触发清理的两个条件（任一满足即删整个 session 目录）：
1. session 目录下所有 meta.json 的 expires_at 均已过期。
2. session 目录本身的 mtime 超过 session_ttl_hours。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# 扫描间隔属于调度算法默认值；调用方可显式覆盖。
_SCAN_INTERVAL_SECONDS = 3600.0


class DraftSessionCleaner:
    """定期扫描显式配置的草稿根目录，清理过期 session。"""

    def __init__(
        self,
        store_dir: str,
        session_ttl_hours: int,
        strategy: Any = None,
        scan_interval_seconds: float = _SCAN_INTERVAL_SECONDS,
    ) -> None:
        if not isinstance(store_dir, str) or not store_dir.strip():
            raise ValueError(
                "config_missing:generation_optimization.draft_workflow.store_dir"
            )
        if int(session_ttl_hours) <= 0:
            raise ValueError("session_ttl_hours must be positive")
        if float(scan_interval_seconds) <= 0:
            raise ValueError("scan_interval_seconds must be positive")

        self._store_dir = store_dir.strip()
        self._session_ttl_seconds = int(session_ttl_hours) * 3600
        self._strategy = strategy
        self._scan_interval = float(scan_interval_seconds)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """启动后台扫描任务（幂等）。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop(), name="draft-session-cleaner")

    async def stop(self) -> None:
        """停止后台任务。"""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("draft_session_cleaner.stop error: %s", exc)
        self._task = None

    async def _run_loop(self) -> None:
        await asyncio.sleep(self._scan_interval)
        while True:
            try:
                await self.scan_once()
            except Exception as exc:
                logger.warning("draft_session_cleaner.scan error: %s", exc)
            await asyncio.sleep(self._scan_interval)

    async def scan_once(self) -> int:
        """扫描一次，返回删除的 session 目录数。"""
        import shutil

        if not os.path.isdir(self._store_dir):
            return 0

        now = time.time()
        deleted = 0
        try:
            session_names = os.listdir(self._store_dir)
        except OSError:
            return 0

        for session_name in session_names:
            session_dir = os.path.join(self._store_dir, session_name)
            if not os.path.isdir(session_dir):
                continue
            if self._is_session_expired(session_dir, now):
                try:
                    shutil.rmtree(session_dir, ignore_errors=True)
                    deleted += 1
                    logger.info(
                        "draft_session_cleaner.session_removed",
                        extra={"session_id": session_name},
                    )
                except OSError as exc:
                    logger.warning(
                        "draft_session_cleaner.rmtree failed for %s: %s",
                        session_dir,
                        exc,
                    )
        if deleted:
            logger.info("draft_session_cleaner.scan_done deleted=%d", deleted)
        return deleted

    def _is_session_expired(self, session_dir: str, now: float) -> bool:
        """判断 session 目录是否过期。"""
        try:
            entries = os.listdir(session_dir)
        except OSError:
            return False

        draft_dirs = [
            os.path.join(session_dir, name)
            for name in entries
            if os.path.isdir(os.path.join(session_dir, name))
        ]

        if not draft_dirs:
            return self._mtime_expired(session_dir, now)

        import json

        all_expired = True
        any_meta_read = False
        for draft_dir in draft_dirs:
            meta_path = os.path.join(draft_dir, "meta.json")
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path, "r", encoding="utf-8") as file:
                    data = json.load(file)
                expires_at = float(data.get("expires_at", 0))
                any_meta_read = True
                if expires_at > now:
                    all_expired = False
                    break
            except (json.JSONDecodeError, OSError, ValueError, TypeError):
                continue

        if any_meta_read:
            return all_expired
        return self._mtime_expired(session_dir, now)

    def _mtime_expired(self, path: str, now: float) -> bool:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return False
        return (now - mtime) > self._session_ttl_seconds
