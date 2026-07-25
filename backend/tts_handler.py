"""聆音 — TTS 编排核心。处理 ai_speak 工具的权限检查→合成→发送→备份全流程。"""
import asyncio
import os
from datetime import datetime
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import File, Plain, Record
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.provider.provider import TTSProvider

from ..storage.base import CloudProvider
from ..storage.custom_api import CustomApiProvider
from ..storage.local import LocalArchive
from ..storage.s3 import S3Provider
from ..storage.webdav import WebDAVProvider

from .density import DensityController
from .permissions import (
    PERM_BASIC, PERM_LABELS, PERM_RESTRICTED, PERM_UNLIMITED,
    PermissionManager,
)
from .synthesizer import segment_text, synthesize_segments, merge_audio_files, format_bytes, format_file_size

PERM_LABEL_MAP = {
    PERM_UNLIMITED: "无限制",
    PERM_BASIC: "基准限制",
    PERM_RESTRICTED: "完全限制",
}


class TtsHandler:
    """TTS 编排入口。持有权限、密度、归档、云存储等子模块，编排 ai_speak 的完整流程。"""

    # 扁平键 → (组, 嵌套键) 映射
    _CFG_MAP = {
        "backup_cloud_enabled": ("backup_settings", "backup_cloud_enabled"),
        "backup_cloud_backend": ("backup_settings", "backup_cloud_backend"),
        "basic_voice_enabled": ("basic_settings", "basic_voice_enabled"),
        "text_min_length": ("text_processing", "text_min_length"),
        "text_segment_max_chars": ("text_processing", "text_segment_max_chars"),
        "backup_local_retention_days": ("backup_settings", "backup_local_retention_days"),
        "trigger_session_interval": ("trigger_probability", "trigger_session_interval"),
        "basic_tts_provider_id": ("basic_settings", "basic_tts_provider_id"),
        "basic_tts_fallback_id": ("basic_settings", "basic_tts_fallback_id"),
        "basic_tts_by_session": ("basic_settings", "basic_tts_by_session"),
        "text_segment_delay": ("text_processing", "text_segment_delay"),
        "text_retry_max_attempts": ("text_processing", "text_retry_max_attempts"),
        "text_merge_enabled": ("text_processing", "text_merge_enabled"),
        "text_merge_timeout": ("text_processing", "text_merge_timeout"),
        "text_merge_target_duration": ("text_processing", "text_merge_target_duration"),
        "send_form": ("sending_effects", "send_form"),
        "backup_chat_id": ("backup_settings", "backup_chat_id"),
    }

    def _cfg(self, key, default=None):
        """读取嵌套配置"""
        gk = self._CFG_MAP.get(key)
        if gk:
            return self.config.get(gk[0], {}).get(gk[1], default)
        return self.config.get(key, default)

    def __init__(self, context, config: dict, persist_callback=None):
        self.context = context
        self.config = config
        self._persist_callback = persist_callback

        # 运行时状态
        self._last_tts_time: dict[str, datetime] = {}
        self._temp_files: list[str] = []

        # 子模块
        self.perms = PermissionManager(self.config, persist_callback=self._persist_callback)
        self.density = DensityController(self.config)
        self.archive = LocalArchive(self.config)
        self._cloud_provider: Optional[CloudProvider] = None

        # WebUI 仪表盘用：最近调用记录环状缓冲区
        self._recent_calls: list[dict] = []

    # ── 终止清理 ──────────────────────────────────────────────

    def cleanup_temp_files(self):
        """清理临时音频文件。"""
        for f in self._temp_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass
        self._temp_files.clear()

    # ── 云存储 Provider 懒加载 ────────────────────────────────

    def _get_cloud_provider(self) -> Optional[CloudProvider]:
        if self._cloud_provider is not None:
            return self._cloud_provider

        if not self._cfg("backup_cloud_enabled", False):
            return None

        backend = self._cfg("backup_cloud_backend", "custom")
        providers = {
            "custom": CustomApiProvider,
            "s3": S3Provider,
            "webdav": WebDAVProvider,
        }
        cls = providers.get(backend)
        if not cls:
            logger.warning(f"[tts_cloud] 未知后端类型: {backend}")
            return None
        self._cloud_provider = cls(self.config.get("cloud_storage", {}))
        return self._cloud_provider

    # ── 核心入口 ──────────────────────────────────────────────

    async def speak(self, event: AstrMessageEvent, text: str) -> Optional[str]:
        """执行 TTS 完整流程。成功返回结果字符串，失败返回错误描述。"""
        session_id = str(event.session)
        user_id = event.get_sender_id()

        logger.info(
            f"[ai_speak] >>> 收到调用 session={session_id} user={user_id} "
            f"text_len={len(text) if text else 0}"
        )
        logger.debug(f"[ai_speak] 全文: {text!r}")

        # 0. 总开关
        if not self._cfg("basic_voice_enabled", True):
            logger.debug("[ai_speak] voice_enabled=false，跳过")
            return None

        # 1. 权限检查
        perm_level = self.perms.get_level(event)
        perm_label = PERM_LABEL_MAP.get(perm_level, f"未知({perm_level})")
        logger.debug(f"[ai_speak] 权限等级: {perm_label} (level={perm_level})")

        if perm_level == PERM_RESTRICTED:
            logger.debug("[ai_speak] 权限等级=完全限制，跳过")
            return None

        # 2. 文本长度校验
        min_len = self._cfg("text_min_length", 2)
        if not text or len(text.strip()) < min_len:
            logger.debug(f"[ai_speak] 文本太短 ({len(text) if text else 0} chars)，跳过")
            return None

        # 3. 速率限制
        if perm_level == PERM_BASIC and self._check_rate_limit(session_id):
            return None

        # 4. 密度检查（含记录，原子操作）
        if perm_level == PERM_BASIC:
            allowed, reason = await self.density.check_and_record(session_id, user_id)
            if not allowed:
                logger.debug(f"[ai_speak] 密度判定拒绝: {reason}")
                return None

        # 5. 获取 TTS Provider
        provider = self._get_tts_provider(event)
        if provider is None:
            logger.warning("[ai_speak] 未找到可用的 TTS Provider")
            return "语音合成失败：未找到可用的 TTS 服务，请检查 AstrBot 的 TTS 提供商配置。"

        # 6. 文本分段
        segment_max_chars = self._cfg("text_segment_max_chars", 80)
        segments = segment_text(text.strip(), segment_max_chars)
        logger.debug(f"[ai_speak] 文本分段: {len(segments)} 段 (max_chars={segment_max_chars})")
        for i, seg in enumerate(segments):
            logger.info(
                f"[ai_speak]   段{i+1}/{len(segments)}: len={len(seg)} "
                f"[{seg[:60]}{'...' if len(seg) > 60 else ''}]"
            )

        # 7. TTS 合成
        audio_paths = await synthesize_segments(provider, segments,
                                                      self._cfg("text_segment_delay", 0.3),
                                                      self._cfg("text_retry_max_attempts", 2))

        # 8. 合并音频
        final_audio = await self._merge_audio(audio_paths)

        # 9. 发送消息
        self._last_tts_time[session_id] = datetime.now()

        result_msg = await self._send_message(event, text, segments, audio_paths, final_audio)

        # 10. 备用会话发送
        await self._send_backup(text, final_audio, segments, audio_paths, event)

        # 11. 本地归档 + 云存储
        await self._archive_and_backup(segments, audio_paths, final_audio, text)

        # 后台清理
        retention = self._cfg("backup_local_retention_days", 7)
        cleaned = self.archive.cleanup_old(retention)
        if cleaned:
            logger.info(f"[tts_storage] 后台清理: 删除 {cleaned} 个过期文件")

        logger.info(
            f"[ai_speak] <<< 完成 session={session_id} user={user_id} "
            f"segments={len(segments)}"
        )
        return result_msg

    # ── 速率限制 ──────────────────────────────────────────────

    def _check_rate_limit(self, session_id: str) -> bool:
        rate_seconds = self._cfg("trigger_session_interval", 5)
        if rate_seconds <= 0:
            return False
        last_time = self._last_tts_time.get(session_id)
        if last_time is None:
            return False
        elapsed = (datetime.now() - last_time).total_seconds()
        if elapsed < rate_seconds:
            logger.info(
                f"[ai_speak] 会话 {session_id} 频率限制 "
                f"({elapsed:.1f}s < {rate_seconds}s)"
            )
            return True
        return False

    # ── TTS Provider 选取 ─────────────────────────────────────

    def _get_tts_provider(self, event: AstrMessageEvent) -> Optional[TTSProvider]:
        session_str = str(event.session)

        # 1. 会话级引擎选择（basic_tts_by_session）
        by_session = self._cfg("basic_tts_by_session", []) or []
        for entry in by_session:
            entry = entry.strip()
            if ":" in entry:
                sid, pid = entry.split(":", 1)
                if sid.strip() == session_str:
                    provider = self._resolve_provider(pid.strip())
                    if provider is not None:
                        logger.info(f"ai_speak: 使用会话指定引擎 [{pid.strip()}]")
                        return provider

        # 2. 首选 Provider
        provider = self._resolve_provider(self._cfg("basic_tts_provider_id", ""))
        if provider is not None:
            return provider
        # 3. 降级 Provider
        provider = self._resolve_provider(self._cfg("basic_tts_fallback_id", ""))
        if provider is not None:
            logger.info("ai_speak: 使用兜底 TTS Provider")
            return provider
        # 4. 系统默认
        return self.context.get_using_tts_provider(event.unified_msg_origin)

    def _resolve_provider(self, provider_id: str) -> Optional[TTSProvider]:
        if not provider_id:
            return None
        p = self.context.get_provider_by_id(provider_id)
        if p is None:
            logger.warning(f"ai_speak: Provider ID '{provider_id}' 未找到")
            return None
        if not isinstance(p, TTSProvider):
            logger.warning(
                f"ai_speak: Provider '{provider_id}' 不是 TTSProvider（{type(p).__name__}）"
            )
            return None
        return p


    async def _merge_audio(self, audio_paths: list[str]) -> Optional[str]:
        merge_enabled = self._cfg("text_merge_enabled", False)
        if merge_enabled and len(audio_paths) > 1:
            merge_timeout = self._cfg("text_merge_timeout", 30)
            target_duration = self._cfg("text_merge_target_duration", 30)
            logger.info(
                f"[ai_speak] 开始合并 {len(audio_paths)} 段音频 "
                f"(timeout={merge_timeout}s, target={target_duration}s)"
            )
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(merge_audio_files, audio_paths, target_duration),
                    timeout=merge_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[ai_speak] 音频合并超时 ({merge_timeout}s)，将分段发送"
                )
                return None
        elif len(audio_paths) == 1:
            return audio_paths[0]
        return None

    # ── 发送消息 ──────────────────────────────────────────────

    def _build_chain(self, text_part: str, audio_path: str) -> MessageChain:
        """根据 send_text_with_voice 配置构造消息链。"""
        send_text = self._cfg("send_form", "voice_only") == "text_and_voice"
        record = Record.fromFileSystem(audio_path)
        if send_text:
            return MessageChain([Plain(text_part), record])
        return MessageChain([record])

    async def _send_message(
        self,
        event: AstrMessageEvent,
        text: str,
        segments: list[str],
        audio_paths: list[str],
        final_audio: Optional[str],
    ) -> str:
        session_id = str(event.session)

        if final_audio is None and len(audio_paths) > 1:
            logger.info(
                f"[ai_speak] 分段发送 {len(audio_paths)} 条语音 session={session_id}"
            )
            for i, (seg, ap) in enumerate(zip(segments, audio_paths)):
                label = f"[{i+1}/{len(segments)}] {seg}"
                await event.send(self._build_chain(label, ap))
                logger.info(
                    f"[ai_speak] 已发送 段{i+1}/{len(segments)} session={session_id}"
                )
            return f"语音消息已分段发送（共 {len(segments)} 段）"
        else:
            display_text = text if len(text) <= 200 else text[:200] + "..."
            logger.info(
                f"[ai_speak] 发送消息: text_len={len(text)} "
                f"audio={'merged' if len(audio_paths) > 1 else 'single'} "
                f"session={session_id}"
            )
            await event.send(self._build_chain(display_text, final_audio))
            logger.debug(f"[ai_speak] 已发送 session={session_id}")
            if len(segments) > 1:
                return f"语音消息已发送成功（{len(segments)} 段已合并）"
            return "语音消息已发送成功"

    # ── 备用会话发送 ──────────────────────────────────────────

    async def _send_backup(
        self,
        text: str,
        final_audio: Optional[str],
        segments: list[str],
        audio_paths: list[str],
        event: AstrMessageEvent,
    ):
        """将语音消息转发到备份群。支持分段逐条发送 + 汇总。"""
        backup = (self._cfg("backup_chat_id") or "").strip()

        # 未配置则默认发给 bot 自己（私聊）
        if not backup or not backup.isdigit():
            self_id = event.get_self_id()
            if not self_id or not self_id.isdigit():
                logger.debug("[ai_speak] 备份跳过：无有效备份目标或 bot 自身 ID")
                return
            backup = self_id
            # 自备份用私聊消息类型，避免把个人号当群号发送
            session = MessageSession(
                event.session.platform_id,
                MessageType.FRIEND_MESSAGE,
                backup,
            )
        else:
            session = MessageSession(
                event.session.platform_id,
                event.session.message_type,
                backup,
            )

        logger.debug(f"[ai_speak] 备份发送到 QQ 群: {session}")
        try:
            # ── 分段发送（多段且未合并）───────────────────────────
            if not final_audio and audio_paths and len(audio_paths) > 1:
                for i, (seg, ap) in enumerate(zip(segments, audio_paths)):
                    if not os.path.exists(ap):
                        logger.warning(
                            f"[ai_speak] 备份: 音频文件不存在 {ap}，跳过段{i+1}"
                        )
                        continue
                    await self._backup_send_triple(
                        session,
                        f"语音备份 段{i+1}/{len(segments)}",
                        seg,
                        ap,
                    )

                # 汇总
                total_size = sum(
                    os.path.getsize(ap) for ap in audio_paths
                    if os.path.exists(ap)
                )
                summary = (
                    f"语音备份汇总\n"
                    f"总段数: {len(segments)}\n"
                    f"总大小: {format_bytes(total_size)}\n"
                    f"全文: {len(text)} 字"
                )
                await self.context.send_message(session, MessageChain([Plain(summary)]))

            # ── 单段或合并后发送 ────────────────────────────────
            else:
                audio = final_audio or (audio_paths[0] if audio_paths else None)
                if not audio or not os.path.exists(audio):
                    return
                await self._backup_send_triple(
                    session, "语音备份", text, audio,
                )

            logger.debug(f"[ai_speak] 备份发送完成: {session}")
        except Exception as e:
            logger.warning(f"[ai_speak] 备份发送失败 ({session}): {e}")

    async def _backup_send_triple(
        self,
        session: MessageSession,
        title: str,
        text_content: str,
        audio_path: str,
    ):
        """向会话发送三个独立消息：文字信息 → 语音 → 原始文件。"""
        display = text_content[:200] + "..." if len(text_content) > 200 else text_content
        size_str = format_file_size(audio_path)
        info = (
            f"{title}\n"
            f"内容: {display}\n"
            f"文件: {os.path.basename(audio_path)}\n"
            f"大小: {size_str}"
        )
        await self.context.send_message(session, MessageChain([Plain(info)]))
        await self.context.send_message(session, MessageChain([Record.fromFileSystem(audio_path)]))
        await self.context.send_message(
            session,
            MessageChain([File(name=os.path.basename(audio_path), file=audio_path)]),
        )

    # ── 归档 + 云备份 ─────────────────────────────────────────

    async def _archive_and_backup(
        self,
        segments: list[str],
        audio_paths: list[str],
        final_audio: Optional[str],
        text: str,
    ):
        archivable = (
            final_audio if final_audio else (audio_paths[0] if audio_paths else None)
        )
        if archivable:
            try:
                archived = self.archive.save_file(archivable, text)
                if archived:
                    await self._cloud_backup(archived, text)
            except Exception as e:
                logger.warning(f"[ai_speak] 归档/上传失败: {e}")
        elif len(audio_paths) > 1:
            for ap in audio_paths[1:]:
                try:
                    self.archive.save_file(ap, text)
                except Exception as e:
                    logger.warning(f"[ai_speak] 归档失败: {e}")

    async def _cloud_backup(self, file_path: str, text: str):
        """异步执行云存储上传。"""
        for provider in (self._get_cloud_provider() for _ in [1] if self._cfg("backup_cloud_enabled", False)):
            if provider:
                try:
                    await provider.upload(file_path, text)
                except Exception as e:
                    logger.warning(f"[tts_cloud] 上传异常: {e}")
