"""
AI Voice Assistant — 云存储 Provider 抽象基类
==============================================
所有云上传 Provider 继承此类，实现 upload() 方法。
"""
import asyncio
from abc import ABC, abstractmethod
from typing import Optional

from astrbot.api import logger

from ..errors import CurlNotFoundError
from .curl import find_curl


class CloudProvider(ABC):
    """云存储上传 Provider 基类。继承者需实现 upload()，可复用 _run_curl() 模板方法。"""

    UPLOAD_TIMEOUT = 120

    def __init__(self, config: dict):
        self.config = config
        self._curl_path: Optional[str] = None

    # ── curl 路径解析 ─────────────────────────────────────────

    def _ensure_curl(self) -> str:
        """确保 curl 可用，返回路径；不可用时抛出 CurlNotFoundError。"""
        if self._curl_path:
            return self._curl_path
        path = find_curl()
        if not path:
            raise CurlNotFoundError("未找到 curl，请确认 curl 已安装并在 PATH 中")
        self._curl_path = path
        return path

    # ── 上传入口 ──────────────────────────────────────────────

    @abstractmethod
    async def upload(self, file_path: str, text: str) -> Optional[str]:
        """上传文件，成功返回 URL/文件名，失败返回 None。"""
        ...

    # ── 模板方法：执行 curl 命令 ──────────────────────────────

    async def _run_curl(self, cmd: list[str]) -> tuple[int, str, str]:
        """执行 curl 命令，返回 (returncode, stdout, stderr)。"""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.UPLOAD_TIMEOUT
            )
            return proc.returncode or 0, stdout.decode(), stderr.decode()
        except asyncio.TimeoutError:
            proc.kill()
            raise

    def _cloud_prefix(self) -> str:
        """云存储子目录/前缀，防止文件散落在根目录。"""
        return "voice_assistant"
