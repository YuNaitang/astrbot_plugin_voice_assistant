"""聆音 — private_companion 共存桥接。PC 检测、共存模式管理、配置同步。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger


# ── 路由模式 ──────────────────────────────────────────────────

ROUTING_MODES = {"auto", "lingyin", "other"}
ROUTING_MODE_LABELS = {
    "auto": "智能（自动检测其他插件的 TTS 状态）",
    "lingyin": "聆音（接管全部语音）",
    "other": "其他（仅作 Provider，不开事件钩子）",
}


class PCBridge:
    """private_companion 桥接检测

    负责检测 PC 是否安装、PC 的 TTS 是否启用。
    路由决策由 main.py 的 voice_routing 配置控制。
    """

    def __init__(self, plugin: Any):
        self._plugin = plugin
        self._pc_installed = False
        self._pc_tts_enabled = False

    # ── 检测 ───────────────────────────────────────────────────

    def detect(self, voice_routing: str = "auto") -> dict[str, Any]:
        """检测 PC 安装状态和 TTS 配置

        Args:
            voice_routing: 路由模式（auto/lingyin/other）

        Returns:
            {installed, tts_enabled}
        """
        self._pc_installed = self._check_installed()
        self._pc_tts_enabled = self._check_pc_tts_enabled() if self._pc_installed else False

        result = {
            "installed": self._pc_installed,
            "tts_enabled": self._pc_tts_enabled,
        }

        if self._pc_installed:
            status = "启用" if self._pc_tts_enabled else "禁用"
            logger.info(
                f"[聆音] private_companion 已安装，TTS Enhancement={{{status}}}"
            )
            if voice_routing == "lingyin" and self._pc_tts_enabled:
                logger.warning(
                    "[聆音] 路由模式设为聆音，但 PC 的 TTS Enhancement 仍为启用状态，"
                    "可能存在功能冲突！建议在 PC 配置中关闭 'enable_tts_enhancement'。"
                )
        else:
            logger.info("[聆音] 未检测到 private_companion")

        return result

    # ── 模式判断 ───────────────────────────────────────────────

    @staticmethod
    def should_enable_hooks(voice_routing: str = "auto",
                            pc_tts_enabled: bool = False) -> bool:
        """判断事件钩子是否应该生效

        auto 模式：PC TTS 未启用 → 开钩子；PC TTS 已启用 → 降级
        lingyin 模式：强制开钩子
        other 模式：强制不开
        """
        if voice_routing == "other":
            return False
        if voice_routing == "lingyin":
            return True
        # auto 模式
        return not pc_tts_enabled

    @staticmethod
    def should_register_provider() -> bool:
        """判断是否注册为 TTS Provider

        所有模式下都注册。
        """
        return True

    def description(self, voice_routing: str = "auto") -> str:
        """返回当前路由状态描述"""
        mode_label = ROUTING_MODE_LABELS.get(voice_routing, voice_routing)
        pc_status = f"PC={'已安装' if self._pc_installed else '未安装'}"
        tts_status = f"PC-TTS={'启用' if self._pc_tts_enabled else '禁用'}" if self._pc_installed else ""
        hook_status = f"事件钩子={'启用' if self.should_enable_hooks(voice_routing, self._pc_tts_enabled) else '跳过'}"
        parts = [f"[{mode_label}]", pc_status, tts_status, hook_status]
        return " | ".join(p for p in parts if p)

    # ── 内部检测 ───────────────────────────────────────────────

    @staticmethod
    def _check_installed() -> bool:
        """检测 PC 插件是否物理安装"""
        try:
            from data.plugins.astrbot_plugin_private_companion.main import get_private_companion_api  # noqa
            return get_private_companion_api() is not None
        except ImportError:
            return False
        except Exception:
            return False

    def _check_pc_tts_enabled(self) -> bool:
        """检测 PC 的 TTS Enhancement 是否启用

        通过读取 PC 的配置文件判断。
        """
        try:
            pc_config = self._read_pc_config()
            if pc_config:
                enabled = pc_config.get("enable_tts_enhancement", False)
                return bool(enabled)
        except Exception:
            pass
        return False

    @staticmethod
    def _read_pc_config() -> Optional[dict]:
        """读取 PC 的插件配置"""
        # 尝试几个可能的配置路径
        candidates = [
            Path("data/config/astrbot_plugin_private_companion.json"),
            Path("data/plugins/astrbot_plugin_private_companion/_conf_schema.json"),
        ]
        for path in candidates:
            try:
                if path.exists():
                    with open(path, encoding="utf-8") as f:
                        return json.load(f)
            except Exception:
                continue
        return None
