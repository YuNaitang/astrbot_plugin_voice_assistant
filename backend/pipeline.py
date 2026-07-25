"""聆音 — 核心管线编排 & 接口契约。引擎模块接口定义 + on_decorating_result 管线编排。"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from astrbot.api import logger
from astrbot.core.message.components import Plain, Record
from astrbot.core.provider.provider import TTSProvider


# ════════════════════════════════════════════════════════════════════
# 接口契约（Interface Contract）
# ════════════════════════════════════════════════════════════════════


@dataclass
class TtsSegment:
    """标签解析结果中的一个语音段"""
    before_text: str = ""       # <lingyin> 前的可见文本
    lingyin_text: str = ""      # <lingyin> 内的朗读文本
    after_text: str = ""        # <lingyin> 后的可见文本（如果有）




@dataclass
class TtsContext:
    """TTS 处理的完整上下文，在管线中传递"""
    segments: list[TtsSegment] = field(default_factory=list)
    provider: Optional[TTSProvider] = None
    provider_kind: str = "generic"
    target_language: str = ""
    foreign_text_display: str = "foreign_first"
    tone_tag: str = ""           # AI 自标的 [tone:xxx]
    event: Any = None
    fallback_plain: str = ""     # 合成失败时的兜底文本


# ── 引擎接口签名 ─────────────────────────────────────────────────

# EmotionEngine.apply(text, tone_tag="", provider_kind="generic") -> str
#   - text: 原始文本
#   - tone_tag: AI 自标的情感标签（如 "happy"）
#   - provider_kind: TTS 引擎类型（"fishaudio_s2", "generic" 等）
#   - returns: 应用情感标记后的文本

# Sanitizer.clean(text, provider_kind="generic") -> str
#   - text: 原始文本
#   - provider_kind: TTS 引擎类型
#   - returns: 清洗后的文本（空字符串 = 无意义内容）

# LanguageEngine.needs_conversion(text, target_lang) -> bool
#   - text: 待检测文本
#   - target_lang: 目标语种（"ja", "en", "zh"）
#   - returns: True 需要转换

# LanguageEngine.convert(text, target_lang, provider) -> str
#   - text: 待翻译文本
#   - target_lang: 目标语种
#   - provider: LLM Provider 实例（用于翻译）
#   - returns: 翻译后的文本

# FrequencyGate.check(event) -> GateResult
#   - event: AstrBot 事件
#   - returns: GateResult(allowed, reason, cooldown_remaining)

# TagParser.parse(text) -> list[TtsSegment]
#   - text: 含 <lingyin> 标签的文本
#   - returns: 解析后的语音段列表

# 音频合成：在 pipeline.execute() 中直接调 provider.get_audio()
#   - 清洗 → 情感 → 语言 → provider.get_audio() → Record
#   - 无需独立的 AudioGenerator 类


# ════════════════════════════════════════════════════════════════════
# 管线编排
# ════════════════════════════════════════════════════════════════════


class TtsPipeline:
    """TTS 核心编排器

    三阶段钩子的入口统一在此：
    - on_llm_request: injector
    - on_llm_response: tag_parser
    - on_decorating_result: pipeline.execute()
    """

    def __init__(self, plugin: Any, sanitizer, emotion,
                 language, tag_parser=None, frequency=None):
        self._plugin = plugin
        self.context = plugin.context if hasattr(plugin, "context") else None
        self.sanitizer = sanitizer
        self.emotion = emotion
        self.language = language
        self.frequency = frequency
        self._tag_parser = tag_parser

    # ── 主编排入口 ──────────────────────────────────────────────

    async def execute(self, ctx: TtsContext) -> list[Any]:

        components: list[Any] = []
        for seg in ctx.segments:
            if not seg.lingyin_text:
                if seg.before_text:
                    components.append(Plain(seg.before_text))
                continue

            # 1. 清洗
            cleaned = self.sanitizer.clean(seg.lingyin_text, ctx.provider_kind)
            if not cleaned:
                continue

            # 2. 情感
            toned = self.emotion.apply(cleaned, ctx.tone_tag, ctx.provider_kind)

            # 3. 语言转换（按需）
            if ctx.target_language:
                if self.language.needs_conversion(toned, ctx.target_language):
                    llm_provider = await self._get_llm_provider()
                    if llm_provider:
                        toned = await self.language.convert(
                            toned, ctx.target_language, llm_provider
                        )

            # 4. 合成音频
            if ctx.provider:
                try:
                    audio_path = await ctx.provider.get_audio(toned)
                    if audio_path:
                        record = Record(file=str(audio_path), url=str(audio_path))
                        components.append(record)

                        # 5. 补充标签外的可见文本
                        # before_text = LLM 写在标签前的文字
                        if seg.before_text and seg.before_text.strip():
                            # 避免与 before_text 重复（当 before_text 已经被输出为片段时）
                            if not components or str(components[-1]) != seg.before_text:
                                components.append(Plain(seg.before_text.strip()))
                        # after_text = LLM 写在标签后的文字
                        if seg.after_text and seg.after_text.strip():
                            components.append(Plain(seg.after_text.strip()))
                        continue
                except Exception as exc:
                    logger.warning(
                        f"[聆音] 语音合成失败: {exc} text={toned[:60]}"
                    )

            # 合成失败 → 输出原文
            components.append(Plain(toned))

        if not components:
            return self._stub_fallback(ctx)
        return components

    # ── 可见文本管理 ──────────────────────────────────────────

    @staticmethod
    def _has_chinese_text(text: str) -> bool:
        """检测文本是否为有效的中文可见文本"""
        if not text or not text.strip():
            return False
        cleaned = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
        if not cleaned:
            return False
        # 含假名 → 不是中文
        if re.search(r"[぀-ヿㇰ-ㇿ]", cleaned):
            return False
        cjk_count = len(re.findall(r"[一-鿿]", cleaned))
        if cjk_count < 2:
            return False
        chinese_markers = (
            "的", "了", "是", "我", "你", "他", "她", "它", "们",
            "这", "那", "不", "有", "在", "就", "吗", "呢", "吧",
        )
        return any(m in cleaned for m in chinese_markers) or cjk_count >= 4

    def _resolve_visible_text(self, seg, ctx) -> str:
        """决定语音块后的可见文本

        策略受 send_foreign_text_display 控制（仅在非中文语种时生效）：
        - foreign_only: 仅外语原文
        - translation_only: 仅中文翻译
        - foreign_first: 外语原文 + 中文翻译
        - translation_first: 中文翻译 + 外语原文
        """
        target_lang = getattr(ctx, "target_language", "")
        display_mode = getattr(ctx, "foreign_text_display", "foreign_first")

        if target_lang in ("", "auto", "zh"):
            return (seg.after_text or seg.before_text or "").strip()

        # 非中文语种
        visible = (seg.after_text or seg.before_text or "").strip()
        fallback = getattr(ctx, "fallback_plain", "")

        # 检测是否需要翻译标注
        original = visible or fallback

        if display_mode == "foreign_only":
            return original
        elif display_mode == "translation_only":
            return ""
        elif display_mode == "foreign_first":
            return original
        elif display_mode == "translation_first":
            # 翻译 + 原文：当前无翻译能力时只返回原文
            return original
        return original

    async def check_frequency(self, event: Any):
        """频率门控检查"""
        if self.frequency:
            return self.frequency.check(event)
        return True

    # ── 辅助 ────────────────────────────────────────────────────

    def _stub_fallback(self, ctx: TtsContext) -> list[Any]:
        """stub 模式兜底：输出原始文本"""
        plain_parts = []
        for seg in ctx.segments or []:
            if seg.before_text:
                plain_parts.append(seg.before_text)
            # lingyin_text 在 stub 模式下直接输出
            if seg.lingyin_text:
                plain_parts.append(seg.lingyin_text)
            if seg.after_text:
                plain_parts.append(seg.after_text)
        text = " ".join(p.strip() for p in plain_parts if p.strip()) or ctx.fallback_plain
        return [Plain(text)] if text else []

    async def _get_llm_provider(self):
        """获取 LLM Provider（用于语言翻译等）"""
        try:
            return self.context.get_using_provider()
        except Exception:
            return None
