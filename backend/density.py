"""
AI Voice Assistant — 密度控制器
================================
管理会话级密度（硬阻断）和用户级密度（概率降权）。
"""
import random
from datetime import datetime, timedelta
from math import exp
from typing import Optional

from astrbot.api import logger


class DensityController:
    """双层密度控制：会话级硬阻断 + 用户级概率降权。"""

    def __init__(self, config: dict):
        self.config = config

        # 会话级密度（硬阻断）
        self._voice_timeline: dict[str, list[datetime]] = {}
        self._density_warned: set[str] = set()

        # 用户级密度（概率降权）
        self._user_trigger_timeline: dict[str, dict[str, list[datetime]]] = {}

    # ── 会话级硬阻断 ──────────────────────────────────────────

    @staticmethod
    def _prune_timeline(timestamps: list[datetime], window_minutes: int) -> list[datetime]:
        now = datetime.now()
        cutoff = now - timedelta(minutes=window_minutes)
        return [t for t in timestamps if t > cutoff]

    def is_over_density_limit(self, session_id: str) -> bool:
        window = self.config.get("density_window_minutes", 10)
        max_count = self.config.get("density_max_count", 3)
        timeline = self._voice_timeline.get(session_id, [])
        timeline = self._prune_timeline(timeline, window)
        self._voice_timeline[session_id] = timeline
        return len(timeline) >= max_count

    # ── 用户级概率降权 ───────────────────────────────────────

    def get_user_probability(self, session_id: str, user_id: str) -> float:
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

    # ── 综合决策 ──────────────────────────────────────────────

    def should_allow(self, session_id: str, user_id: str) -> tuple:
        """综合决策：先会话硬阻断，再用户概率降权。
        Returns: (是否允许: bool, 原因描述: str)
        """
        if self.is_over_density_limit(session_id):
            reason = f"会话语音密度超限，请稍后再试"
            logger.info(f"[密度结果] 拒绝 — {reason}")
            return False, reason

        prob = self.get_user_probability(session_id, user_id)
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

    # ── 记录发送 ──────────────────────────────────────────────

    def record_sent(self, session_id: str, user_id: str):
        self._voice_timeline.setdefault(session_id, []).append(datetime.now())
        user_map = self._user_trigger_timeline.setdefault(session_id, {})
        user_map.setdefault(user_id, []).append(datetime.now())
        self._density_warned.discard(session_id)

    def is_warned(self, session_id: str) -> bool:
        return session_id in self._density_warned

    def mark_warned(self, session_id: str):
        self._density_warned.add(session_id)
