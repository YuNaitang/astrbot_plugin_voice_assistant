"""
AI Voice Assistant — 自定义 API 云存储 Provider
================================================
使用 multipart/form-data POST 上传到用户自定义 API。
"""
import json as _json
import os
import random
from datetime import datetime
from typing import Optional

from astrbot.api import logger

from ..errors import CurlNotFoundError
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
    """用户自定义 API Provider。"""

    async def upload(self, file_path: str, text: str) -> Optional[str]:
        curl_path = self._ensure_curl()

        url = (self.config.get("cloud_custom_url") or "").strip()
        if not url:
            logger.warning("[tts_cloud] cloud_custom_url 未配置，跳过")
            return None

        headers_raw = (self.config.get("cloud_custom_headers") or "").strip()
        body_raw = (self.config.get("cloud_custom_body") or "").strip()
        result_path = (self.config.get("cloud_custom_result_path") or "").strip()

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = f"{random.randint(100000, 999999):06d}"
        filename = f"voice_{ts}_{uid}.wav"

        cmd = [curl_path, "-f", "-s", "-X", "POST"]
        for line in headers_raw.splitlines():
            line = line.strip()
            if ":" in line:
                cmd.extend(["-H", line])
        cmd.extend(["-F", f"file=@{file_path};filename={filename}"])
        if body_raw:
            try:
                body_obj = _json.loads(body_raw)
                for k, v in body_obj.items():
                    cmd.extend(["-F", f"{k}={v}"])
            except _json.JSONDecodeError:
                logger.warning("[tts_cloud] cloud_custom_body 不是合法 JSON，跳过")

        cmd.append(url)

        try:
            returncode, stdout, stderr = await self._run_curl(cmd)
            if returncode != 0:
                logger.warning(
                    f"[tts_cloud] 自定义上传失败 (exit={returncode}): "
                    f"{stderr[:200]}"
                )
                return None

            if result_path:
                got = _extract_json_path(stdout, result_path)
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
                return None  # 无 result_path 时不知道 URL
        except CurlNotFoundError:
            logger.warning("[tts_cloud] 未找到 curl，请确认 curl 已安装并在 PATH 中")
            return None
        except __import__("asyncio").TimeoutError:
            logger.warning("[tts_cloud] 自定义上传超时")
            return None
        except Exception as e:
            logger.warning(f"[tts_cloud] 自定义上传异常: {e}")
            return None
