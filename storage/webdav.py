"""
聆音 — WebDAV 云存储 Provider
============================================
使用 aiohttp PUT 上传到 WebDAV 兼容存储。
"""
import asyncio
import os
import base64
from datetime import datetime
from typing import Optional

from astrbot.api import logger

from .base import CloudProvider


class WebDAVProvider(CloudProvider):
    """WebDAV 兼容存储 Provider（aiohttp PUT）。"""

    async def upload(self, file_path: str, text: str) -> Optional[str]:
        import aiohttp

        url = (self.config.get("cloud_webdav_url") or "").strip()
        username = (self.config.get("cloud_webdav_username") or "").strip()
        password = (self.config.get("cloud_webdav_password") or "").strip()

        if not url:
            logger.warning("[tts_cloud] cloud_webdav_url 未配置，跳过")
            return None

        if not os.path.exists(file_path):
            logger.warning(f"[tts_cloud] WebDAV 上传: 文件不存在 {file_path}")
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = f"{os.urandom(3).hex():06s}"
        filename = f"voice_{ts}_{uid}.wav"
        upload_url = f"{url.rstrip('/')}/{self._cloud_prefix()}/{filename}"

        # Basic 认证头
        headers = {}
        if username and password:
            auth = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {auth}"

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                with open(file_path, "rb") as f:
                    async with session.put(
                        upload_url,
                        data=f,
                        timeout=aiohttp.ClientTimeout(total=self.UPLOAD_TIMEOUT),
                    ) as resp:
                        if resp.status in (200, 201, 204):
                            logger.info(f"[tts_cloud] WebDAV 上传成功: {filename}")
                            return filename
                        else:
                            text = await resp.text()
                            logger.warning(
                                f"[tts_cloud] WebDAV 上传失败 (HTTP {resp.status}): "
                                f"{text[:200]}"
                            )
                            return None

        except asyncio.TimeoutError:
            logger.warning("[tts_cloud] WebDAV 上传超时")
            return None
        except aiohttp.ClientError as e:
            logger.warning(f"[tts_cloud] WebDAV 上传网络错误: {e}")
            return None
        except Exception as e:
            logger.warning(f"[tts_cloud] WebDAV 上传异常: {e}")
            return None
