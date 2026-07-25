"""
聆音 — 情感引擎

AI 自标情感 [tone:xxx] → Keyword 兜底 → 韵律降级。
"""

from __future__ import annotations

import re
from typing import Optional

# ── AI 自标情感模式 ──────────────────────────────────────────

TONE_PATTERN = re.compile(r"\[tone:\s*([a-zA-Z_]+)\]", re.IGNORECASE)

# AI 自标别名映射（各种写法 → 规范名）
TONE_ALIASES = {
    "angry": "angry", "生气": "angry", "怒": "angry",
    "happy": "happy", "开心": "happy", "高兴": "happy", "joyful": "happy",
    "sad": "sad", "难过": "sad", "伤心": "sad", "悲": "sad",
    "surprised": "surprised", "惊讶": "surprised", "吃惊": "surprised",
    "worried": "worried", "担心": "worried", "心配": "worried",
    "excited": "excited", "兴奋": "excited", "激动": "excited",
    "grateful": "grateful", "感谢": "grateful", "谢谢": "grateful",
    "comforting": "comforting", "安慰": "comforting", "温柔": "comforting",
    "sleepy": "sleepy", "困": "sleepy", "眠": "sleepy",
    "embarrassed": "embarrassed", "害羞": "embarrassed", "恥": "embarrassed",
    "whispering": "whispering", "悄悄": "whispering", "小声": "whispering",
    "laughing": "laughing", "笑": "laughing",
    "sighing": "sighing", "叹气": "sighing",
    "soft": "soft", "softly": "soft", "轻声": "soft",
}

# ── Keyword 检测规则（移植自 PC） ───────────────────────────

EMOTION_RULES: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = (
    ("angry", (
        (r"气死|氣死|生气|生氣|火大|滚开|滾開|混蛋|ふざけ|むかつ|怒って|怒る", 4),
    )),
    ("upset", (
        (r"笨蛋|ばか|バカ|都说了|都說了|怎么还|怎麼還|不许|不許|不准|烦死|煩死|讨厌啦|討厭啦|やめて|って言った|しつこい|ひどい", 3),
        (r"(?:^|[\s，,。.!！?？…~～])(哼|ふん|むぅ|まったく)(?:[\s，,。.!！?？…~～]|$)", 1),
    )),
    ("sad", (
        (r"难过|難過|伤心|傷心|想哭|泪|淚|悲しい|つらい|寂しい|泣きたい", 3),
    )),
    ("worried", (
        (r"担心|擔心|小心一点|小心一點|没事吧|沒事吧|还好吗|還好嗎|心配|大丈夫[？?]|気をつけ", 3),
    )),
    ("surprised", (
        (r"竟然|居然|真的吗|真的嗎|真的假的|没想到|沒想到|えっ|ええっ|まさか|本当[？?]", 3),
    )),
    ("excited", (
        (r"好期待|太棒了|好耶|冲冲冲|衝衝衝|迫不及待|楽しみ|わくわく|最高|やった", 3),
    )),
    ("happy", (
        (r"开心|開心|高兴|高興|喜欢你|喜歡你|爱你|愛你|太好了|嬉しい|楽しい|大好き|よかった", 3),
    )),
    ("grateful", (
        (r"谢谢你|謝謝你|感谢|感謝|多亏你|多虧你|ありがとう|助かった", 3),
    )),
    ("comforting", (
        (r"别怕|別怕|没关系|沒關係|我陪你|我在呢|慢慢来|慢慢來|そばにいる|無理しないで|安心して", 3),
    )),
    ("sleepy", (
        (r"好困|困死|想睡|睡着|睡著|打哈欠|眠い|眠たい|寝たい|あくび", 3),
    )),
    ("embarrassed", (
        (r"害羞|羞死|脸红|臉紅|不好意思|别看|別看|被发现|被發現|恥ずか|照れ|顔が赤|見ないで", 3),
    )),
)

# 语气规则
TONE_RULES: tuple[tuple[str, str], ...] = (
    ("sighing", r"(?:^|[\s，,、。.!！?？…~～])(唉|哎|呜+|嗚+|唔|はぁ|ふぅ|うーん|まったく)(?:[\s，,、。.!！?？…~～]|$)"),
    ("whispering", r"悄悄|小声|小聲|耳边|耳邊|こっそり|囁|小声で"),
    ("laughing", r"哈哈|嘿嘿|嘻嘻|笑死|ふふ|はは|あはは|笑っ"),
    ("sobbing", r"哭了|哭泣|抽泣|泣いて|すすり泣|しくしく"),
    ("soft", r"晚安|慢慢说|慢慢說|轻声|輕聲|おやすみ|優しく|そっと"),
)


