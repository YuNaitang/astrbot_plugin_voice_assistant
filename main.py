"""
AI Voice Assistant — AstrBot 通用 TTS 编排插件

AI 通过 LLM 工具 ai_speak() 主动发起语音合成。
不绑定特定 TTS 供应商，用户可在管理面板选择首选/兜底 Provider。
"""
import os
from datetime import datetime
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star
from astrbot.api import logger
from astrbot.core.message.components import Plain, Record
from astrbot.core.provider.provider import TTSProvider


class Main(Star):
    """AI Voice Assistant — 让 AI 主动调用 TTS 回复语音"""

    def __init__(self, context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # ---------- 运行时状态 ----------
        self._last_tts_time: dict[str, datetime] = {}
        self._temp_files: list[str] = []
        self._providers_logged: bool = False

        # 尝试在启动时枚举——此时代理商可能尚未初始化
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

    # ----------------------------------------------------------------
    # Provider 发现
    # ----------------------------------------------------------------

    def _log_available_tts_providers(self, force: bool = False):
        """打印所有已注册 TTS Provider（幂等，仅第一次成功时输出）"""
        if self._providers_logged and not force:
            return

        try:
            providers = self.context.get_all_tts_providers()
        except Exception as e:
            logger.debug(f"获取 TTS Provider 列表失败（可能尚未初始化）: {e}")
            return

        if not providers:
            return  # 静默等待下次尝试

        logger.info(f"AI Voice Assistant: 发现 {len(providers)} 个 TTS Provider:")
        for p in providers:
            try:
                meta = p.meta()
                logger.info(f"  · id={meta.id}  type={meta.type}  model={meta.model or 'N/A'}")
            except Exception:
                logger.info(f"  · (无法获取元数据的 Provider: {type(p).__name__})")

        self._providers_logged = True

    # ----------------------------------------------------------------
    # LLM 工具 — ai_speak
    # ----------------------------------------------------------------

    @filter.llm_tool(name="ai_speak")
    async def ai_speak(self, event: AstrMessageEvent, text: str):
        """用语音回复用户。当你认为回复内容适合用语音表达、或用户期望听到语音时调用。
        调用后系统会自动合成语音并同时发送文字和语音消息。不要调用得太频繁。只有在需要时调用。

        Args:
            text(string): 想说出的文本（中文，自然流畅的口语表达）
        """
        # 确保 Provider 列表已打印
        self._log_available_tts_providers()

        # ---- 0. 总开关 ----
        if not self.config.get("voice_enabled", True):
            _log_debug(self.config, "ai_speak: voice_enabled=false，跳过")
            return

        # ---- 1. 权限检查 ----
        if self._check_permission(event):
            return  # 权限拒绝，静默

        # ---- 2. 文本长度校验 ----
        min_len = self.config.get("min_text_length", 2)
        max_len = self.config.get("max_text_length", 500)

        if not text or len(text.strip()) < min_len:
            _log_debug(self.config, f"ai_speak: 文本太短 ({len(text) if text else 0} chars)，跳过")
            return

        if len(text) > max_len:
            # 超长则截断（语音是文字回复的补充，截断损失小于拒绝）
            original_len = len(text)
            text = text[:max_len]
            _log_debug(self.config,
                       f"ai_speak: 文本过长 ({original_len})，已截断至 {max_len} 字符")

        # ---- 3. 速率限制 ----
        session_id = str(event.session)
        if self._check_rate_limit(session_id):
            return  # 频率过高，静默

        # ---- 4. 获取 TTS Provider ----
        provider = self._get_tts_provider(event)
        if provider is None:
            logger.warning("ai_speak: 未找到可用的 TTS Provider")
            return

        # ---- 5. TTS 合成 ----
        try:
            audio_path = await provider.get_audio(text)
        except Exception as e:
            provider_id = "?"
            try:
                provider_id = provider.meta().id
            except Exception:
                pass
            logger.error(f"ai_speak: TTS 合成失败 (provider={provider_id}): {e}")
            return

        _log_debug(self.config, f"ai_speak: 已合成 [{text[:50].replace(chr(10), ' ')}...] → {audio_path}")
        self._temp_files.append(audio_path)
        self._last_tts_time[session_id] = datetime.now()

        # ---- 6. 双输出：文字 + 语音（仅此一条 yield） ----
        yield event.chain_result([
            Plain(text),
            Record.fromFileSystem(audio_path),
        ])

    # ----------------------------------------------------------------
    # 权限检查
    # ----------------------------------------------------------------

    def _check_permission(self, event: AstrMessageEvent) -> Optional[str]:
        """返回 None = 通过，非 None = 被拦截的原因"""
        # AstrBot 管理员直接放行
        if event.is_admin():
            return None

        session_id = str(event.session)

        # 黑名单优先
        blacklist = self.config.get("sessions_blacklist", []) or []
        if blacklist and session_id in blacklist:
            _log_debug(self.config, f"ai_speak: 会话 {session_id} 在黑名单中，拦截")
            return "blacklisted"

        # 白名单
        whitelist = self.config.get("sessions_whitelist", []) or []
        if whitelist and session_id not in whitelist:
            _log_debug(self.config, f"ai_speak: 会话 {session_id} 不在白名单中，拦截")
            return "not_whitelisted"

        return None

    # ----------------------------------------------------------------
    # Provider 选取：首选 → 兜底 → 系统默认
    # ----------------------------------------------------------------

    def _get_tts_provider(self, event: AstrMessageEvent) -> Optional[TTSProvider]:
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
        if not provider_id:
            return None
        p = self.context.get_provider_by_id(provider_id)
        if p is None:
            logger.warning(
                f"ai_speak: Provider ID '{provider_id}' 未找到，请检查 _conf_schema.json 配置"
            )
            return None
        if not isinstance(p, TTSProvider):
            logger.warning(
                f"ai_speak: Provider '{provider_id}' 类型不是 TTSProvider（got {type(p).__name__}）"
            )
            return None
        return p

    # ----------------------------------------------------------------
    # 速率限制
    # ----------------------------------------------------------------

    def _check_rate_limit(self, session_id: str) -> Optional[str]:
        rate_seconds = self.config.get("rate_limit_seconds", 5)
        if rate_seconds <= 0:
            return None

        last_time = self._last_tts_time.get(session_id)
        if last_time is None:
            return None

        elapsed = (datetime.now() - last_time).total_seconds()
        if elapsed < rate_seconds:
            _log_debug(
                self.config,
                f"ai_speak: 会话 {session_id} 频率限制 ({elapsed:.1f}s < {rate_seconds}s)"
            )
            return "rate_limited"
        return None


# ====================================================================
# 小工具
# ====================================================================

def _log_debug(config: dict, msg: str):
    """仅在 log_level=debug 时输出日志"""
    if config.get("log_level", "info") == "debug":
        logger.debug(msg)
