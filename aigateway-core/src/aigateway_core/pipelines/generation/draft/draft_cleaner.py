"""
Draft Session Cleaner — 会话级草稿目录定时清理
==============================================

后台 asyncio 任务,定期扫描草稿文件存储根目录,删除过期的 session 目录。

触发清理的两个条件(任一满足即删整个 session 目录):
1. session 目录下所有 meta.json 的 expires_at 均已过期(单草稿 TTL 语义)。
2. session 目录本身的 mtime 超过 session_ttl_hours(兜底,防 meta 丢失/异常)。

设计:兜底机制——前端关闭会话时主动调 DELETE /admin/drafts/session/{id} 即时清理;
本任务覆盖"前端未调用"(刷新关闭浏览器、崩溃)的场景,保证磁盘不无限增长。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 扫描间隔(秒)。默认 1 小时。
_SCAN_INTERVAL_SECONDS = 3600.0


class DraftSessionCleaner:
    """定期扫描 /data/drafts,清理过期 session 目录。"""

    def __init__(
        self,
        store_dir: str,
        session_ttl_hours: int,
        strategy: Any = None,
        scan_interval_seconds: float = _SCAN_INTERVAL_SECONDS,
    ) -> None:
        self._store_dir = store_dir or "/data/drafts"
        self._session_ttl_seconds = max(1, session_ttl_hours) * 3600
        self._strategy = strategy
        self._scan_interval = scan_interval_seconds
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """启动后台扫描任务(幂等)。"""
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("draft_session_cleaner.stop error: %s", exc)
        self._task = None

    async def _run_loop(self) -> None:
        # 启动后先等一轮,避免与 lifespan 初始化抢资源。
        await asyncio.sleep(self._scan_interval)
        while True:
            try:
                await self.scan_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("draft_session_cleaner.scan error: %s", exc)
            await asyncio.sleep(self._scan_interval)

    async def scan_once(self) -> int:
        """扫描一次,返回删除的 session 目录数。"""
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
                    logger.warning("draft_session_cleaner.rmtree failed for %s: %s", session_dir, exc)
        if deleted:
            logger.info("draft_session_cleaner.scan_done deleted=%d", deleted)
        return deleted

    def _is_session_expired(self, session_dir: str, now: float) -> bool:
        """判断 session 目录是否过期。

        规则:
        - 读目录下所有 draft 子目录的 meta.json expires_at;
          若所有 draft 均已过期 → 整个 session 过期。
        - 若无任何 draft 子目录(空 session)或 meta 读取失败,
          退回 mtime 兜底:目录 mtime 超过 session_ttl_seconds 即判过期。
        """
        try:
            entries = os.listdir(session_dir)
        except OSError:
            return False

        draft_dirs = [
            os.path.join(session_dir, name) for name in entries
            if os.path.isdir(os.path.join(session_dir, name))
        ]

        if not draft_dirs:
            # 空 session 目录:mtime 兜底
            return self._mtime_expired(session_dir, now)

        import json
        all_expired = True
        any_meta_read = False
        for draft_dir in draft_dirs:
            meta_path = os.path.join(draft_dir, "meta.json")
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                expires_at = float(data.get("expires_at", 0))
                any_meta_read = True
                if expires_at > now:
                    all_expired = False
                    break
            except (json.JSONDecodeError, OSError, ValueError, TypeError):
                continue

        if any_meta_read:
            return all_expired
        # 所有 meta 都读不出 → mtime 兜底
        return self._mtime_expired(session_dir, now)

    def _mtime_expired(self, path: str, now: float) -> bool:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return False
        return (now - mtime) > self._session_ttl_seconds
