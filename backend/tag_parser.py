"""聆音 — TTS 标签解析 & 响应保护。归一化/解析/保护 <lingyin>/<tts>/<pc_tts> 标签及情感标注。"""

from __future__ import annotations

import re
import uuid
from typing import Any

from astrbot.api import logger

from .pipeline import TtsSegment
from .emotion import TONE_PATTERN

# 自定义标签 <lingyin>（聆音原生）
LINGYIN_PATTERN = re.compile(r"<lingyin>(.*?)</lingyin>", re.IGNORECASE | re.DOTALL)
LINGYIN_TAG = re.compile(r"</?lingyin>", re.IGNORECASE)

# 兼容标签 <tts>/<pc_tts>（PC / AstrBot 原生）
TTS_ALT_PATTERN = re.compile(r"<t{2,}s\b[^>]*>.*?</t{2,}s>", re.IGNORECASE | re.DOTALL)
TTS_ALT_TAG = re.compile(r"</?(?:pc[_-]?tts|t{2,}s)\b[^>]*>", re.IGNORECASE)

# 情感标注
# （TONE_PATTERN 定义在 emotion.py 中，此处复用）

# Token 保护
TOKEN_PREFIX = "LINGYIN"


class TagParser:
    """标签解析器

    兼容三种标签格式：<lingyin>、<tts>、<pc_tts>
    """

    @staticmethod
    def normalize(text: str, extra_tags: list[str] | None = None) -> str:
        """统一所有 TTS 标签为 <lingyin> 格式

        Args:
            text: 含标签的文本
            extra_tags: 额外兼容的标签名列表（不含尖括号），如 ["my_tts"]
        """
        source = str(text or "")
        # PC 标签 → 统一
        source = re.sub(
            r"<(/?)pc[_-]?tts\b[^>]*>",
            lambda m: f"</lingyin>" if m.group(1) else "<lingyin>",
            source,
            flags=re.IGNORECASE,
        )
        source = re.sub(
            r"<(/?)t{2,}s\b[^>]*>",
            lambda m: f"</lingyin>" if m.group(1) else "<lingyin>",
            source,
            flags=re.IGNORECASE,
        )
        # 自定义标签 → 统一
        if extra_tags:
            for tag in extra_tags:
                tag = tag.strip().strip("<>")
                if not tag or tag == "lingyin":
                    continue
                source = re.sub(
                    rf"<(/?){re.escape(tag)}\b[^>]*>",
                    lambda m: f"</lingyin>" if m.group(1) else "<lingyin>",
                    source,
                    flags=re.IGNORECASE,
                )
        return source

    @staticmethod
    def parse(text: str) -> list[TtsSegment]:
        """解析 <lingyin> 标签

        Args:
            text: 含 <lingyin> 标签的文本

        Returns:
            TtsSegment 列表
        """
        source = TagParser.normalize(str(text or ""))
        segments: list[TtsSegment] = []
        pos = 0

        for match in re.finditer(r"<lingyin>(.*?)</lingyin>", source, re.IGNORECASE | re.DOTALL):
            before = source[pos:match.start()].strip()
            content = match.group(1).strip()

            # 检查 <lingyin> 块后的可见文本（到下一个标签或结尾）
            next_pos = match.end()
            after = source[next_pos:].strip()
            # 只拿标签后到下一个 <lingyin> 之间的文本
            next_tag = re.search(r"<lingyin>", after, re.IGNORECASE)
            if next_tag:
                after = after[:next_tag.start()].strip()
            else:
                after = after  # 取全部剩余文本

            if content:
                segments.append(TtsSegment(
                    before_text=before,
                    lingyin_text=content,
                    after_text=after,
                ))
            pos = match.end()

        # 如果没有任何标签，把整段文本作为 before_text
        if not segments and source.strip():
            segments.append(TtsSegment(before_text=source.strip()))

        return segments

    @staticmethod
    def extract_tone(text: str) -> tuple[str, str]:
        """从文本中提取 [tone:xxx] 标注

        Args:
            text: 含 [tone:xxx] 的文本

        Returns:
            (情感标签, 移除标注后的文本)
        """
        match = TONE_PATTERN.search(text)
        if match:
            tone = match.group(1).strip().lower()
            cleaned = TONE_PATTERN.sub("", text).strip()
            return tone, cleaned
        return "", text

    @staticmethod
    def protect_blocks(text: str) -> tuple[str, dict[str, str]]:
        """用 Token 保护 <lingyin> 块，防止框架拆分

        Args:
            text: 含 <lingyin> 标签的文本

        Returns:
            (保护后的文本, token 映射表)
        """
        protected: dict[str, str] = {}

        def _repl(match: re.Match) -> str:
            token = f"[[{TOKEN_PREFIX}:{uuid.uuid4().hex[:16]}]]"
            protected[token] = match.group(0)
            return token

        result = re.sub(r"<lingyin>.*?</lingyin>", _repl, text, flags=re.IGNORECASE | re.DOTALL)
        return result, protected

    @staticmethod
    def restore_blocks(text: str, protected: dict[str, str]) -> str:
        """恢复被 Token 保护的 <lingyin> 块"""
        if not protected:
            return text

        def _repl(match: re.Match) -> str:
            return protected.get(match.group(1), "")

        return re.sub(rf"\[\[{TOKEN_PREFIX}:([0-9a-f]{{16}})\]\]", _repl, text)

    @staticmethod
    def strip_any_tts_markup(text: str) -> str:
        """移除所有 TTS 标签（兜底清理用）"""
        cleaned = re.sub(r"</?lingyin>", "", str(text or ""), flags=re.IGNORECASE)
        cleaned = re.sub(r"</?(?:pc[_-]?tts|t{2,}s)\b[^>]*>", "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()
