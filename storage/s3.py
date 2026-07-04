"""
AI Voice Assistant — S3 兼容云存储 Provider
============================================
使用 curl --aws-sigv4 签名上传到 S3 兼容存储。
"""
import json as _json
import os
import random
from datetime import datetime
from typing import Optional

from astrbot.api import logger

from ..errors import CurlNotFoundError
from .base import CloudProvider


class S3Provider(CloudProvider):
    """S3 兼容存储 Provider。"""

    async def upload(self, file_path: str, text: str) -> Optional[str]:
        curl_path = self._ensure_curl()

        endpoint = (self.config.get("cloud_s3_endpoint") or "").strip()
        region = (self.config.get("cloud_s3_region") or "").strip()
        bucket = (self.config.get("cloud_s3_bucket") or "").strip()
        access_key = (self.config.get("cloud_s3_access_key") or "").strip()
        secret_key = (self.config.get("cloud_s3_secret_key") or "").strip()
        path_style = self.config.get("cloud_s3_path_style", True)

        missing = []
        if not endpoint:
            missing.append("cloud_s3_endpoint")
        if not region:
            missing.append("cloud_s3_region")
        if not bucket:
            missing.append("cloud_s3_bucket")
        if not access_key or not secret_key:
            missing.append("cloud_s3_access_key/secret_key")
        if missing:
            logger.warning(f"[tts_cloud] S3 配置不完整: {', '.join(missing)}，跳过")
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = f"{random.randint(100000, 999999):06d}"
        key = f"voice_{ts}_{uid}.wav"

        if path_style:
            upload_url = f"{endpoint.rstrip('/')}/{bucket}/{self._cloud_prefix()}/{key}"
        else:
            e = endpoint.rstrip("/").lstrip("https://").lstrip("http://")
            upload_url = f"https://{bucket}.{e}/{self._cloud_prefix()}/{key}"

        cmd = [
            curl_path, "-f", "-s",
            "--aws-sigv4", f"aws:amz:{region}:s3",
            "--user", f"{access_key}:{secret_key}",
            "-X", "PUT",
            "--data-binary", f"@{file_path}",
            upload_url,
        ]

        try:
            returncode, stdout, stderr = await self._run_curl(cmd)
            if returncode == 0:
                logger.info(f"[tts_cloud] S3 上传成功: {upload_url}")
                return upload_url
            else:
                logger.warning(
                    f"[tts_cloud] S3 上传失败 (exit={returncode}): {stderr[:200]}"
                )
                return None
        except CurlNotFoundError:
            logger.warning("[tts_cloud] 未找到 curl，请确认 curl 已安装并在 PATH 中")
            return None
        except __import__("asyncio").TimeoutError:
            logger.warning("[tts_cloud] S3 上传超时")
            return None
        except Exception as e:
            logger.warning(f"[tts_cloud] S3 上传异常: {e}")
            return None
