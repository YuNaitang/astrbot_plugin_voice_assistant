"""聆音 — System Prompt 注入引擎。在 on_llm_request 阶段向 LLM 注入语音规则。"""

from __future__ import annotations

from typing import Any, Optional


# ── 默认语音规则模板 ─────────────────────────────────────────

VOICE_RULES_TEMPLATE = """
【语音规则】
你可以使用语音来增强回复的表现力。

使用 <lingyin> 标签标记你想朗读的部分：
  <lingyin>朗读文本</lingyin>标签外写正常文字

规则：
1. 是否使用语音、用多少语音，由你的人格和当前语境自然决定
   - 如果你今天心情好/话多 → 可以用语音
   - 如果你心情平淡/话少 → 少用或不用
   - 聊天时偶尔用语音点缀更自然
2. 如果你想让语音带情感，在标签前加 [tone:xxx] 标注：
   示例：[tone:happy]<lingyin>今日はいい天気だね。</lingyin>
   支持的 tone: happy, sad, angry, surprised, worried, excited,
               grateful, comforting, sleepy, embarrassed,
               whispering, laughing, sighing, soft
3. 非中文语音在 <lingyin> 中直接写目标语言即可，
   标签外写中文释义让用户看懂
4. 适量使用语音，效果更自然
5. 如果你不想用语音，正常回复即可，不需要写任何标签
"""


class Injector:
    """System Prompt 注入器"""

    def __init__(self, target_language: str = "ja"):
        self.target_language = target_language
        self._language_labels = {"ja": "日语", "zh": "中文", "en": "英语"}

    def build_voice_rules(self, target_language: Optional[str] = None) -> str:
        """构建语音规则提示文本"""
        lang = target_language or self.target_language
        result = VOICE_RULES_TEMPLATE.strip()
        if lang and lang != "auto":
            label = self._language_labels.get(lang, lang)
            result += f"\n当前语音目标语种：{label}"
        return result

    @staticmethod
    def build_decision_line(should_use: bool) -> str:
        """返回概率决策行：本轮是否放行语音"""
        return "[语音概率提醒] 本次语音概率决策为：放行，你应该尝试使用语音。" if should_use else "[语音概率提醒] 本次语音概率决策为：不放行，你不应该使用语音。"

    def inject(self, system_prompt: str, *,
               target_language: Optional[str] = None) -> str:
        """注入语音规则到 system prompt"""
        return "\n\n" + self.build_voice_rules(target_language)
