"""
AI Voice Assistant — AstrBot 通用 TTS 编排插件

允许AI 通过工具自主调用 TTS 回复语音。
支持多 Provider 降级、三级权限管理、双层密度控制、长文本分段合并。
"""
import asyncio
import os
import random
import re
import tempfile
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

from .permissions import PermissionManager, PERM_UNLIMITED, PERM_BASIC, PERM_RESTRICTED, PERM_LABELS
from .storage import AudioStorage


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

        # 子模块
        self.perms = PermissionManager(self.config)
        self.storage = AudioStorage(self.config)

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
    # LLM 请求注入（密度提醒 + extra prompt）
    # ------------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """每次 LLM 请求前，注入语音相关系统提示"""
        extra = self.config.get("voice_prompt_extra", "")
        if extra:
            req.system_prompt += f"\n\n[语音行为规则]\n{extra}"

        session_id = str(event.session)
        if self._is_over_density_limit(session_id):
            if session_id not in self._density_warned:
                req.system_prompt += (
                    "\n\n[注意] 你最近已经发送了很多语音消息。"
                    "在收到重置通知之前，请不要再使用 ai_speak 工具。"
                )
                self._density_warned.add(session_id)

    # ------------------------------------------------
    # 密度控制
    # ------------------------------------------------

    @staticmethod
    def _prune_timeline(timestamps: list[datetime], window_minutes: int) -> list[datetime]:
        now = datetime.now()
        cutoff = now - timedelta(minutes=window_minutes)
        return [t for t in timestamps if t > cutoff]

    def _is_over_density_limit(self, session_id: str) -> bool:
        window = self.config.get("density_window_minutes", 10)
        max_count = self.config.get("density_max_count", 3)
        timeline = self._voice_timeline.get(session_id, [])
        timeline = self._prune_timeline(timeline, window)
        self._voice_timeline[session_id] = timeline
        return len(timeline) >= max_count

    def _get_user_probability(self, session_id: str, user_id: str) -> float:
        window = self.config.get("user_density_window_minutes", 60)
        threshold = self.config.get("user_density_threshold", 5)
        steepness = self.config.get("user_density_curve_steepness", 0.7)
        if steepness <= 0:
            return 1.0
        user_map = self._user_trigger_timeline.get(session_id, {})
        timeline = self._prune_timeline(user_map.get(user_id, []), window)
        user_map[user_id] = timeline
        self._user_trigger_timeline[session_id] = user_map
        count = len(timeline)
        return 1.0 / (1.0 + exp(steepness * (count - threshold)))

    def _should_allow_voice(self, session_id: str, user_id: str) -> tuple:
        """综合决策：先会话硬阻断，再用户概率降权。
        Returns: (是否允许: bool, 原因描述: str)
        """
        if self._is_over_density_limit(session_id):
            reason = f"会话语音密度超限，请稍后再试"
            logger.info(f"[密度结果] 拒绝 — {reason}")
            return False, reason

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

        # 0. 总开关
        if not self.config.get("voice_enabled", True):
            logger.info("[ai_speak] voice_enabled=false，跳过")
            return

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
            return

        # 2. 文本长度校验
        min_len = self.config.get("min_text_length", 2)
        if not text or len(text.strip()) < min_len:
            logger.info(f"[ai_speak] 文本太短 ({len(text) if text else 0} chars)，跳过")
            return

        # 3. 速率限制
        if perm_level == PERM_BASIC and self._check_rate_limit(session_id):
            return

        # 4. 密度检查
        if perm_level == PERM_BASIC:
            allowed, reason = self._should_allow_voice(session_id, user_id)
            if not allowed:
                logger.info(f"[ai_speak] 密度判定拒绝: {reason}")
                return

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
            logger.info(f"[ai_speak]   段{i+1}/{len(segments)}: len={len(seg)} [{seg[:60]}{'...' if len(seg) > 60 else ''}]")

        # 7. TTS 合成
        audio_paths = []
        for i, seg in enumerate(segments):
            try:
                logger.info(f"[ai_speak] TTS合成 段{i+1}/{len(segments)}: text={seg!r}")
                audio_path = await provider.get_audio(seg)
                logger.info(f"[ai_speak] TTS合成完成 段{i+1}: path={audio_path}")
                audio_paths.append(audio_path)
                self._temp_files.append(audio_path)
            except Exception as e:
                provider_id = "?"
                try:
                    provider_id = provider.meta().id
                except Exception:
                    pass
                logger.error(f"[ai_speak] TTS合成失败 段{i+1} (provider={provider_id}): {e}")
                return f"语音合成失败（{provider_id}）：{e!s}"

        # 8. 合并音频
        merge_enabled = self.config.get("tts_merge_enabled", False)
        if merge_enabled and len(audio_paths) > 1:
            merge_timeout = self.config.get("tts_merge_timeout_seconds", 30)
            logger.info(f"[ai_speak] 开始合并 {len(audio_paths)} 段音频 (timeout={merge_timeout}s)")
            try:
                final_audio = await asyncio.wait_for(
                    asyncio.to_thread(self._merge_audio_files, audio_paths),
                    timeout=merge_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[ai_speak] 音频合并超时 ({merge_timeout}s)，将分段发送")
                final_audio = None
        elif len(audio_paths) == 1:
            final_audio = audio_paths[0]
        else:
            final_audio = None

        # 9. 发送消息
        self._last_tts_time[session_id] = datetime.now()
        self._record_voice_sent(session_id, user_id)

        if final_audio is None and len(audio_paths) > 1:
            logger.info(f"[ai_speak] 分段发送 {len(audio_paths)} 条语音 session={session_id}")
            for i, (seg, ap) in enumerate(zip(segments, audio_paths)):
                await event.send(MessageChain([
                    Plain(f"[{i+1}/{len(segments)}] {seg}"),
                    Record.fromFileSystem(ap),
                ]))
                logger.info(f"[ai_speak] 已发送 段{i+1}/{len(segments)} session={session_id}")
            result_msg = f"语音消息已分段发送（共 {len(segments)} 段）"
        else:
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

        # 10. 备用会话发送
        await self._send_backup(text, final_audio, segments, audio_paths, event)

        # 11. 本地归档 + 云存储
        archivable = final_audio if final_audio else (audio_paths[0] if audio_paths else None)
        if archivable:
            archived = self.storage.save_file(archivable)
            if archived:
                self.storage.cloud_backup(archived, text)
        elif len(audio_paths) > 1:
            for ap in audio_paths[1:]:
                self.storage.save_file(ap)

        retention = self.config.get("local_audio_retention_days", 7)
        cleaned = self.storage.cleanup_old(retention)
        if cleaned:
            logger.info(f"[tts_storage] 后台清理: 删除 {cleaned} 个过期文件")

        logger.info(
            f"[ai_speak] <<< 完成 session={session_id} user={user_id} "
            f"segments={len(segments)}"
        )
        return result_msg

    # ------------------------------------------------
    # 备用会话发送
    # ------------------------------------------------

    async def _send_backup(self, text: str, final_audio: str, segments: list,
                           audio_paths: list, event: AstrMessageEvent):
        backup = (self.config.get("backup_session_id") or "").strip()
        if not backup:
            return

        is_private = False
        if ":friend" in backup:
            backup = backup.replace(":friend", "").strip()
            is_private = True
        elif ":group" in backup:
            backup = backup.replace(":group", "").strip()

        if not backup or not backup.isdigit():
            logger.warning(f"[ai_speak] 备份发送: 无效的 QQ 号 '{backup}'，跳过")
            return

        session = MessageSesion(
            event.session.platform_id,
            MessageType.FRIEND_MESSAGE if is_private else MessageType.GROUP_MESSAGE,
            backup,
        )

        logger.info(f"[ai_speak] 备份发送到 QQ: {session}")
        try:
            if final_audio and len(audio_paths) <= 1:
                display_text = text if len(text) <= 200 else text[:200] + "..."
                await self.context.send_by_session(
                    session, MessageChain([Plain(display_text), Record.fromFileSystem(final_audio)])
                )
            elif audio_paths:
                for i, (seg, ap) in enumerate(zip(segments, audio_paths)):
                    await self.context.send_by_session(
                        session, MessageChain([Plain(f"[{i+1}/{len(segments)}] {seg}"), Record.fromFileSystem(ap)])
                    )
            logger.info(f"[ai_speak] 备份发送完成: {session}")
        except Exception as e:
            logger.warning(f"[ai_speak] 备份发送失败 ({session}): {e}")

    # ------------------------------------------------
    # Provider 选取
    # ------------------------------------------------

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
            logger.warning(f"ai_speak: Provider '{provider_id}' 不是 TTSProvider（{type(p).__name__}）")
            return None
        return p

    # ------------------------------------------------
    # 速率限制
    # ------------------------------------------------

    def _check_rate_limit(self, session_id: str) -> bool:
        rate_seconds = self.config.get("rate_limit_seconds", 5)
        if rate_seconds <= 0:
            return False
        last_time = self._last_tts_time.get(session_id)
        if last_time is None:
            return False
        elapsed = (datetime.now() - last_time).total_seconds()
        if elapsed < rate_seconds:
            logger.info(f"[ai_speak] 会话 {session_id} 频率限制 ({elapsed:.1f}s < {rate_seconds}s)")
            return True
        return False

    # ------------------------------------------------
    # 文本分段
    # ------------------------------------------------

    @staticmethod
    def _segment_text(text: str, max_chars: int = 80) -> list[str]:
        """按「换行 → 句号 → 逗号 → 强制切分」优先级将长文本分段。"""
        if len(text) <= max_chars:
            return [text]

        # 第一轮：按换行符切分
        blocks = re.split(r'\n+', text)
        segments = []
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            if len(block) <= max_chars:
                segments.append(block)
            else:
                # 第二轮：按句号/问号/感叹号切分
                sub = re.split(r'(?<=[。？！])', block)
                for sub_seg in sub:
                    sub_seg = sub_seg.strip()
                    if not sub_seg:
                        continue
                    if len(sub_seg) <= max_chars:
                        segments.append(sub_seg)
                    else:
                        # 第三轮：按逗号/冒号/分号切分
                        sub2 = re.split(r'(?<=[，；：])', sub_seg)
                        for s in sub2:
                            s = s.strip()
                            if not s:
                                continue
                            if len(s) <= max_chars:
                                segments.append(s)
                            else:
                                # 强制切分
                                while len(s) > max_chars:
                                    segments.append(s[:max_chars])
                                    s = s[max_chars:]
                                if s:
                                    segments.append(s)
        return segments

    # ------------------------------------------------
    # 音频合并
    # ------------------------------------------------

    @staticmethod
    def _merge_audio_files(audio_paths: list[str]) -> str:
        """使用 pydub 将多个 WAV 文件合并为一条。若 pydub 不可用则抛出 ImportError。"""
        from pydub import AudioSegment
        combined = AudioSegment.empty()
        for ap in audio_paths:
            seg = AudioSegment.from_file(ap)
            combined += seg
        merged_dir = tempfile.gettempdir()
        merged_path = os.path.join(merged_dir, f"tts_merged_{random.randint(100000, 999999)}.wav")
        combined.export(merged_path, format="wav")
        return merged_path

    # ------------------------------------------------
    # 指令 — /voice_perm
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
        """
        if not event.is_admin():
            await event.send(MessageChain([Plain("❌ 权限不足：仅管理员可管理语音权限")]))
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

        if action == "help":
            await event.send(MessageChain([Plain(
                "📋 语音权限管理\n\n"
                "/voice_perm set <session_id> <0|1|2>\n"
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

        if action == "list":
            entries = self.config.get("session_permissions", []) or []
            if not entries:
                default_label = PERM_LABELS.get(self.config.get("default_permission_level", PERM_BASIC), "?")
                await event.send(MessageChain([Plain(
                    f"📋 暂无自定义权限配置\n全部会话使用默认等级: {default_label}"
                )]))
            else:
                lines = ["📋 自定义权限列表:"]
                for entry in sorted(entries):
                    entry = entry.strip()
                    if ':' in entry:
                        sid, lvl_str = entry.rsplit(":", 1)
                        try:
                            lvl = int(lvl_str)
                            label = PERM_LABELS.get(lvl, f"未知({lvl})")
                        except ValueError:
                            label = f"无效({lvl_str})"
                        lines.append(f"  {sid} → {label}")
                default_label = PERM_LABELS.get(self.config.get("default_permission_level", PERM_BASIC), "?")
                lines.append(f"\n默认等级: {default_label}")
                await event.send(MessageChain([Plain("\n".join(lines))]))
            return

        if action == "get":
            target_sid = parts[2] if len(parts) >= 3 else str(event.session)
            level = self.perms.cache.get(target_sid)
            if level is None:
                level = self.config.get("default_permission_level", PERM_BASIC)
                source = "默认"
            else:
                source = "自定义"
            label = PERM_LABELS.get(level, f"未知({level})")
            await event.send(MessageChain([Plain(f"📋 会话: {target_sid}\n等级: {label} (level={level})\n来源: {source}")]))
            return

        if action == "set":
            if len(parts) < 4:
                await event.send(MessageChain([Plain("❌ 用法: /voice_perm set <session_id> <0|1|2>")]))
                return
            target_sid = parts[2]
            try:
                level = int(parts[3])
                if level not in (PERM_UNLIMITED, PERM_BASIC, PERM_RESTRICTED):
                    raise ValueError
            except ValueError:
                await event.send(MessageChain([Plain("❌ 等级必须为 0/1/2")]))
                return
            self.perms.set_level(target_sid, level)
            label = PERM_LABELS[level]
            await event.send(MessageChain([Plain(f"✅ 已设置: {target_sid} → {label} (level={level})")]))
            logger.info(f"[voice_perm] 管理员设置权限: {target_sid} → {label}")
            return

        if action == "del":
            if len(parts) < 3:
                await event.send(MessageChain([Plain("❌ 用法: /voice_perm del <session_id>")]))
                return
            target_sid = parts[2]
            self.perms.remove_level(target_sid)
            default_label = PERM_LABELS.get(self.config.get("default_permission_level", PERM_BASIC), "?")
            await event.send(MessageChain([Plain(f"✅ 已删除自定义权限: {target_sid}\n已恢复默认等级: {default_label}")]))
            logger.info(f"[voice_perm] 管理员删除权限: {target_sid}")
            return

        await event.send(MessageChain([Plain(f"❌ 未知操作: {action}\n用法: /voice_perm set|get|list|del|help")]))
