"""聆音 — TTS 文本清洗引擎。合成前净化文本，确保 TTS 输入干净自然。"""

from __future__ import annotations

import re
from typing import Any


# ── 清洗规则常量（移植自 PC） ─────────────────────────────────

# 移除模式：括号块、颜文字、箭头类
DEFAULT_REMOVE_PATTERNS = (
    r"[（(][^（())]{1,30}[）)]",
    r"[＞>][＿_][＜<]",
    r"[＾^][＿_][＾^]",
    r"[oO][＿_][oO]",
    r"[xX][＿_][xX]",
    r"[－-][＿_][－-]",
    r"[★☆♪♫♬♩♡♥❤️💖💕💗💓💝💟💜💛💚💙🧡🤍🖤🤎💔❣️💋]",
    r"[→←↑↓↖↗↘↙↔↕↺↻]",
)

# 过滤词：直接删除
DEFAULT_FILTER_WORDS = (
    "ω", "Ω", "σ", "Σ", "ε", "д", "Д",
    "´", "`", "＝", "∀", "∇",
    "orz", "OTZ", "QAQ", "QWQ", "TAT", "TUT", "www",
)

# 替换映射：网络用语 → 自然用语
DEFAULT_REPLACEMENTS = {
    "233": "哈哈哈",
    "666": "厉害",
    "999": "很棒",
    "555": "呜呜呜",
}

# 情感标签模式
EMOTION_TAG_PATTERN = re.compile(r"\[([^\[\]\n]{1,24})\]")
FISH_S2_CUE_PATTERN = re.compile(r"\[([^\[\]\n]{1,40})\]")
FISH_S1_CUE_PATTERN = re.compile(r"\(([^()\n]{1,24})\)", re.IGNORECASE)


class Sanitizer:
    """TTS 文本清洗器

    在语音合成前对文本做净化处理，确保 TTS 获得干净自然的输入。
    """

    def __init__(self, remove_patterns=None, filter_words=None, replacements=None):
        self._remove_patterns = list(remove_patterns or DEFAULT_REMOVE_PATTERNS)
        self._filter_words = list(filter_words or DEFAULT_FILTER_WORDS)
        self._replacements = dict(replacements or DEFAULT_REPLACEMENTS)

    def clean(self, text: str, provider_kind: str = "generic") -> str:
        """清洗文本，准备送给 TTS 合成。空字符串表示无意义内容。"""
        if not text:
            return ""
        source = str(text)
        if len(source) > 10000:
            return ""

        # 保护情感标签（对支持情感标签的 Provider）
        protected: dict[str, str] = {}
        if provider_kind.startswith("fishaudio") or provider_kind == "gsv":
            pattern_map = {"fishaudio_s1": FISH_S1_CUE_PATTERN}
            cue_pat = pattern_map.get(provider_kind, FISH_S2_CUE_PATTERN)

            def _protect(m: re.Match) -> str:
                tok = f"LYEM{len(protected)}TOK"
                protected[tok] = m.group(0)
                return tok

            source = cue_pat.sub(_protect, source)
        else:
            source = EMOTION_TAG_PATTERN.sub("", source)

        # 基础清洗：移除模式、过滤词、替换
        source = self._base_clean(source)

        # 去重（连续重复字符 >2 保留两个）
        source = re.sub(r"([^\d])\1{2,}", lambda m: m.group(1) * 2, source)

        # 清洗连续标点
        source = re.sub(r'["""]\s*["""]', "", source)
        source = re.sub(r"['']\s*['']", "", source)
        source = re.sub(r"[「」『』【】\[\]]\s*[「」『』【】\[\]]", "", source)
        source = re.sub(r"[,，、;；]\s*(?=[,，、;；\s])", "", source)
        source = re.sub(r"[,，、;；]\s*$", "", source)
        source = re.sub(r"^\s*[,，、;；]\s*", "", source)

        # 合并空格
        source = re.sub(r"\s+", " ", source).strip()

        # 回填保护的情感标签
        for token, original in protected.items():
            source = source.replace(token, original)

        source = source.strip()

        # 是否有意义内容
        if not self._has_meaningful_content(source):
            return ""

        return source

    # ── 辅助 ──────────────────────────────────────────────────

    @staticmethod
    def _has_meaningful_content(text: str) -> bool:
        """检查清洗后是否还有实质内容"""
        content = str(text or "").strip()
        if not content:
            return False
        simplified = EMOTION_TAG_PATTERN.sub("", content)
        simplified = re.sub(r"[（(][^（()]*[）)]", "", simplified)
        simplified = re.sub(r"[\s\.,，。!！?？~～…:：;；、\-—_()（）\[\]{}<>《》'\"“”‘’`|｜/\\]+", "", simplified)
        return bool(simplified)

    @staticmethod
    def is_allowed_emotion_provider(provider_kind: str) -> bool:
        """判断 TTS 引擎是否支持情感标签"""
        return provider_kind.startswith("fishaudio") or provider_kind == "gsv"

    # ── 兜底轻量清洗（供 Provider 直接调用） ──────────────────

    def fallback_clean(self, text: str) -> str:
        """轻量清洗，供 Provider 兜底调用。"""
        if not text:
            return ""
        if len(text) > 10000:
            return ""
        return self._base_clean(str(text))

    def _base_clean(self, source: str) -> str:
        """基础清洗：移除模式、过滤词、替换、合并空格。"""
        for pattern in self._remove_patterns:
            try:
                source = re.sub(pattern, "", source)
            except re.error:
                continue
        for word in self._filter_words:
            source = source.replace(word, "")
        for orig, repl in self._replacements.items():
            source = source.replace(orig, repl)
        return re.sub(r"\s+", " ", source).strip()
