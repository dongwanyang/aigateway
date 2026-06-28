"""
会话管理模块

负责会话历史的持久化和加载。会话数据存储在 ~/.aigateway/sessions/{name}.json。
每个会话保留最近 MAX_HISTORY 条消息，超出则截断。
"""

import json
import os
from pathlib import Path
from typing import Any


# 每个会话保留的最大消息数
MAX_HISTORY: int = 50

# 会话存储根目录
SESSIONS_DIR: Path = Path.home() / ".aigateway" / "sessions"


class Session:
    """会话管理器。

    属性:
        name: 会话名称
        history: 消息历史列表，每项为 {"role": "...", "content": "..."} 字典
    """

    def __init__(self, name: str | None = None) -> None:
        """初始化会话。

        参数:
            name: 会话名称。如果为 None 则使用默认会话 "default"。
        """
        self.name: str = name or "default"
        self.history: list[dict[str, Any]] = []
        # 首次创建时尝试加载已有会话
        self._load()

    def _ensure_dir(self) -> None:
        """确保会话目录存在。"""
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    def _session_path(self) -> Path:
        """获取会话数据文件的绝对路径。

        返回:
            会话 JSON 文件的 Path 对象。
        """
        return SESSIONS_DIR / f"{self.name}.json"

    def _load(self) -> None:
        """从磁盘加载会话历史。"""
        path = self._session_path()
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data: dict[str, Any] = json.load(f)
                self.history = data.get("history", [])
            except (json.JSONDecodeError, OSError):
                # 文件损坏则忽略，从头开始新会话
                self.history = []
        else:
            self.history = []

    def save(self) -> None:
        """将当前会话历史持久化到磁盘。

        保存前自动截断至 MAX_HISTORY 条最近的消息。
        """
        # 只保留最近 MAX_HISTORY 条
        self.history = self.history[-MAX_HISTORY:]

        self._ensure_dir()
        path = self._session_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"name": self.name, "history": self.history}, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"[yellow]警告: 无法保存会话 {self.name}: {e}[/]")

    def add_message(self, role: str, content: str) -> None:
        """添加一条消息到会话历史。

        参数:
            role: 消息角色 ("system" | "user" | "assistant")
            content: 消息内容
        """
        self.history.append({"role": role, "content": content})
        self.save()

    def get_messages(self) -> list[dict[str, str]]:
        """获取当前会话的完整消息历史。

        返回:
            消息列表，每项为 {"role": ..., "content": ...} 字典。
        """
        # 只保留最近 MAX_HISTORY 条
        return self.history[-MAX_HISTORY:]

    def clear(self) -> None:
        """清空当前会话历史。"""
        self.history = []
        self.save()

    @staticmethod
    def list_sessions() -> list[str]:
        """列出所有已保存的会话名称。

        返回:
            会话名称列表（不含 .json 后缀）。
        """
        sessions: list[str] = []
        if SESSIONS_DIR.exists():
            for path in SESSIONS_DIR.iterdir():
                if path.suffix == ".json":
                    sessions.append(path.stem)
        return sorted(sessions)

    @staticmethod
    def delete_session(name: str) -> bool:
        """删除指定名称的会话。

        参数:
            name: 会话名称

        返回:
            是否成功删除。
        """
        path = SESSIONS_DIR / f"{name}.json"
        if path.exists():
            path.unlink()
            return True
        return False
