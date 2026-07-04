"""
AI Voice Assistant — 云存储 Provider 抽象基类
==============================================
所有云上传 Provider 继承此类，实现 upload() 方法。
"""
import asyncio
from abc import ABC, abstractmethod
from typing import Optional

from astrbot.api import logger


class CloudProvider(ABC):
    """云存储上传 Provider 基类。"""

    UPLOAD_TIMEOUT = 120

    def __init__(self, config: dict):
        self.config = config

    # ── 上传入口 ──────────────────────────────────────────────

    @abstractmethod
    async def upload(self, file_path: str, text: str) -> Optional[str]:
        """上传文件，成功返回 URL/路径名，失败返回 None。"""
        ...

    def _cloud_prefix(self) -> str:
        """云存储子目录/前缀，防止文件散落在根目录。"""
        return "voice_assistant"
