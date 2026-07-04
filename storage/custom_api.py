"""
AI Voice Assistant — 自定义 API 云存储 Provider
================================================
使用 aiohttp multipart/form-data POST 上传到用户自定义 API。
"""
import asyncio
import json as _json
import os
from typing import Optional

from astrbot.api import logger

from .base import CloudProvider


def _extract_json_path(body: str, json_path: str) -> Optional[str]:
    """从 JSON 字符串中按点号路径提取值。例: 'data.url' → result['data']['url']"""
    try:
        obj = _json.loads(body)
        for key in json_path.split("."):
            if isinstance(obj, dict):
                obj = obj.get(key)
            else:
                return None
        return str(obj) if obj is not None else None
    except (_json.JSONDecodeError, TypeError, AttributeError):
        return None


class CustomApiProvider(CloudProvider):
    """用户自定义 API Provider（aiohttp multipart POST）。"""

    async def upload(self, file_path: str, text: str) -> Optional[str]:
        import aiohttp

        url = (self.config.get("cloud_custom_url") or "").strip()
        if not url:
            logger.warning("[tts_cloud] cloud_custom_url 未配置，跳过")
            return None

        if not os.path.exists(file_path):
            logger.warning(f"[tts_cloud] 自定义上传: 文件不存在 {file_path}")
            return None

        headers_raw = (self.config.get("cloud_custom_headers") or "").strip()
        body_raw = (self.config.get("cloud_custom_body") or "").strip()
        result_path = (self.config.get("cloud_custom_result_path") or "").strip()

        # 解析额外请求头
        extra_headers = {}
        for line in headers_raw.splitlines():
            line = line.strip()
            if ":" in line:
                k, _, v = line.partition(":")
                extra_headers[k.strip()] = v.strip()

        # 构造 multipart 表单
        data = aiohttp.FormData()
        with open(file_path, "rb") as f:
            data.add_field(
                "file",
                f.read(),
                filename=os.path.basename(file_path),
                content_type="audio/wav",
            )
        if body_raw:
            try:
                body_obj = _json.loads(body_raw)
                for k, v in body_obj.items():
                    data.add_field(k, str(v))
            except _json.JSONDecodeError:
                logger.warning("[tts_cloud] cloud_custom_body 不是合法 JSON，跳过")

        try:
            async with aiohttp.ClientSession(headers=extra_headers) as session:
                async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=self.UPLOAD_TIMEOUT)) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        logger.warning(
                            f"[tts_cloud] 自定义上传失败 (HTTP {resp.status}): "
                            f"{body[:200]}"
                        )
                        return None

                    if result_path:
                        got = _extract_json_path(body, result_path)
                        if got:
                            logger.info(f"[tts_cloud] 自定义上传成功 → {got}")
                            return got
                        else:
                            logger.warning(
                                f"[tts_cloud] 自定义上传成功，未能从响应提取 URL "
                                f"(path={result_path})"
                            )
                            return None
                    else:
                        logger.info("[tts_cloud] 自定义上传成功")
                        return None

        except asyncio.TimeoutError:
            logger.warning("[tts_cloud] 自定义上传超时")
            return None
        except aiohttp.ClientError as e:
            logger.warning(f"[tts_cloud] 自定义上传网络错误: {e}")
            return None
        except Exception as e:
            logger.warning(f"[tts_cloud] 自定义上传异常: {e}")
            return None
