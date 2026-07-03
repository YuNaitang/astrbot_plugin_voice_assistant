"""
AI Voice Assistant — 本地音频归档 + 云存储后端
================================================
包含本地文件持久化（7 天清理）、自定义 API / S3 / WebDAV / SMB 四种云后端。
"""
import asyncio
import json as _json
import os
import random
from datetime import datetime, timedelta
from typing import Optional

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


class AudioStorage:
    """本地音频归档 + 云存储上传。所有上传操作异步执行，失败不阻塞主流程。"""

    def __init__(self, config: dict):
        self.config = config
        self._storage_dir: Optional[str] = None
        self._enabled = False
        self._init()

    # ----------------------------------------------------------------
    # 本地归档
    # ----------------------------------------------------------------

    def _init(self) -> None:
        """初始化本地音频归档目录。"""
        raw = (self.config.get("local_audio_dir") or "").strip()
        if not raw:
            raw = os.path.join(
                get_astrbot_plugin_data_path(),
                "astrbot_plugin_voice_assistant",
                "tts_archive",
            )
        raw = os.path.realpath(raw)
        try:
            os.makedirs(raw, exist_ok=True)
            self._storage_dir = raw
            self._enabled = True
            retention = self.config.get("local_audio_retention_days", 7)
            cleaned = self.cleanup_old(retention)
            logger.info(
                f"AI Voice Assistant: 音频存储目录 {raw} "
                f"(保留 {retention} 天，本次清理 {cleaned} 个)"
            )
        except OSError as e:
            logger.warning(f"AI Voice Assistant: 无法创建音频存储目录 {raw}: {e}")
            self._enabled = False

    def cleanup_old(self, retention_days: int = 7) -> int:
        """删除超过 retention_days 天的音频文件。返回删除数量。"""
        if not self._storage_dir:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)
        count = 0
        try:
            for fname in os.listdir(self._storage_dir):
                fpath = os.path.join(self._storage_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
                if mtime < cutoff:
                    os.remove(fpath)
                    count += 1
        except OSError as e:
            logger.warning(f"[tts_storage] 清理音频文件失败: {e}")
        return count

    def save_file(self, audio_path: str) -> Optional[str]:
        """将临时音频文件移到本地持久目录。返回持久化路径，失败返回 None。"""
        if not self._enabled or not self._storage_dir:
            return None
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            uid = random.randint(100000, 999999)
            basename = f"tts_{ts}_{uid}.wav"
            dest = os.path.join(self._storage_dir, basename)
            counter = 1
            while os.path.exists(dest):
                dest = os.path.join(
                    self._storage_dir,
                    f"tts_{ts}_{uid}_{counter}.wav",
                )
                counter += 1
            os.rename(audio_path, dest)
            logger.info(f"[tts_storage] 已归档: {dest}")
            return dest
        except Exception as e:
            logger.warning(f"[tts_storage] 归档失败: {e}")
            return None

    # ----------------------------------------------------------------
    # 云存储 —— 派发入口
    # ----------------------------------------------------------------

    def cloud_backup(self, file_path: str, text: str) -> None:
        """将已归档的音频文件上传到云端存储，异步进行，失败不阻塞。"""
        if not self.config.get("cloud_backup_enabled", False):
            return

        backend = self.config.get("cloud_backend", "custom")

        dispatch = {
            "custom": self._put_custom,
            "s3": self._put_s3,
            "webdav": self._put_webdav,
            "smb": self._put_smb,
        }
        method = dispatch.get(backend)
        if not method:
            logger.warning(f"[tts_cloud] 未知后端类型: {backend}")
            return

        try:
            method(file_path, text)
        except Exception as e:
            logger.warning(f"[tts_cloud] {backend} 上传启动失败: {e}")

    # ----------------------------------------------------------------
    # Custom —— 用户自定义 API
    # ----------------------------------------------------------------

    def _put_custom(self, file_path: str, text: str) -> None:
        """自定义 API: multipart/form-data 上传 + JSON 路径提取 URL。"""
        url = (self.config.get("cloud_custom_url") or "").strip()
        if not url:
            logger.warning("[tts_cloud] cloud_custom_url 未配置，跳过")
            return

        headers_raw = (self.config.get("cloud_custom_headers") or "").strip()
        body_raw = (self.config.get("cloud_custom_body") or "").strip()
        result_path = (self.config.get("cloud_custom_result_path") or "").strip()

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = f"{random.randint(100000, 999999):06d}"
        filename = f"tts_{ts}_{uid}.wav"

        cmd = ["curl", "-f", "-s", "-X", "POST"]
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
        needs_extract = bool(result_path)

        async def _upload():
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=120
                )
                if proc.returncode != 0:
                    logger.warning(
                        f"[tts_cloud] 自定义上传失败 (exit={proc.returncode}): "
                        f"{stderr.decode()[:200]}"
                    )
                    return
                body = stdout.decode()
                if needs_extract:
                    got = _extract_json_path(body, result_path)
                    if got:
                        logger.info(f"[tts_cloud] 自定义上传成功 → {got}")
                    else:
                        logger.warning(
                            f"[tts_cloud] 自定义上传成功，未能从响应提取 URL "
                            f"(path={result_path})"
                        )
                else:
                    logger.info("[tts_cloud] 自定义上传成功")
            except asyncio.TimeoutError:
                logger.warning("[tts_cloud] 自定义上传超时")
            except Exception as e:
                logger.warning(f"[tts_cloud] 自定义上传异常: {e}")

        asyncio.create_task(_upload())

    # ----------------------------------------------------------------
    # S3 —— curl --aws-sigv4 签名
    # ----------------------------------------------------------------

    def _put_s3(self, file_path: str, text: str) -> None:
        """S3 兼容存储: 使用 curl --aws-sigv4 签名上传。"""
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
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = f"{random.randint(100000, 999999):06d}"
        key = f"tts_{ts}_{uid}.wav"

        if path_style:
            upload_url = f"{endpoint.rstrip('/')}/{bucket}/{key}"
        else:
            e = endpoint.rstrip("/").lstrip("https://").lstrip("http://")
            upload_url = f"https://{bucket}.{e}/{key}"

        cmd = [
            "curl", "-f", "-s",
            "--aws-sigv4", f"aws:amz:{region}:s3",
            "--user", f"{access_key}:{secret_key}",
            "-X", "PUT",
            "--data-binary", f"@{file_path}",
            upload_url,
        ]

        async def _upload():
            try:
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode == 0:
                    logger.info(f"[tts_cloud] S3 上传成功: {upload_url}")
                else:
                    logger.warning(f"[tts_cloud] S3 上传失败 (exit={proc.returncode}): {stderr.decode()[:200]}")
            except asyncio.TimeoutError:
                logger.warning("[tts_cloud] S3 上传超时")
            except Exception as e:
                logger.warning(f"[tts_cloud] S3 上传异常: {e}")

        asyncio.create_task(_upload())

    # ----------------------------------------------------------------
    # WebDAV —— curl -T + basic auth
    # ----------------------------------------------------------------

    def _put_webdav(self, file_path: str, text: str) -> None:
        """WebDAV: 使用 curl -T 上传。"""
        url = (self.config.get("cloud_webdav_url") or "").strip()
        username = (self.config.get("cloud_webdav_username") or "").strip()
        password = (self.config.get("cloud_webdav_password") or "").strip()

        if not url:
            logger.warning("[tts_cloud] cloud_webdav_url 未配置，跳过")
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = f"{random.randint(100000, 999999):06d}"
        filename = f"tts_{ts}_{uid}.wav"

        cmd = ["curl", "-f", "-s", "-T", file_path]
        if username and password:
            cmd.extend(["-u", f"{username}:{password}"])
        cmd.append(f"{url.rstrip('/')}/{filename}")

        async def _upload():
            try:
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode == 0:
                    logger.info(f"[tts_cloud] WebDAV 上传成功: {filename}")
                else:
                    logger.warning(f"[tts_cloud] WebDAV 上传失败 (exit={proc.returncode}): {stderr.decode()[:200]}")
            except asyncio.TimeoutError:
                logger.warning("[tts_cloud] WebDAV 上传超时")
            except Exception as e:
                logger.warning(f"[tts_cloud] WebDAV 上传异常: {e}")

        asyncio.create_task(_upload())

    # ----------------------------------------------------------------
    # SMB —— smbclient / shutil.copy2 降级
    # ----------------------------------------------------------------

    def _put_smb(self, file_path: str, text: str) -> None:
        """SMB 共享: 使用 smbclient 或 shutil.copy。"""
        share = (self.config.get("cloud_smb_share") or "").strip()
        username = (self.config.get("cloud_smb_username") or "").strip()
        password = (self.config.get("cloud_smb_password") or "").strip()
        domain = (self.config.get("cloud_smb_domain") or "").strip()

        if not share:
            logger.warning("[tts_cloud] cloud_smb_share 未配置，跳过")
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = f"{random.randint(100000, 999999):06d}"
        filename = f"tts_{ts}_{uid}.wav"

        async def _upload():
            try:
                if username:
                    user_part = f"{domain}\\{username}" if domain else username
                    auth = f"{user_part}%{password}" if password else user_part
                    cmd = ["smbclient", share, "-U", auth, "-c", f"put {file_path} {filename}"]
                else:
                    cmd = ["smbclient", share, "-N", "-c", f"put {file_path} {filename}"]
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode == 0:
                    logger.info(f"[tts_cloud] SMB 上传成功: {share}/{filename}")
                else:
                    logger.warning(f"[tts_cloud] SMB 上传失败 (exit={proc.returncode}): {stderr.decode()[:200]}")
            except FileNotFoundError:
                import shutil
                try:
                    norm_share = share.replace("/", "\\")
                    dest = os.path.join(norm_share, filename)
                    shutil.copy2(file_path, dest)
                    logger.info(f"[tts_cloud] SMB 复制成功: {dest}")
                except Exception as e2:
                    logger.warning(f"[tts_cloud] SMB 复制失败: {e2}")
            except asyncio.TimeoutError:
                logger.warning("[tts_cloud] SMB 上传超时")
            except Exception as e:
                logger.warning(f"[tts_cloud] SMB 上传异常: {e}")

        asyncio.create_task(_upload())


# ====================================================================
# 工具函数
# ====================================================================

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
