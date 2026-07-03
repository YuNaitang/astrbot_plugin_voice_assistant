"""
AI Voice Assistant — AstrBot 通用 TTS 编排插件

允许AI 通过工具自主调用 TTS 回复语音。
支持多 Provider 降级、三级权限管理、双层密度控制、长文本分段合并。
"""
import os
import re
import random
import asyncio
import tempfile
import subprocess
from datetime import datetime, timedelta
from math import exp
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star
from astrbot.api import logger
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.message_session import MessageSession as MessageSesion
from astrbot.core.provider.provider import TTSProvider
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


# ====================================================================
# 权限等级常量
# ====================================================================

PERM_UNLIMITED = 0    # 无限制
PERM_BASIC = 1        # 基准限制（速率 + 密度）
PERM_RESTRICTED = 2   # 完全限制（黑名单）

PERM_LABELS = {0: "无限制", 1: "基准限制", 2: "完全限制"}


class Main(Star):
    """AI Voice Assistant — 让 AI 主动调用 TTS 回复语音"""

    def __init__(self, context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 运行时状态
        self._last_tts_time: dict[str, datetime] = {}
        self._temp_files: list[str] = []
        self._providers_logged: bool = False

        # 会话级密度（硬阻断）
        self._voice_timeline: dict[str, list[datetime]] = {}
        self._density_warned: set[str] = set()

        # 用户级密度（概率降权）
        self._user_trigger_timeline: dict[str, dict[str, list[datetime]]] = {}

        # 权限缓存：session_id → level
        self._perm_cache: dict[str, int] = {}
        self._load_permission_cache()

        # 本地音频存储
        self._audio_storage_enabled = False
        self._audio_storage_dir: Optional[str] = None
        self._init_local_storage()

        self._log_available_tts_providers()
        logger.info(
            f"AI Voice Assistant 已加载 "
            f"(enabled={self.config.get('voice_enabled', True)}, "
            f"log_level={self.config.get('log_level', 'info')})"
        )

    async def terminate(self):
        """插件卸载时清理临时音频文件"""
        for f in self._temp_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass
        self._temp_files.clear()
        logger.info("AI Voice Assistant 已卸载")

    # ------------------------------------------------
    # 本地音频存储 + 7 天清理
    # ------------------------------------------------

    def _init_local_storage(self) -> None:
        """初始化本地音频归档目录，使用 AstrBot 官方插件数据目录。"""
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
            self._audio_storage_dir = raw
            self._audio_storage_enabled = True
            # 启动时清理过期文件
            retention = self.config.get("local_audio_retention_days", 7)
            cleaned = self._cleanup_old_audio(retention)
            logger.info(
                f"AI Voice Assistant: 音频存储目录 {raw} "
                f"(保留 {retention} 天，本次清理 {cleaned} 个)"
            )
        except OSError as e:
            logger.warning(f"AI Voice Assistant: 无法创建音频存储目录 {raw}: {e}")
            self._audio_storage_enabled = False

    def _cleanup_old_audio(self, retention_days: int = 7) -> int:
        """删除超过 retention_days 天的音频文件。返回删除数量。"""
        if not self._audio_storage_dir:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)
        count = 0
        try:
            for fname in os.listdir(self._audio_storage_dir):
                fpath = os.path.join(self._audio_storage_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
                if mtime < cutoff:
                    os.remove(fpath)
                    count += 1
        except OSError as e:
            logger.warning(f"[tts_storage] 清理音频文件失败: {e}")
        return count

    def _save_audio_file(self, audio_path: str) -> Optional[str]:
        """将临时音频文件移到本地持久目录。返回持久化路径，失败返回 None。"""
        if not self._audio_storage_enabled or not self._audio_storage_dir:
            return None
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            uid = random.randint(100000, 999999)
            basename = f"tts_{ts}_{uid}.wav"
            dest = os.path.join(self._audio_storage_dir, basename)
            # 避免同名覆盖
            counter = 1
            while os.path.exists(dest):
                dest = os.path.join(
                    self._audio_storage_dir,
                    f"tts_{ts}_{uid}_{counter}.wav",
                )
                counter += 1
            os.rename(audio_path, dest)
            logger.info(f"[tts_storage] 已归档: {dest}")
            return dest
        except Exception as e:
            logger.warning(f"[tts_storage] 归档失败: {e}")
            return None

    def _cloud_backup(self, file_path: str, text: str) -> None:
        """将已归档的音频文件上传到云端存储，异步进行，失败不阻塞。"""
        if not self.config.get("cloud_backup_enabled", False):
            return

        backend = self.config.get("cloud_backend", "custom")

        dispatch = {
            "custom": self._cloud_put_custom,
            "s3": self._cloud_put_s3,
            "webdav": self._cloud_put_webdav,
            "smb": self._cloud_put_smb,
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
    # 云存储后端
    # ----------------------------------------------------------------

    def _cloud_put_custom(self, file_path: str, text: str) -> None:
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

        # 请求头
        for line in headers_raw.splitlines():
            line = line.strip()
            if ":" in line:
                cmd.extend(["-H", line])

        # 文件字段（固定为 file）
        cmd.extend(["-F", f"file=@{file_path};filename={filename}"])

        # 请求体额外字段
        if body_raw:
            try:
                import json as _json
                body_obj = _json.loads(body_raw)
                for k, v in body_obj.items():
                    cmd.extend(["-F", f"{k}={v}"])
            except _json.JSONDecodeError:
                logger.warning("[tts_cloud] cloud_custom_body 不是合法 JSON，跳过")
                logger.warning(f"[tts_cloud] 收到: {body_raw!r}")

        cmd.append(url)

        # 标记是否需要解析返回 URL
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
                    url = self._extract_json_path(body, result_path)
                    if url:
                        logger.info(f"[tts_cloud] 自定义上传成功 → {url}")
                    else:
                        logger.warning(
                            f"[tts_cloud] 自定义上传成功，但未能从响应提取 URL "
                            f"(path={result_path})"
                        )
                        logger.debug(f"[tts_cloud] 响应体: {body[:500]}")
                else:
                    logger.info("[tts_cloud] 自定义上传成功")
            except asyncio.TimeoutError:
                logger.warning("[tts_cloud] 自定义上传超时")
            except Exception as e:
                logger.warning(f"[tts_cloud] 自定义上传异常: {e}")

        asyncio.create_task(_upload())

    @staticmethod
    def _extract_json_path(body: str, json_path: str) -> Optional[str]:
        """从 JSON 字符串中按点号路径提取值。例: 'data.url' → result['data']['url']"""
        import json as _json
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

    def _cloud_put_s3(self, file_path: str, text: str) -> None:
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

        # 构造上传 URL
        if path_style:
            upload_url = f"{endpoint.rstrip('/')}/{bucket}/{key}"
        else:
            upload_url = f"https://{bucket}.{endpoint.rstrip('/').lstrip('https://').lstrip('http://')}/{key}"

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
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode == 0:
                    logger.info(f"[tts_cloud] S3 上传成功: {upload_url}")
                else:
                    logger.warning(
                        f"[tts_cloud] S3 上传失败 (exit={proc.returncode}): "
                        f"{stderr.decode()[:200]}"
                    )
            except asyncio.TimeoutError:
                logger.warning("[tts_cloud] S3 上传超时")
            except Exception as e:
                logger.warning(f"[tts_cloud] S3 上传异常: {e}")

        asyncio.create_task(_upload())

    def _cloud_put_webdav(self, file_path: str, text: str) -> None:
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
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode == 0:
                    logger.info(f"[tts_cloud] WebDAV 上传成功: {filename}")
                else:
                    logger.warning(
                        f"[tts_cloud] WebDAV 上传失败 (exit={proc.returncode}): "
                        f"{stderr.decode()[:200]}"
                    )
            except asyncio.TimeoutError:
                logger.warning("[tts_cloud] WebDAV 上传超时")
            except Exception as e:
                logger.warning(f"[tts_cloud] WebDAV 上传异常: {e}")

        asyncio.create_task(_upload())

    def _cloud_put_smb(self, file_path: str, text: str) -> None:
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
                # 先尝试 smbclient
                if username:
                    user_part = f"{domain}\\{username}" if domain else username
                    auth = f"{user_part}%{password}" if password else user_part
                    cmd = [
                        "smbclient", share,
                        "-U", auth,
                        "-c", f"put {file_path} {filename}",
                    ]
                else:
                    cmd = [
                        "smbclient", share,
                        "-N",
                        "-c", f"put {file_path} {filename}",
                    ]

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=120
                )
                if proc.returncode == 0:
                    logger.info(f"[tts_cloud] SMB 上传成功: {share}/{filename}")
                else:
                    logger.warning(
                        f"[tts_cloud] SMB 上传失败 (exit={proc.returncode}): "
                        f"{stderr.decode()[:200]}"
                    )
            except FileNotFoundError:
                # smbclient 不可用，降级为 shutil.copy
                try:
                    import shutil
                    # 将 Windows 路径格式统一
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

    # ------------------------------------------------
    # Provider 发现
    # ------------------------------------------------

    def _log_available_tts_providers(self, force: bool = False):
        """打印所有已注册 TTS Provider（仅第一次成功时输出）"""
        if self._providers_logged and not force:
            return

        try:
            providers = self.context.get_all_tts_providers()
        except Exception as e:
            logger.debug(f"获取 TTS Provider 列表失败（可能尚未初始化）: {e}")
            return

        if not providers:
            return

        logger.info(f"AI Voice Assistant: 发现 {len(providers)} 个 TTS Provider:")
        for p in providers:
            try:
                meta = p.meta()
                logger.info(f"  · id={meta.id}  type={meta.type}  model={meta.model or 'N/A'}")
            except Exception:
                logger.info(f"  · (无法获取元数据的 Provider: {type(p).__name__})")

        self._providers_logged = True

    # ------------------------------------------------
    # 权限等级管理
    # ------------------------------------------------

    def _load_permission_cache(self):
        """从配置加载权限映射到缓存。

        支持格式：
        - session_permissions: ["session_id:level", ...]（新格式）
        - sessions_blacklist: [...] → 自动映射为 level=2（兼容旧格式）
        """
        self._perm_cache.clear()

        # 1. 新格式：session_permissions
        entries = self.config.get("session_permissions", []) or []
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.rsplit(":", 1)
            if len(parts) == 2:
                sid, level_str = parts
                try:
                    level = int(level_str)
                    if level in (PERM_UNLIMITED, PERM_BASIC, PERM_RESTRICTED):
                        self._perm_cache[sid.strip()] = level
                except ValueError:
                    logger.warning(f"[voice_perm] 无效的权限配置条目: {entry}")

        # 2. 兼容旧格式：sessions_blacklist → level=2
        blacklist = self.config.get("sessions_blacklist", []) or []
        for sid in blacklist:
            sid = sid.strip()
            if sid and sid not in self._perm_cache:
                self._perm_cache[sid] = PERM_RESTRICTED

    def _get_session_permission_level(self, event: AstrMessageEvent) -> int:
        """获取会话的语音权限等级。

        查找顺序：
        1. 完整 session ID 精确匹配
        2. QQ 号匹配（私聊/群聊 session_id）
        3. 管理员私聊 → 默认 UNLIMITED
        4. 全局默认等级
        """
        session_str = str(event.session)
        msg_type = event.session.message_type
        sid = event.session.session_id

        # 1. 完整 session ID 匹配
        if session_str in self._perm_cache:
            return self._perm_cache[session_str]

        # 2. QQ 号匹配
        if sid and sid in self._perm_cache:
            return self._perm_cache[sid]

        # 3. 管理员私聊 → 默认无限制
        if event.is_admin() and msg_type == MessageType.FRIEND_MESSAGE:
            return PERM_UNLIMITED

        # 4. 全局默认
        return self.config.get("default_permission_level", PERM_BASIC)

    def _save_permission(self, session_id: str, level: int):
        """保存权限到配置并刷新缓存。"""
        entries = self.config.get("session_permissions", []) or []
        prefix = f"{session_id}:"
        new_entries = [e for e in entries if not e.startswith(prefix)]
        new_entries.append(f"{session_id}:{level}")
        self.config["session_permissions"] = new_entries
        self._load_permission_cache()
        self._persist_config()

    def _remove_permission(self, session_id: str):
        """删除自定义权限配置，恢复默认等级。"""
        entries = self.config.get("session_permissions", []) or []
        prefix = f"{session_id}:"
        self.config["session_permissions"] = [e for e in entries if not e.startswith(prefix)]
        self._load_permission_cache()
        self._persist_config()

    def _persist_config(self):
        """尝试持久化配置到文件。"""
        try:
            import json
            import os
            config_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "..", "data", "config",
                "astrbot_plugin_voice_assistant.json",
            )
            # 标准化路径
            config_path = os.path.normpath(config_path)
            if os.path.exists(os.path.dirname(config_path)):
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, ensure_ascii=False, indent=2)
                logger.debug(f"[voice_perm] 配置已持久化: {config_path}")
        except Exception as e:
            logger.debug(f"[voice_perm] 配置持久化失败（非致命）: {e}")

    # ------------------------------------------------
    # LLM 请求注入（密度提醒 + extra prompt）
    # ------------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """每次 LLM 请求前，注入语音相关系统提示"""
        # —— 注入 extra prompt ——
        extra = self.config.get("voice_prompt_extra", "")
        if extra:
            req.system_prompt += f"\n\n[语音行为规则]\n{extra}"

        # —— 会话级密度超限提醒（每个窗口只提醒一次） ——
        session_id = str(event.session)
        if self._is_over_density_limit(session_id):
            if session_id not in self._density_warned:
                req.system_prompt += (
                    "\n\n[注意] 你最近已经发送了很多语音消息。"
                    "在收到重置通知之前，请不要再使用 ai_speak 工具。"
                )
                self._density_warned.add(session_id)

    # ------------------------------------------------
    # 双层密度控制
    # ------------------------------------------------

    @staticmethod
    def _prune_timeline(
        timestamps: list[datetime], window_minutes: int
    ) -> list[datetime]:
        """裁剪滑动窗口外的时间戳"""
        now = datetime.now()
        cutoff = now - timedelta(minutes=window_minutes)
        return [t for t in timestamps if t > cutoff]

    def _is_over_density_limit(self, session_id: str) -> bool:
        """会话级硬阻断：超限后完全阻止语音。"""
        window = self.config.get("density_window_minutes", 10)
        max_count = self.config.get("density_max_count", 3)
        timeline = self._voice_timeline.get(session_id, [])
        old_count = len(timeline)
        timeline = self._prune_timeline(timeline, window)
        new_count = len(timeline)
        self._voice_timeline[session_id] = timeline

        is_over = new_count >= max_count
        logger.info(
            f"[密度判定-会话] session={session_id} "
            f"window={window}min max={max_count} "
            f"裁剪前={old_count} 裁剪后={new_count} "
            f"结果={'超限' if is_over else '放行'}"
        )
        return is_over

    def _get_user_probability(self, session_id: str, user_id: str) -> float:
        """Logistic 曲线计算用户级概率降权系数。"""
        window = self.config.get("user_density_window_minutes", 60)
        threshold = self.config.get("user_density_threshold", 5)
        steepness = self.config.get("user_density_curve_steepness", 0.7)

        if steepness <= 0:
            logger.info(
                f"[密度判定-用户] session={session_id} user={user_id} "
                f"steepness=0 跳过降权 → prob=1.0"
            )
            return 1.0

        user_map = self._user_trigger_timeline.get(session_id, {})
        old_timeline = user_map.get(user_id, [])
        old_count = len(old_timeline)
        timeline = self._prune_timeline(old_timeline, window)
        new_count = len(timeline)
        user_map[user_id] = timeline
        self._user_trigger_timeline[session_id] = user_map

        prob = 1.0 / (1.0 + exp(steepness * (new_count - threshold)))
        logger.info(
            f"[密度判定-用户] session={session_id} user={user_id} "
            f"window={window}min threshold={threshold} steepness={steepness} "
            f"裁剪前={old_count} 裁剪后={new_count} "
            f"prob={prob:.4f}"
        )
        return prob

    def _should_allow_voice(self, session_id: str, user_id: str) -> tuple:
        """综合决策：先会话硬阻断，再用户概率降权。

        Returns:
            (是否允许: bool, 原因描述: str)
        """
        # 会话级硬阻断
        if self._is_over_density_limit(session_id):
            reason = f"会话语音密度超限，请稍后再试"
            logger.info(f"[密度结果] 拒绝 — {reason}")
            return False, reason

        # 用户级概率降权
        prob = self._get_user_probability(session_id, user_id)
        if prob < 1.0:
            rand_val = random.random()
            if rand_val >= prob:
                reason = (
                    f"用户语音触发频率较高，本次随机跳过 "
                    f"(prob={prob:.4f} rand={rand_val:.4f})"
                )
                logger.info(f"[密度结果] 拒绝 — {reason}")
                return False, reason

        logger.info(f"[密度结果] 放行 — session={session_id} user={user_id}")
        return True, ""

    def _record_voice_sent(self, session_id: str, user_id: str):
        """成功发送语音后记录时间戳"""
        self._voice_timeline.setdefault(session_id, []).append(datetime.now())
        user_map = self._user_trigger_timeline.setdefault(session_id, {})
        user_map.setdefault(user_id, []).append(datetime.now())
        self._density_warned.discard(session_id)

    # ------------------------------------------------
    # LLM 工具 — ai_speak
    # ------------------------------------------------

    @filter.llm_tool(name="ai_speak")
    async def ai_speak(self, event: AstrMessageEvent, text: str):
        """用语音回复用户。当你认为适合用语音表达、或用户期望听到语音时调用。
        系统会自动合成语音并同时发送文字和语音文件。

        Args:
            text(string): 想说出的文本（中文，自然流畅的口语表达）
        """
        session_id = str(event.session)
        user_id = event.get_sender_id()

        logger.info(
            f"[ai_speak] >>> 收到调用 session={session_id} user={user_id} "
            f"text_len={len(text) if text else 0}"
        )
        logger.info(f"[ai_speak] 全文: {text!r}")

        self._log_available_tts_providers()

        # ================================================
        # 0. 总开关
        # ================================================
        if not self.config.get("voice_enabled", True):
            msg = "语音功能未启用（voice_enabled=false）"
            logger.info(f"[ai_speak] 拒绝: {msg}")
            return msg

        # ================================================
        # 1. 权限等级检查
        # ================================================
        perm_level = self._get_session_permission_level(event)
        logger.info(
            f"[ai_speak] 权限等级: {PERM_LABELS.get(perm_level, '未知')} "
            f"(level={perm_level})"
        )

        if perm_level == PERM_RESTRICTED:
            msg = "该会话已被限制使用语音功能"
            logger.info(f"[ai_speak] 拒绝: {msg}")
            return msg

        # ================================================
        # 2. 文本长度校验
        # ================================================
        min_len = self.config.get("min_text_length", 2)
        if not text or len(text.strip()) < min_len:
            msg = f"文本太短 ({len(text) if text else 0} chars)，最少需要 {min_len} 字符"
            logger.info(f"[ai_speak] 跳过: {msg}")
            return msg

        # ================================================
        # 3. 基准限制等级：速率 + 密度检查
        # ================================================
        if perm_level == PERM_BASIC:
            # 速率限制
            rate_msg = self._check_rate_limit(session_id)
            if rate_msg:
                logger.info(f"[ai_speak] 拒绝: {rate_msg}")
                return rate_msg

            # 双层密度
            allowed, reason = self._should_allow_voice(session_id, user_id)
            if not allowed:
                logger.info(f"[ai_speak] 拒绝: {reason}")
                return reason

        # ================================================
        # 4. 获取 TTS Provider
        # ================================================
        provider = self._get_tts_provider(event)
        if provider is None:
            msg = "语音合成失败：未找到可用的 TTS 服务，请检查 AstrBot 的 TTS 提供商配置。"
            logger.warning(f"[ai_speak] {msg}")
            return msg

        try:
            provider_meta = provider.meta()
            logger.info(
                f"[ai_speak] TTS Provider: id={provider_meta.id} "
                f"type={provider_meta.type}"
            )
        except Exception:
            pass

        # ================================================
        # 5. 文本分段（长文本处理）
        # ================================================
        segment_max_chars = self.config.get("tts_segment_max_chars", 80)
        segments = self._segment_text(text.strip(), segment_max_chars)
        logger.info(
            f"[ai_speak] 文本分段: {len(segments)} 段 "
            f"(max_chars={segment_max_chars})"
        )
        for i, seg in enumerate(segments):
            logger.info(f"[ai_speak]   段{i+1}/{len(segments)}: "
                        f"len={len(seg)} [{seg[:60]}{'...' if len(seg) > 60 else ''}]")

        # ================================================
        # 6. TTS 合成（逐段）
        # ================================================
        audio_paths = []
        for i, seg in enumerate(segments):
            try:
                logger.info(
                    f"[ai_speak] TTS合成 段{i+1}/{len(segments)}: "
                    f"text={seg!r}"
                )
                audio_path = await provider.get_audio(seg)
                logger.info(
                    f"[ai_speak] TTS合成完成 段{i+1}: "
                    f"path={audio_path}"
                )
                audio_paths.append(audio_path)
                self._temp_files.append(audio_path)
            except Exception as e:
                provider_id = "?"
                try:
                    provider_id = provider.meta().id
                except Exception:
                    pass
                logger.error(
                    f"[ai_speak] TTS合成失败 段{i+1} "
                    f"(provider={provider_id}): {e}"
                )
                return f"语音合成失败（{provider_id}）：{e!s}"

        # ================================================
        # 7. 合并音频（仅在启用且多段时尝试）
        # ================================================
        merge_enabled = self.config.get("tts_merge_enabled", False)
        if merge_enabled and len(audio_paths) > 1:
            merge_timeout = self.config.get("tts_merge_timeout_seconds", 30)
            logger.info(
                f"[ai_speak] 开始合并 {len(audio_paths)} 段音频 "
                f"(timeout={merge_timeout}s)"
            )
            try:
                final_audio = await asyncio.wait_for(
                    asyncio.to_thread(self._merge_audio_files, audio_paths),
                    timeout=merge_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[ai_speak] 音频合并超时 ({merge_timeout}s)，将分段发送"
                )
                final_audio = None
        elif len(audio_paths) == 1:
            final_audio = audio_paths[0]
        else:
            # 多段但未启用合并 → 直接分段发送
            final_audio = None

        # ================================================
        # 8. 发送消息
        # ================================================
        self._last_tts_time[session_id] = datetime.now()
        self._record_voice_sent(session_id, user_id)

        if final_audio is None and len(audio_paths) > 1:
            # 合并失败 / 超时 → 分段发送
            logger.info(
                f"[ai_speak] 分段发送 {len(audio_paths)} 条语音 "
                f"session={session_id}"
            )
            for i, (seg, ap) in enumerate(zip(segments, audio_paths)):
                await event.send(MessageChain([
                    Plain(f"[{i+1}/{len(segments)}] {seg}"),
                    Record.fromFileSystem(ap),
                ]))
                logger.info(
                    f"[ai_speak] 已发送 段{i+1}/{len(segments)} "
                    f"session={session_id}"
                )
            result_msg = f"语音消息已分段发送（共 {len(segments)} 段）"
        else:
            # 成功合并 / 单段 → 一条发送
            display_text = text if len(text) <= 200 else text[:200] + "..."
            logger.info(
                f"[ai_speak] 发送消息: text_len={len(text)} "
                f"audio={'merged' if len(audio_paths) > 1 else 'single'} "
                f"session={session_id}"
            )
            await event.send(MessageChain([
                Plain(display_text),
                Record.fromFileSystem(final_audio),
            ]))
            logger.info(f"[ai_speak] 已发送 session={session_id}")

            if len(segments) > 1:
                result_msg = f"语音消息已发送成功（{len(segments)} 段已合并）"
            else:
                result_msg = "语音消息已发送成功"

        logger.info(
            f"[ai_speak] <<< 完成 session={session_id} user={user_id} "
            f"segments={len(segments)}"
        )

        # ================================================
        # 9. 备用会话发送（best-effort）
        # ================================================
        await self._send_backup(text, final_audio, segments, audio_paths, event)

        # ================================================
        # 10. 本地归档 + 云存储（best-effort）
        # ================================================
        archivable = final_audio if final_audio else (audio_paths[0] if audio_paths else None)
        if archivable:
            archived = self._save_audio_file(archivable)
            if archived:
                self._cloud_backup(archived, text)
        elif len(audio_paths) > 1:
            for ap in audio_paths[1:]:
                self._save_audio_file(ap)

        retention = self.config.get('local_audio_retention_days', 7)
        cleaned = self._cleanup_old_audio(retention)
        if cleaned:
            logger.info(f'[tts_storage] 后台清理: 删除 {cleaned} 个过期文件')

        return result_msg

    # ------------------------------------------------
    # 备用会话发送
    # ------------------------------------------------

    async def _send_backup(self, text: str, final_audio: str, segments: list,
                           audio_paths: list, event: AstrMessageEvent):
        """将语音备份发送到指定 QQ 群/好友，失败不影响主流程。"""
        backup = (self.config.get("backup_session_id") or "").strip()
        if not backup:
            return

        # 解析备份目标: "123456" = 群聊, "123456:friend" = 私聊
        is_private = False
        if ":friend" in backup:
            backup = backup.replace(":friend", "").strip()
            is_private = True
        elif ":group" in backup:
            backup = backup.replace(":group", "").strip()

        if not backup or not backup.isdigit():
            logger.warning(f"[ai_speak] 备份发送: 无效的 QQ 号 '{backup}'，跳过")
            return

        # 构造目标会话（复用当前事件的平台 ID）
        session = MessageSesion(
            event.session.platform_id,
            MessageType.FRIEND_MESSAGE if is_private else MessageType.GROUP_MESSAGE,
            backup,
        )

        logger.info(f"[ai_speak] 备份发送到 QQ: {session}")
        try:
            if final_audio and len(audio_paths) <= 1:
                # 单段或已合并 → 一条发送
                display_text = text if len(text) <= 200 else text[:200] + "..."
                await self.context.send_by_session(
                    session,
                    MessageChain([
                        Plain(display_text),
                        Record.fromFileSystem(final_audio),
                    ])
                )
            elif audio_paths:
                # 多段 → 逐段发送
                for i, (seg, ap) in enumerate(zip(segments, audio_paths)):
                    await self.context.send_by_session(
                        session,
                        MessageChain([
                            Plain(f"[{i+1}/{len(segments)}] {seg}"),
                            Record.fromFileSystem(ap),
                        ])
                    )
            logger.info(f"[ai_speak] 备份发送完成: {session}")
        except Exception as e:
            logger.warning(f"[ai_speak] 备份发送失败 ({session}): {e}")

    # ------------------------------------------------
    # Provider 选取：首选 → 兜底 → 系统默认
    # ------------------------------------------------

    def _get_tts_provider(self, event: AstrMessageEvent) -> Optional[TTSProvider]:
        """三级降级获取 TTS Provider"""
        provider = self._resolve_provider(
            self.config.get("tts_provider_id", "")
        )
        if provider is not None:
            return provider

        provider = self._resolve_provider(
            self.config.get("tts_fallback_provider_id", "")
        )
        if provider is not None:
            logger.info("ai_speak: 使用兜底 TTS Provider")
            return provider

        return self.context.get_using_tts_provider(event.unified_msg_origin)

    def _resolve_provider(self, provider_id: str) -> Optional[TTSProvider]:
        """按 ID 查找 Provider，过滤非 TTS 类型"""
        if not provider_id:
            return None
        p = self.context.get_provider_by_id(provider_id)
        if p is None:
            logger.warning(f"ai_speak: Provider ID '{provider_id}' 未找到")
            return None
        if not isinstance(p, TTSProvider):
            logger.warning(
                f"ai_speak: Provider '{provider_id}' 不是 TTSProvider "
                f"（{type(p).__name__}）"
            )
            return None
        return p

    # ------------------------------------------------
    # 速率限制
    # ------------------------------------------------

    def _check_rate_limit(self, session_id: str) -> Optional[str]:
        """检查会话级速率限制。返回拦截原因或 None（放行）。"""
        rate_seconds = self.config.get("rate_limit_seconds", 5)
        if rate_seconds <= 0:
            return None

        last_time = self._last_tts_time.get(session_id)
        if last_time is None:
            return None

        elapsed = (datetime.now() - last_time).total_seconds()
        if elapsed < rate_seconds:
            msg = (
                f"会话频率限制（距上次 {elapsed:.1f}s，"
                f"需等待 {rate_seconds}s）"
            )
            logger.info(
                f"[ai_speak] 速率限制: session={session_id} "
                f"elapsed={elapsed:.1f}s < {rate_seconds}s"
            )
            return msg
        return None

    # ------------------------------------------------
    # 长文本分段
    # ------------------------------------------------

    def _segment_text(self, text: str, max_chars: int = 80) -> list[str]:
        """将长文本按换行符、句号等分割为适合 TTS 的小段。

        分割策略（按优先级）：
        1. 先按换行符 \\n 分割
        2. 再按句尾标点（。！？.!?）分割
        3. 每段最多 max_chars 字符
        4. 单句超长时强制按 max_chars 切分

        Args:
            text: 原始文本
            max_chars: 每段最大字符数

        Returns:
            分段后的文本列表，保证非空
        """
        if len(text) <= max_chars:
            return [text]

        segments = []

        # 第一步：按换行符分段
        paragraphs = text.split('\n')

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(para) <= max_chars:
                segments.append(para)
                continue

            # 第二步：按句尾标点分割
            sentences = re.split(r'(?<=[。！？.!?])', para)
            current = ""

            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue

                if len(current) + len(sent) <= max_chars:
                    current += sent
                else:
                    if current:
                        segments.append(current)

                    # 单句超长，强制切分
                    if len(sent) > max_chars:
                        for i in range(0, len(sent), max_chars):
                            segments.append(sent[i:i + max_chars])
                        current = ""
                    else:
                        current = sent

            if current:
                segments.append(current)

        # 确保至少返回一段
        return segments if segments else [text[:max_chars]]

    # ------------------------------------------------
    # 音频合并
    # ------------------------------------------------

    def _merge_audio_files(self, audio_paths: list[str]) -> Optional[str]:
        """合并多个音频文件为一个。

        降级策略：pydub → ffmpeg → wave → None（分段发送）

        Args:
            audio_paths: 音频文件路径列表

        Returns:
            合并后的文件路径，或 None（所有方法均失败时）
        """
        if not audio_paths:
            return None
        if len(audio_paths) == 1:
            return audio_paths[0]

        output_dir = os.path.dirname(audio_paths[0])
        output_path = os.path.join(output_dir, "tts_merged.wav")

        # ---- 方法 1: pydub ----
        try:
            from pydub import AudioSegment
            logger.info(
                f"[merge] 使用 pydub 合并 {len(audio_paths)} 个音频文件"
            )
            combined = AudioSegment.empty()
            for path in audio_paths:
                segment = AudioSegment.from_file(path)
                combined += segment
            combined.export(output_path, format="wav")
            logger.info(f"[merge] pydub 合并完成: {output_path}")
            self._temp_files.append(output_path)
            return output_path
        except ImportError:
            logger.debug("[merge] pydub 未安装，尝试下一方案")
        except Exception as e:
            logger.warning(f"[merge] pydub 合并失败: {e}，尝试下一方案")

        # ---- 方法 2: ffmpeg 命令行 ----
        try:
            concat_list = tempfile.NamedTemporaryFile(
                mode='w', suffix='.txt', delete=False, encoding='utf-8'
            )
            for path in audio_paths:
                abs_path = os.path.abspath(path).replace('\\', '/')
                concat_list.write(f"file '{abs_path}'\n")
            concat_list.close()

            logger.info(
                f"[merge] 使用 ffmpeg 合并 {len(audio_paths)} 个音频文件"
            )
            result = subprocess.run(
                ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                 '-i', concat_list.name, '-c', 'copy', output_path],
                capture_output=True, text=True, timeout=50,
            )
            os.unlink(concat_list.name)

            if result.returncode == 0 and os.path.exists(output_path):
                logger.info(f"[merge] ffmpeg 合并完成: {output_path}")
                self._temp_files.append(output_path)
                return output_path
            else:
                logger.warning(f"[merge] ffmpeg 合并失败: {result.stderr}")
        except FileNotFoundError:
            logger.debug("[merge] ffmpeg 未安装，尝试下一方案")
        except subprocess.TimeoutExpired:
            logger.warning("[merge] ffmpeg 合并超时")
        except Exception as e:
            logger.warning(f"[merge] ffmpeg 合并异常: {e}")

        # ---- 方法 3: wave 模块（仅 WAV） ----
        try:
            import wave
            logger.info(
                f"[merge] 使用 wave 模块合并 {len(audio_paths)} 个 WAV 文件"
            )
            params = None
            frames = []
            for path in audio_paths:
                with wave.open(path, 'rb') as wf:
                    if params is None:
                        params = wf.getparams()
                    frames.append(wf.readframes(wf.getnframes()))

            with wave.open(output_path, 'wb') as wf:
                wf.setparams(params)
                for f in frames:
                    wf.writeframes(f)

            logger.info(f"[merge] wave 合并完成: {output_path}")
            self._temp_files.append(output_path)
            return output_path
        except Exception as e:
            logger.warning(f"[merge] wave 合并失败: {e}")

        logger.error("[merge] 所有合并方法均失败，将分段发送")
        return None

    # ------------------------------------------------
    # 命令：语音权限管理
    # ------------------------------------------------

    @filter.command("voice_perm")
    async def cmd_voice_perm(self, event: AstrMessageEvent):
        """管理语音权限等级。

        用法:
          /voice_perm set <session_id> <0|1|2>  — 设置权限
          /voice_perm get [session_id]           — 查询权限
          /voice_perm list                       — 列出所有自定义权限
          /voice_perm help                       — 显示帮助
          /voice_perm del <session_id>           — 删除自定义权限

        等级: 0=无限制 1=基准限制 2=完全限制
        """
        if not event.is_admin():
            await event.send(MessageChain([
                Plain("❌ 权限不足：仅管理员可管理语音权限")
            ]))
            return

        raw_msg = event.get_message_str()
        parts = raw_msg.strip().split()

        if len(parts) < 2:
            await event.send(MessageChain([Plain(
                "📋 语音权限管理\n\n"
                "/voice_perm set <session_id> <0|1|2>\n"
                "/voice_perm get [session_id]\n"
                "/voice_perm list\n"
                "/voice_perm del <session_id>\n"
                "/voice_perm help\n\n"
                "用 /sid 获取当前会话 ID"
            )]))
            return

        action = parts[1].lower()

        # --- help ---
        if action == "help":
            await event.send(MessageChain([Plain(
                "📋 语音权限管理\n\n"
                "/voice_perm set <session_id> <0|1|2>\n"
                "  设置会话的语音权限等级\n"
                "  0 = 无限制（不进行任何限制）\n"
                "  1 = 基准限制（速率+密度控制，默认）\n"
                "  2 = 完全限制（禁止语音，即黑名单）\n\n"
                "/voice_perm get [session_id]\n"
                "  查询会话的权限等级（不传=当前会话）\n\n"
                "/voice_perm list\n"
                "  列出所有自定义权限配置\n\n"
                "/voice_perm del <session_id>\n"
                "  删除自定义权限，恢复默认等级\n\n"
                "用 /sid 获取当前会话的完整 ID\n"
                "管理员的私聊会话默认为无限制等级"
            )]))
            return

        # --- list ---
        if action == "list":
            entries = self.config.get("session_permissions", []) or []
            if not entries:
                default_label = PERM_LABELS.get(
                    self.config.get("default_permission_level", PERM_BASIC), "?"
                )
                await event.send(MessageChain([Plain(
                    f"📋 暂无自定义权限配置\n"
                    f"全部会话使用默认等级: {default_label}"
                )]))
            else:
                lines = ["📋 自定义权限列表:"]
                for entry in sorted(entries):
                    entry = entry.strip()
                    if ':' in entry:
                        parts_entry = entry.rsplit(":", 1)
                        if len(parts_entry) == 2:
                            sid, lvl_str = parts_entry
                            try:
                                lvl = int(lvl_str)
                                label = PERM_LABELS.get(lvl, f"未知({lvl})")
                            except ValueError:
                                label = f"无效({lvl_str})"
                            lines.append(f"  {sid} → {label}")
                default_label = PERM_LABELS.get(
                    self.config.get("default_permission_level", PERM_BASIC), "?"
                )
                lines.append(f"\n默认等级: {default_label}")
                await event.send(MessageChain([Plain("\n".join(lines))]))
            return

        # --- get ---
        if action == "get":
            if len(parts) >= 3:
                target_sid = parts[2]
            else:
                target_sid = str(event.session)

            # 查找等级
            level = self._perm_cache.get(target_sid)
            if level is None:
                level = self.config.get("default_permission_level", PERM_BASIC)
                source = "默认"
            else:
                source = "自定义"

            label = PERM_LABELS.get(level, f"未知({level})")
            await event.send(MessageChain([Plain(
                f"📋 会话: {target_sid}\n"
                f"等级: {label} (level={level})\n"
                f"来源: {source}"
            )]))
            return

        # --- set ---
        if action == "set":
            if len(parts) < 4:
                await event.send(MessageChain([Plain(
                    "❌ 用法: /voice_perm set <session_id> <0|1|2>\n"
                    "用 /sid 获取当前会话 ID"
                )]))
                return

            target_sid = parts[2]
            try:
                level = int(parts[3])
                if level not in (PERM_UNLIMITED, PERM_BASIC, PERM_RESTRICTED):
                    raise ValueError
            except ValueError:
                await event.send(MessageChain([Plain(
                    "❌ 等级必须为 0/1/2\n"
                    "  0 = 无限制  1 = 基准限制  2 = 完全限制"
                )]))
                return

            self._save_permission(target_sid, level)
            label = PERM_LABELS[level]
            await event.send(MessageChain([Plain(
                f"✅ 已设置: {target_sid} → {label} (level={level})"
            )]))
            logger.info(
                f"[voice_perm] 管理员设置权限: "
                f"{target_sid} → {label}"
            )
            return

        # --- del ---
        if action == "del":
            if len(parts) < 3:
                await event.send(MessageChain([Plain(
                    "❌ 用法: /voice_perm del <session_id>"
                )]))
                return

            target_sid = parts[2]
            self._remove_permission(target_sid)
            default_level = self.config.get("default_permission_level", PERM_BASIC)
            default_label = PERM_LABELS.get(default_level, "?")
            await event.send(MessageChain([Plain(
                f"✅ 已删除自定义权限: {target_sid}\n"
                f"已恢复默认等级: {default_label}"
            )]))
            logger.info(
                f"[voice_perm] 管理员删除权限: {target_sid}"
            )
            return

        # --- unknown ---
        await event.send(MessageChain([Plain(
            f"❌ 未知操作: {action}\n"
            f"用法: /voice_perm set|get|list|del|help"
        )]))
