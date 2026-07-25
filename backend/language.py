"""聆音 — TTS 语言引擎。AI直接输出目标语言 → 聆音校验 → 按需 LLM 翻译兜底。"""

from __future__ import annotations

import re as _re
from typing import Any, Optional


# 中文标记词（出现在文本中表明大概率是中文而非日语）
CHINESE_MARKERS = (
    "的", "了", "吗", "呢", "吧", "呀", "哦", "啊", "嘛",
    "就是", "有点", "很", "超", "画风", "氛围", "标签", "喜欢",
    "不好意思", "说出口", "温柔",
)


class LanguageEngine:
    """TTS 语言检测与转换引擎"""

    SUPPORTED_LANGUAGES = {"ja", "zh", "en"}
    LANGUAGE_LABELS = {"ja": "日语", "zh": "中文", "en": "英语"}

    def __init__(self):
        self._conversion_provider = None

    # ── 语言检测 ──────────────────────────────────────────────

    def needs_conversion(self, text: str, target_lang: str) -> bool:
        """检测文本是否需要转换到目标语种

        Args:
            text: 待检测文本（已清洗）
            target_lang: 目标语种（"ja", "zh", "en"）

        Returns:
            True 需要转换，False 不需要
        """
        if not text or target_lang not in self.SUPPORTED_LANGUAGES:
            return False

        spoken = text.strip()
        if not spoken:
            return False

        # 目标是中文 → 不需转换（视为已正确）
        if target_lang == "zh":
            return False

        cjk_count = len(_re.findall(r"[一-鿿]", spoken))

        # 目标是英语 → 如果含 CJK 字符就需转换
        if target_lang == "en":
            return cjk_count > 0

        # 目标是日语 → 检查是否混入了需要转换的中文
        if target_lang != "ja":
            return False

        kana_count = len(_re.findall(r"[぀-ヿㇰ-ㇿ]", spoken))

        # 含中文标记词 → 大概率是中文文本，需要转换
        if any(marker in spoken for marker in CHINESE_MARKERS):
            return True

        # 无假名且 CJK ≥ 4 → 需要转换
        if not kana_count and cjk_count >= 4:
            return True

        # CJK 较多且假名比例低 → 需要转换
        if cjk_count >= 6 and kana_count < max(2, int(cjk_count * 0.35)):
            return True

        return False

    # ── 语言转换（LLM 翻译兜底） ─────────────────────────────

    async def convert(self, text: str, target_lang: str,
                      provider) -> str:
        """将文本翻译为目标语种（LLM 兜底）

        当 Layer 1（AI 直接输出目标语言）质量不达标时，
        调用此方法做 LLM 翻译。

        Args:
            text: 需要翻译的文本
            target_lang: 目标语种
            provider: LLM Provider 实例

        Returns:
            翻译后的文本
        """
        if not text or not provider:
            return text

        lang_label = self.LANGUAGE_LABELS.get(target_lang, target_lang)
        prompt = (
            f"请把下面内容改写成自然{lang_label}口语，适合语音朗读。要求：\n"
            f"1. 专有名词（人名、品牌、术语）保留原文\n"
            f"2. 保留原本的情绪、语气和人格色彩\n"
            f"3. 只输出朗读文本，不要解释，不要加标签\n"
            f"4. 输出必须是完整自然的{lang_label}句子\n\n"
            f"待改写文本：\n{text}"
        )

        try:
            max_tokens = max(360, min(3000, len(str(text or "")) * 2 + 120))
            resp = await provider.text_chat(
                prompt=prompt,
                max_tokens=max_tokens,
            )
            result = str(getattr(resp, "completion_text", resp) or "").strip()
            return result if result else text
        except Exception:
            return text

    # ── 语言质量校验（Layer 2 校验） ─────────────────────────

    def check_quality(self, lingyin_text: str, target_lang: str) -> tuple[bool, str]:
        """检查 <lingyin> 块内的文本是否像目标语种

        Args:
            lingyin_text: <lingyin> 内的文本
            target_lang: 目标语种

        Returns:
            (是否通过校验, 建议)
        """
        if not lingyin_text or target_lang not in self.SUPPORTED_LANGUAGES:
            return True, ""

        if target_lang == "ja":
            kana = _re.findall(r"[぀-ヿㇰ-ㇿ]", lingyin_text)
            cjk = _re.findall(r"[一-鿿]", lingyin_text)
            # 大量汉字 + 极少假名 → 可能不是日语
            if len(cjk) >= 6 and len(kana) < max(2, int(len(cjk) * 0.35)):
                return False, "文本含大量汉字但假名比例低，可能不是自然日语"
            # 含中文标记词 → 可能是中文
            if any(m in lingyin_text for m in CHINESE_MARKERS):
                return False, f"文本含中文标记词（{'/'.join(m for m in CHINESE_MARKERS if m in lingyin_text)}），可能不是日语"

        elif target_lang == "en":
            # 含 CJK 字符 → 不是英语
            if _re.search(r"[一-鿿]", lingyin_text):
                return False, "文本含中文字符，可能不是英语"

        return True, ""
