"""聆音 — 密度控制器。会话级硬阻断 + 用户级概率降权。"""
import asyncio
import random
from datetime import datetime, timedelta
from math import exp
from typing import Optional

from astrbot.api import logger


class DensityController:
    """双层密度控制：会话级硬阻断 + 用户级概率降权。"""

    def __init__(self, config: dict):
        self.config = config
        self._lock = asyncio.Lock()

        # 会话级密度（硬阻断）
        self._voice_timeline: dict[str, list[datetime]] = {}
        self._density_warned: dict[str, datetime] = {}
        self._density_warned_max = 5000

        # 用户级密度（概率降权）
        self._user_trigger_timeline: dict[str, dict[str, list[datetime]]] = {}

    # ── 会话级硬阻断 ──────────────────────────────────────────

    @staticmethod
    def _prune_timeline(timestamps: list[datetime], window_minutes: int) -> list[datetime]:
        now = datetime.now()
        cutoff = now - timedelta(minutes=window_minutes)
        return [t for t in timestamps if t > cutoff]

    def is_over_density_limit(self, session_id: str) -> bool:
        window = self.config.get("trigger_probability", {}).get("trigger_density_window", 10)
        max_count = self.config.get("trigger_probability", {}).get("trigger_density_max_count", 3)
        timeline = self._voice_timeline.get(session_id, [])
        timeline = self._prune_timeline(timeline, window)
        self._voice_timeline[session_id] = timeline
        return len(timeline) >= max_count

    # ── 用户级概率降权 ───────────────────────────────────────

    def get_user_probability(self, session_id: str, user_id: str) -> float:
        window = self.config.get("trigger_probability", {}).get("trigger_user_density_window", 60)
        threshold = self.config.get("trigger_probability", {}).get("trigger_user_threshold", 5)
        steepness = self.config.get("trigger_probability", {}).get("trigger_curve_steepness", 0.7)
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
        """综合决策。Returns: (是否允许: bool, 原因描述: str)"""
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

    # ── 原子性检查 + 记录 ─────────────────────────────────────

    async def check_and_record(self, session_id: str, user_id: str) -> tuple:
        """原子性检查+记录，避免 TOCTOU。Returns: (是否允许, 原因)"""
        async with self._lock:
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

            self.record_sent(session_id, user_id)
            logger.info(f"[密度结果] 放行 — session={session_id} user={user_id}")
            return True, ""

    # ── 记录发送 ──────────────────────────────────────────────

    def record_sent(self, session_id: str, user_id: str):
        self._voice_timeline.setdefault(session_id, []).append(datetime.now())
        user_map = self._user_trigger_timeline.setdefault(session_id, {})
        user_map.setdefault(user_id, []).append(datetime.now())
        self._density_warned.pop(session_id, None)

    def is_warned(self, session_id: str) -> bool:
        return session_id in self._density_warned

    def mark_warned(self, session_id: str):
        self._density_warned[session_id] = datetime.now()
        if len(self._density_warned) > self._density_warned_max:
            self._prune_old_warnings()

    def _prune_old_warnings(self):
        """清理过期警告，防止内存泄漏。"""
        window = self.config.get("trigger_probability", {}).get("trigger_density_window", 10)
        cutoff = datetime.now() - timedelta(minutes=window)
        self._density_warned = {
            sid: ts for sid, ts in self._density_warned.items() if ts > cutoff
        }
