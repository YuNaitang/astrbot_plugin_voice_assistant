"""
AI Voice Assistant — TTS 编排核心
===================================
负责 ai_speak 工具的完整流程：权限检查 → 文本校验 → 速率限制 → 密度检查
→ Provider 选取 → 分段 → TTS 合成 → 合并 → 发送 → 备份 → 归档。
"""
import asyncio
import os
import random
import re
import tempfile
from datetime import datetime
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import File, Plain, Record
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.message_session import MessageSession as MessageSesion
from astrbot.core.provider.provider import TTSProvider

from ..errors import (
    TTSProviderError,
    VoiceAssistantError,
)
from ..storage.base import CloudProvider
from ..storage.custom_api import CustomApiProvider
from ..storage.local import LocalArchive
from ..storage.s3 import S3Provider
from ..storage.webdav import WebDAVProvider

from .density import DensityController
from .permissions import (
    PERM_BASIC,
    PERM_LABELS,
    PERM_RESTRICTED,
    PERM_UNLIMITED,
    PermissionManager,
)


class TtsHandler:
    """TTS 编排入口。持有权限、密度、归档、云存储等子模块，编排 ai_speak 的完整流程。"""

    def __init__(self, context, config: dict):
        self.context = context
        self.config = config

        # 运行时状态
        self._last_tts_time: dict[str, datetime] = {}
        self._temp_files: list[str] = []

        # 子模块
        self.perms = PermissionManager(self.config)
        self.density = DensityController(self.config)
        self.archive = LocalArchive(self.config)
        self._cloud_provider: Optional[CloudProvider] = None

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

        if not self.config.get("cloud_backup_enabled", False):
            return None

        backend = self.config.get("cloud_backend", "custom")
        providers = {
            "custom": CustomApiProvider,
            "s3": S3Provider,
            "webdav": WebDAVProvider,
        }
        cls = providers.get(backend)
        if not cls:
            logger.warning(f"[tts_cloud] 未知后端类型: {backend}")
            return None
        self._cloud_provider = cls(self.config)
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
        logger.info(f"[ai_speak] 全文: {text!r}")

        # 0. 总开关
        if not self.config.get("voice_enabled", True):
            logger.info("[ai_speak] voice_enabled=false，跳过")
            return None

        # 1. 权限检查
        perm_level = self.perms.get_level(event)
        perm_label = {
            PERM_UNLIMITED: "无限制",
            PERM_BASIC: "基准限制",
            PERM_RESTRICTED: "完全限制",
        }.get(perm_level, f"未知({perm_level})")
        logger.info(f"[ai_speak] 权限等级: {perm_label} (level={perm_level})")

        if perm_level == PERM_RESTRICTED:
            logger.info("[ai_speak] 权限等级=完全限制，跳过")
            return None

        # 2. 文本长度校验
        min_len = self.config.get("min_text_length", 2)
        if not text or len(text.strip()) < min_len:
            logger.info(f"[ai_speak] 文本太短 ({len(text) if text else 0} chars)，跳过")
            return None

        # 3. 速率限制
        if perm_level == PERM_BASIC and self._check_rate_limit(session_id):
            return None

        # 4. 密度检查
        if perm_level == PERM_BASIC:
            allowed, reason = self.density.should_allow(session_id, user_id)
            if not allowed:
                logger.info(f"[ai_speak] 密度判定拒绝: {reason}")
                return None

        # 5. 获取 TTS Provider
        provider = self._get_tts_provider(event)
        if provider is None:
            logger.warning("[ai_speak] 未找到可用的 TTS Provider")
            return "语音合成失败：未找到可用的 TTS 服务，请检查 AstrBot 的 TTS 提供商配置。"

        # 6. 文本分段
        segment_max_chars = self.config.get("tts_segment_max_chars", 80)
        segments = self._segment_text(text.strip(), segment_max_chars)
        logger.info(f"[ai_speak] 文本分段: {len(segments)} 段 (max_chars={segment_max_chars})")
        for i, seg in enumerate(segments):
            logger.info(
                f"[ai_speak]   段{i+1}/{len(segments)}: len={len(seg)} "
                f"[{seg[:60]}{'...' if len(seg) > 60 else ''}]"
            )

        # 7. TTS 合成
        audio_paths = await self._synthesize_segments(provider, segments)

        # 8. 合并音频
        final_audio = await self._merge_audio(audio_paths)

        # 9. 发送消息
        self._last_tts_time[session_id] = datetime.now()
        self.density.record_sent(session_id, user_id)

        result_msg = await self._send_message(event, text, segments, audio_paths, final_audio)

        # 10. 备用会话发送
        await self._send_backup(text, final_audio, segments, audio_paths, event)

        # 11. 本地归档 + 云存储
        await self._archive_and_backup(segments, audio_paths, final_audio, text)

        # 后台清理
        retention = self.config.get("local_audio_retention_days", 7)
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
        rate_seconds = self.config.get("rate_limit_seconds", 5)
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
        provider = self._resolve_provider(self.config.get("tts_provider_id", ""))
        if provider is not None:
            return provider
        provider = self._resolve_provider(self.config.get("tts_fallback_provider_id", ""))
        if provider is not None:
            logger.info("ai_speak: 使用兜底 TTS Provider")
            return provider
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

    # ── 分段 / 合并 / 合成 ────────────────────────────────────

    @staticmethod
    def _segment_text(text: str, max_chars: int = 80) -> list[str]:
        """按「换行 → 句号 → 逗号 → 强制切分」优先级将长文本分段。"""
        if len(text) <= max_chars:
            return [text]

        blocks = re.split(r'\n+', text)
        segments = []
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            if len(block) <= max_chars:
                segments.append(block)
            else:
                sub = re.split(r'(?<=[。？！])', block)
                for sub_seg in sub:
                    sub_seg = sub_seg.strip()
                    if not sub_seg:
                        continue
                    if len(sub_seg) <= max_chars:
                        segments.append(sub_seg)
                    else:
                        sub2 = re.split(r'(?<=[，；：])', sub_seg)
                        for s in sub2:
                            s = s.strip()
                            if not s:
                                continue
                            if len(s) <= max_chars:
                                segments.append(s)
                            else:
                                while len(s) > max_chars:
                                    segments.append(s[:max_chars])
                                    s = s[max_chars:]
                                if s:
                                    segments.append(s)
        return segments

    @staticmethod
    def _merge_audio_files(audio_paths: list[str]) -> str:
        """使用 pydub 将多个 WAV 文件合并为一条。若 pydub 不可用则抛出 ImportError。"""
        from pydub import AudioSegment
        combined = AudioSegment.empty()
        for ap in audio_paths:
            seg = AudioSegment.from_file(ap)
            combined += seg
        merged_dir = tempfile.gettempdir()
        merged_path = os.path.join(
            merged_dir, f"tts_merged_{random.randint(100000, 999999)}.wav"
        )
        combined.export(merged_path, format="wav")
        return merged_path

    async def _synthesize_segments(
        self, provider: TTSProvider, segments: list[str]
    ) -> list[str]:
        delay = self.config.get("tts_inter_segment_delay", 0.3)
        max_attempts = self.config.get("tts_retry_max_attempts", 2)

        audio_paths = []
        for i, seg in enumerate(segments):
            # 段间间隔：避免 TTS API 限流
            if i > 0 and delay > 0:
                await asyncio.sleep(delay)

            for attempt in range(1 + max_attempts):
                try:
                    logger.info(
                        f"[ai_speak] TTS合成 段{i+1}/{len(segments)}: "
                        f"text={seg!r}{f' (retry {attempt})' if attempt else ''}"
                    )
                    audio_path = await provider.get_audio(seg)
                    logger.info(f"[ai_speak] TTS合成完成 段{i+1}: path={audio_path}")
                    audio_paths.append(audio_path)
                    self._temp_files.append(audio_path)
                    break  # 成功，跳出重试循环
                except Exception as e:
                    provider_id = "?"
                    try:
                        provider_id = provider.meta().id
                    except Exception:
                        pass

                    if attempt < max_attempts:
                        wait = 2 ** attempt  # 指数退避: 1s, 2s, 4s...
                        logger.warning(
                            f"[ai_speak] TTS合成 段{i+1} 失败，{wait}s 后重试 "
                            f"({attempt+1}/{max_attempts}) "
                            f"(provider={provider_id}): {e}"
                        )
                        await asyncio.sleep(wait)
                    else:
                        logger.error(
                            f"[ai_speak] TTS合成失败 段{i+1} "
                            f"(provider={provider_id}): {e}"
                        )
                        raise TTSProviderError(
                            f"语音合成失败（{provider_id}）：{e!s}"
                        ) from e
        return audio_paths

    async def _merge_audio(self, audio_paths: list[str]) -> Optional[str]:
        merge_enabled = self.config.get("tts_merge_enabled", False)
        if merge_enabled and len(audio_paths) > 1:
            merge_timeout = self.config.get("tts_merge_timeout_seconds", 30)
            logger.info(
                f"[ai_speak] 开始合并 {len(audio_paths)} 段音频 (timeout={merge_timeout}s)"
            )
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._merge_audio_files, audio_paths),
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
        send_text = self.config.get("send_text_with_voice", False)
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
            logger.info(f"[ai_speak] 已发送 session={session_id}")
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
        """备用会话发送：将语音消息转发到指定会话（或默认发给 bot 自己）。

        每条消息包含三要素：
          1. 文件信息（文本描述 + 文件大小等元数据）
          2. 语音消息（Record 组件）
          3. 原始 WAV 文件（File 组件）
        """
        backup = (self.config.get("backup_session_id") or "").strip()

        is_private = True  # 默认私聊发送
        if ":friend" in backup:
            backup = backup.replace(":friend", "").strip()
            is_private = True
        elif ":group" in backup:
            backup = backup.replace(":group", "").strip()

        # 未配置则默认发给 bot 自己
        if not backup or not backup.isdigit():
            self_id = event.get_self_id()
            if not self_id:
                logger.info("[ai_speak] 未配置备份会话且无法获取 bot 自身 ID，跳过")
                return
            backup = self_id
            is_private = True
            logger.info(f"[ai_speak] 备份发送到 bot 自身: {backup}")

        session = MessageSesion(
            event.session.platform_id,
            MessageType.FRIEND_MESSAGE if is_private else MessageType.GROUP_MESSAGE,
            backup,
        )

        logger.info(f"[ai_speak] 备份发送到 QQ: {session}")
        try:
            # 优先用 final_audio（单段或合并后），不存在时取第一段
            audio_to_backup = final_audio or (audio_paths[0] if audio_paths else None)
            if not audio_to_backup:
                return

            if not os.path.exists(audio_to_backup):
                logger.warning(f"[ai_speak] 备份发送: 音频文件不存在 {audio_to_backup}，跳过")
                return

            display_text = text if len(text) <= 200 else text[:200] + "..."
            size_str = self._format_file_size(audio_to_backup)
            info = (
                f"📁 语音备份"
                f"{f' 段{i+1}/{len(segments)}' if not final_audio and audio_paths else ''}\n"
                f"内容: {display_text}\n"
                f"文件: {os.path.basename(audio_to_backup)}\n"
                f"大小: {size_str}"
            )
            await self.context.send_message(
                session,
                MessageChain([
                    Plain(info),
                    Record.fromFileSystem(audio_to_backup),
                    File(name=os.path.basename(audio_to_backup), file=audio_to_backup),
                ]),
            )
            logger.info(f"[ai_speak] 备份发送完成: {session}")
        except Exception as e:
            logger.warning(f"[ai_speak] 备份发送失败 ({session}): {e}")

    @staticmethod
    def _format_file_size(file_path: str) -> str:
        """格式化文件大小，返回可读字符串。"""
        try:
            size = os.path.getsize(file_path)
            if size < 1024:
                return f"{size} B"
            elif size < 1024 * 1024:
                return f"{size / 1024:.1f} KB"
            else:
                return f"{size / (1024 * 1024):.1f} MB"
        except OSError:
            return "未知"

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
                archived = self.archive.save_file(archivable)
                if archived:
                    await self._cloud_backup(archived, text)
            except Exception as e:
                logger.warning(f"[ai_speak] 归档/上传失败: {e}")
        elif len(audio_paths) > 1:
            for ap in audio_paths[1:]:
                try:
                    self.archive.save_file(ap)
                except Exception as e:
                    logger.warning(f"[ai_speak] 归档失败: {e}")

    async def _cloud_backup(self, file_path: str, text: str):
        """异步执行云存储上传。"""
        provider = self._get_cloud_provider()
        if not provider:
            return

        try:
            await provider.upload(file_path, text)
        except Exception as e:
            logger.warning(f"[tts_cloud] 上传异常: {e}")
