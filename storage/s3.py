"""
AI Voice Assistant — S3 兼容云存储 Provider
============================================
使用 boto3 (AWS SDK) 上传到任何 S3 兼容存储。
"""
import mimetypes
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, EndpointConnectionError

from astrbot.api import logger

from ..errors import CloudUploadError
from .base import CloudProvider


class S3Provider(CloudProvider):
    """S3 兼容存储 Provider（使用 AWS SDK）。"""

    MAX_RETRIES = 3
    RETRY_DELAY = 2

    async def upload(self, file_path: str, text: str) -> Optional[str]:
        if not os.path.exists(file_path):
            logger.warning(f"[tts_cloud] S3 上传: 文件不存在 {file_path}")
            return None

        endpoint = (self.config.get("cloud_s3_endpoint") or "").strip()
        region = (self.config.get("cloud_s3_region") or "").strip()
        bucket = (self.config.get("cloud_s3_bucket") or "").strip()
        access_key = (self.config.get("cloud_s3_access_key") or "").strip()
        secret_key = (self.config.get("cloud_s3_secret_key") or "").strip()
        path_style = self.config.get("cloud_s3_path_style", True)

        missing = []
        if not endpoint: missing.append("cloud_s3_endpoint")
        if not region: missing.append("cloud_s3_region")
        if not bucket: missing.append("cloud_s3_bucket")
        if not access_key or not secret_key: missing.append("cloud_s3_access_key/secret_key")
        if missing:
            logger.warning(f"[tts_cloud] S3 配置不完整: {', '.join(missing)}，跳过")
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = f"{os.urandom(3).hex():06s}"
        key = f"voice_{ts}_{uid}.wav"

        s3_key = f"{self._cloud_prefix()}/{key}"
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = "audio/wav"

        # 构造 S3 客户端
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint.rstrip("/"),
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path" if path_style else "virtual"},
            ),
        )

        # 带重试的上传
        for attempt in range(self.MAX_RETRIES):
            try:
                with open(file_path, "rb") as f:
                    s3.put_object(
                        Bucket=bucket,
                        Key=s3_key,
                        Body=f,
                        ContentType=mime_type,
                    )
                logger.info(f"[tts_cloud] S3 上传成功: {s3_key}")
                return s3_key

            except ClientError as e:
                code = e.response["Error"]["Code"]
                msg = e.response["Error"]["Message"]
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning(
                        f"[tts_cloud] S3 上传失败 ({code})，{self.RETRY_DELAY}s 后重试: {msg}"
                    )
                    await __import__("asyncio").sleep(self.RETRY_DELAY)
                    continue
                logger.warning(f"[tts_cloud] S3 上传失败 ({code}): {msg}")
                return None

            except EndpointConnectionError as e:
                logger.warning(f"[tts_cloud] S3 无法连接端点: {e}")
                return None

            except Exception as e:
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning(
                        f"[tts_cloud] S3 上传异常，{self.RETRY_DELAY}s 后重试: {e}"
                    )
                    await __import__("asyncio").sleep(self.RETRY_DELAY)
                    continue
                logger.warning(f"[tts_cloud] S3 上传异常: {e}")
                return None

        return None