class EmotionEngine:
    """分层情感引擎"""

    def __init__(self):
        pass

    # ── 主入口 ────────────────────────────────────────────────

    def apply(self, text: str, tone_tag: str = "",
              provider_kind: str = "generic") -> str:
        """应用情感控制。AI自标 → Keyword → 无情感，按 provider_kind 选择标记方式。"""
        if not text:
            return text

        # Step 1: 确定情感
        tone = self._resolve_tone(text, tone_tag)

        # Step 2: 应用标记
        return self._annotate(text, tone, provider_kind)

    # ── 情感解析 ──────────────────────────────────────────────

    def _resolve_tone(self, text: str, tone_tag: str = "") -> str:
        """确定情感标签：AI自标 > Keyword > 空"""
        if tone_tag:
            normalized = TONE_ALIASES.get(tone_tag.lower(), tone_tag.lower())
            return normalized

        # Keyword 检测
        keyword_tone = self._keyword_detect(text)
        return keyword_tone

    @staticmethod
    def parse_self_annotation(text: str) -> tuple[str, str]:
        """从文本中提取 AI 自标情感 [tone:xxx]

        Args:
            text: 含 [tone:xxx] 的文本

        Returns:
            (情感标签, 移除标注后的文本)
        """
        match = TONE_PATTERN.search(text)
        if match:
            tone = match.group(1).strip().lower()
            normalized = TONE_ALIASES.get(tone, tone)
            cleaned = TONE_PATTERN.sub("", text).strip()
            return normalized, cleaned
        return "", text

    # ── Keyword 检测（移植自 PC） ─────────────────────────────

    def _keyword_detect(self, text: str) -> str:
        """基于关键词的文本情感检测"""
        source = re.sub(r"\s+", " ", str(text or "")).strip().lower()
        if not source:
            return ""

        # 计算每种情绪的分数
        scores: dict[str, int] = {}
        for cue, patterns in EMOTION_RULES:
            scores[cue] = sum(
                weight for pattern, weight in patterns
                if re.search(pattern, source, re.IGNORECASE)
            )

        # 检测"撒娇抱怨"模式
        playful_complaint = bool(re.search(r"嘛|啦|呀|哦|呜|嗚|唔|じゃん|だもん|バカ|ばか|[~～]", source))
        if scores.get("upset", 0) >= 3 and scores.get("angry", 0) < scores["upset"] and playful_complaint:
            scores["embarrassed"] = max(scores.get("embarrassed", 0), 2)

        # 主情绪选择
        priority = (
            "angry", "upset", "sad", "worried", "surprised", "excited",
            "happy", "grateful", "comforting", "sleepy", "embarrassed",
        )
        primary = max(
            [c for c in priority if scores.get(c, 0) >= 3],
            key=lambda c: (scores[c], -priority.index(c)),
            default="",
        )

        # 语气检测
        tones = [c for c, p in TONE_RULES if re.search(p, source, re.IGNORECASE)]
        if not primary:
            return tones[0] if tones else ""

        return primary

    # ── 标注方式 ──────────────────────────────────────────────

    def _annotate(self, text: str, tone: str,
                  provider_kind: str) -> str:
        """根据 TTS 引擎类型选择标注方式"""
        if not tone:
            return text

        if provider_kind == "fishaudio_s1":
            # 圆括号标签
            tag = f"({tone})"
            return f"{tag}{text}"

        if provider_kind.startswith("fishaudio"):
            # FishAudio S2 兼容：方括号标签
            tag = f"[{tone}]"
            return f"{tag}{text}"

        if provider_kind == "gsv":
            # GSV：中文方括号标签
            chinese_tags = {
                "happy": "[开心]", "sad": "[难过]", "angry": "[生气]",
                "surprised": "[惊讶]", "whispering": "[小声]",
                "laughing": "[笑]", "sighing": "[叹气]", "soft": "[温柔]",
                "embarrassed": "[害羞]", "excited": "[兴奋]",
            }
            return f"{chinese_tags.get(tone, f'[{tone}]')}{text}"

        # Generic Provider：韵律降级
        return self._prosody_fallback(text, tone)

    # ── 韵律降级 ──────────────────────────────────────────────

    @staticmethod
    def _prosody_fallback(text: str, tone: str) -> str:
        """TTS 引擎不支持情感标签时的文本层语气调整（如加语气词、标点）。"""
        if not text:
            return text

        # 耳语 → 加括号
        if tone in ("whispering", "soft"):
            return f"（{text}）"

        # 兴奋 → 加感叹号（如果句尾没有）
        if tone == "excited":
            if not text.rstrip().endswith(("！", "!")):
                return f"{text}！"
            return text

        # 悲伤 → 加省略号
        if tone == "sad":
            if not text.rstrip().endswith("…"):
                return f"{text}……"
            return text

        # 叹气 → 句首加语气词
        if tone == "sighing":
            if not text.startswith(("唉", "哎", "はぁ")):
                return f"唉……{text}"
            return text

        # 安慰 → 句尾加波浪号
        if tone == "comforting":
            return f"{text}~"

        return text
