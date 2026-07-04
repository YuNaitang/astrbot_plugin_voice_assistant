"""
聆音 — 权限管理
====================================
权限等级: 0=无限制, 1=基准限制, 2=完全限制。
支持按 session ID / QQ 号单独配置，支持 /voice_perm 指令管理。
"""
import json as _json
import os
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType


PERM_UNLIMITED = 0
PERM_BASIC = 1
PERM_RESTRICTED = 2

PERM_LABELS = {0: "无限制", 1: "基准限制", 2: "完全限制"}


class PermissionManager:
    """会话语音权限管理器。"""

    def __init__(self, config: dict):
        self.config = config
        self.cache: dict[str, int] = {}
        self.load_cache()

    # ----------------------------------------------------------------
    # 缓存加载
    # ----------------------------------------------------------------

    def load_cache(self):
        """从配置加载权限映射到缓存。"""
        self.cache.clear()

        # 1. 新格式：session_permissions
        entries = self.config.get("session_permissions", []) or []
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.rsplit(":", 1)
            if len(parts) == 2:
                sid, level_str = parts
                try:
                    level = int(level_str)
                    if level in (PERM_UNLIMITED, PERM_BASIC, PERM_RESTRICTED):
                        self.cache[sid.strip()] = level
                except ValueError:
                    logger.warning(f"[voice_perm] 无效的权限配置条目: {entry}")

        # 2. 兼容旧格式：sessions_blacklist → level=2
        blacklist = self.config.get("sessions_blacklist", []) or []
        for sid in blacklist:
            sid = sid.strip()
            if sid and sid not in self.cache:
                self.cache[sid] = PERM_RESTRICTED

    # ----------------------------------------------------------------
    # 查询
    # ----------------------------------------------------------------

    def get_level(self, event: AstrMessageEvent) -> int:
        """获取会话的语音权限等级。"""
        session_str = str(event.session)
        msg_type = event.session.message_type
        sid = event.session.session_id

        if session_str in self.cache:
            return self.cache[session_str]
        if sid and sid in self.cache:
            return self.cache[sid]
        if event.is_admin() and msg_type == MessageType.FRIEND_MESSAGE:
            return PERM_UNLIMITED
        return self.config.get("default_permission_level", PERM_BASIC)

    # ----------------------------------------------------------------
    # 修改
    # ----------------------------------------------------------------

    def set_level(self, session_id: str, level: int):
        """保存权限到配置并刷新缓存。"""
        entries = self.config.get("session_permissions", []) or []
        prefix = f"{session_id}:"
        new_entries = [e for e in entries if not e.startswith(prefix)]
        new_entries.append(f"{session_id}:{level}")
        self.config["session_permissions"] = new_entries
        self.load_cache()
        self._persist()

    def remove_level(self, session_id: str):
        """删除自定义权限配置，恢复默认等级。"""
        entries = self.config.get("session_permissions", []) or []
        prefix = f"{session_id}:"
        self.config["session_permissions"] = [e for e in entries if not e.startswith(prefix)]
        self.load_cache()
        self._persist()

    # ----------------------------------------------------------------
    # 持久化
    # ----------------------------------------------------------------

    def _persist(self):
        """尝试持久化配置到文件。"""
        try:
            config_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "..", "..", "data", "config",
                "astrbot_plugin_voice_assistant.json",
            )
            config_path = os.path.normpath(config_path)
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                _json.dump(self.config, f, ensure_ascii=False, indent=2)
            logger.debug(f"[voice_perm] 配置已持久化: {config_path}")
        except Exception as e:
            logger.warning(f"[voice_perm] 配置持久化失败（非致命）: {e}")
