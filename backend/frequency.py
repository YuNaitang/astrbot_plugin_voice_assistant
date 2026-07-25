"""聆音 — 频率门控引擎。软约束 + 概率采样 + 兜底保护。频率由 AI 人格主导。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class GateResult:
    """频率门控检查结果"""
    allowed: bool = True
    reason: str = ""
    should_inject_reminder: bool = False
    density_exceeded: bool = False


class FrequencyGate:
    """频率门控（软约束 + 先验概率 + 兜底保护）

    分层逻辑（正常模式）：
    1. 软约束：同一条回复最多一段语音
    2. 密度控制：会话级硬阻断 + 用户级概率降权
    3. 速率间隔：会话/用户级防刷
    4. 概率采样：群聊/私聊各自独立概率

    强制模式（trigger_force_probability=true）：
    跳过所有约束，直接概率采样。
    """

    def __init__(self, config: Optional[dict] = None,
                 density_controller=None):
        self._config = config or {}
        self._density = density_controller  # DensityController 实例
        self._session_last_at: dict[str, float] = {}
        self._user_last_at: dict[str, float] = {}

    # ── 先验概率采样 ──────────────────────────────────────────

    def _cfg(self, group, key, default=None):
        return self._config.get(group, {}).get(key, default)

    def _get_effective_probability(self, is_group: bool) -> float:
        """获取当前会话类型的触发概率"""
        key = "trigger_group_probability" if is_group else "trigger_private_probability"
        return float(self._cfg("trigger_probability", key, 1.0))

    def _sample_probability(self, prob: float) -> bool:
        """先验概率采样"""
        if prob <= 0.0:
            return False
        if prob >= 1.0:
            return True
        return random.random() < prob

    # ── 判断消息类型 ──────────────────────────────────────────

    @staticmethod
    def _is_group_event(event: Any) -> bool:
        """判断事件是否为群聊消息"""
        try:
            from astrbot.core.platform.message_type import MessageType
            msg_type = getattr(event, "session", None)
            if msg_type is not None:
                mt = getattr(msg_type, "message_type", None)
                return mt in (MessageType.GROUP_MESSAGE,)
        except Exception:
            pass
        return False

    # ── 主编排入口 ────────────────────────────────────────────

    def check(self, event: Any, *,
              via_tool: bool = False,
              voice_count_in_reply: int = 0,
              session_id: str = "",
              user_id: str = "") -> GateResult:
        """检查是否允许语音

        Args:
            event: AstrBot 事件
            via_tool: 是否通过 ai_speak 工具调用
            voice_count_in_reply: 同一条回复中已有的语音段数
            session_id: 会话 ID
            user_id: 用户 ID

        Returns:
            GateResult
        """
        is_group = self._is_group_event(event)

        # ── 强制概率模式：跳过所有约束 ──────────────────────────
        if self._cfg("trigger_probability", "trigger_force_probability", False):
            prob = self._get_effective_probability(is_group)
            allowed = self._sample_probability(prob)
            return GateResult(
                allowed=allowed,
                reason=f"强制概率采样 ({prob:.2f})" if not allowed else "",
            )

        # ai_speak 工具不受约束
        if via_tool:
            return GateResult(allowed=True)

        # 1. 软约束：同一条回复最多一段语音
        if voice_count_in_reply >= 1:
            return GateResult(
                allowed=False,
                reason="同一条回复中最多一段语音",
            )

        # 2. 密度控制（委托 DensityController）
        if self._density is not None and session_id:
            allowed, reason = self._density.should_allow(session_id, user_id)
            if not allowed:
                return GateResult(
                    allowed=False,
                    reason=reason,
                    should_inject_reminder="密度超限" in reason,
                    density_exceeded="密度超限" in reason,
                )

        # 3. 速率间隔：会话级
        session_interval = float(self._cfg("trigger_probability", "trigger_session_interval", 0))
        if session_interval > 0 and session_id:
            last_at = self._session_last_at.get(session_id, 0.0)
            elapsed = time.time() - last_at
            if elapsed < session_interval:
                return GateResult(
                    allowed=False,
                    reason=f"会话触发间隔 ({elapsed:.0f}s < {session_interval:.0f}s)",
                )

        # 3b. 速率间隔：用户级
        user_interval = float(self._cfg("trigger_probability", "trigger_user_interval", 0))
        if user_interval > 0 and user_id:
            last_at = self._user_last_at.get(user_id, 0.0)
            elapsed = time.time() - last_at
            if elapsed < user_interval:
                return GateResult(
                    allowed=False,
                    reason=f"用户触发间隔 ({elapsed:.0f}s < {user_interval:.0f}s)",
                )

        # 4. 先验概率采样
        prob = self._get_effective_probability(is_group)
        if not self._sample_probability(prob):
            return GateResult(
                allowed=False,
                reason=f"概率采样未通过 (prob={prob:.2f})",
            )

        return GateResult(allowed=True)

    def mark_sent(self, event: Any, session_id: str = "") -> None:
        """记录语音发送时间（用于速率间隔 + 密度控制）"""
        session = self._get_session_key(event) or session_id
        if session:
            self._session_last_at[session] = time.time()

        user_id = getattr(event, "get_sender_id", lambda: "")() if not session_id else ""
        if user_id:
            self._user_last_at[user_id] = time.time()

        if self._density is not None and session_id:
            self._density.record_sent(session_id, user_id)

    @staticmethod
    def _get_session_key(event: Any) -> str:
        try:
            return str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            return ""
