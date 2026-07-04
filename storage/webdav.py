"""
AI Voice Assistant — WebDAV 云存储 Provider
============================================
使用 curl -T 上传到 WebDAV 兼容存储。
"""
import os
import random
from datetime import datetime
from typing import Optional

from astrbot.api import logger

from ..errors import CurlNotFoundError
from .base import CloudProvider


class WebDAVProvider(CloudProvider):
    """WebDAV 兼容存储 Provider。"""

    async def upload(self, file_path: str, text: str) -> Optional[str]:
        curl_path = self._ensure_curl()

        url = (self.config.get("cloud_webdav_url") or "").strip()
        username = (self.config.get("cloud_webdav_username") or "").strip()
        password = (self.config.get("cloud_webdav_password") or "").strip()

        if not url:
            logger.warning("[tts_cloud] cloud_webdav_url 未配置，跳过")
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = f"{random.randint(100000, 999999):06d}"
        filename = f"voice_{ts}_{uid}.wav"

        cmd = [curl_path, "-f", "-s", "-T", file_path]
        if username and password:
            cmd.extend(["-u", f"{username}:{password}"])
        cmd.append(f"{url.rstrip('/')}/{self._cloud_prefix()}/{filename}")

        try:
            returncode, stdout, stderr = await self._run_curl(cmd)
            if returncode == 0:
                logger.info(f"[tts_cloud] WebDAV 上传成功: {filename}")
                return filename
            else:
                logger.warning(
                    f"[tts_cloud] WebDAV 上传失败 (exit={returncode}): {stderr[:200]}"
                )
                return None
        except CurlNotFoundError:
            logger.warning("[tts_cloud] 未找到 curl，请确认 curl 已安装并在 PATH 中")
            return None
        except __import__("asyncio").TimeoutError:
            logger.warning("[tts_cloud] WebDAV 上传超时")
            return None
        except Exception as e:
            logger.warning(f"[tts_cloud] WebDAV 上传异常: {e}")
            return None
