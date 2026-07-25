"""
聆音 — 增强型 TTS Provider

注册为 AstrBot 标准 TTSProvider。内部做兜底清洗+情感增强，主增强管线在 pipeline.py。
多 Provider 降级路由：首选 → 降级 → 系统默认。
"""

from __future__ import annotations

from typing import Any, Optional

from astrbot.api import logger
from astrbot.core.provider.provider import TTSProvider

# 为避免循环导入，在方法内按需 import



# ── Provider 类型标识 ──────────────────────────────────────────

PROVIDER_TYPE = "lingyin_tts"
PROVIDER_DESC = "聆音 AI 语音合成 — 拟人化 TTS 系统"
PROVIDER_DISPLAY_NAME = "聆音 TTS"


class LingYinTTSProvider(TTSProvider):
    """聆音增强型 TTS Provider

    透明替换原生 TTS 调度。所有语音合成请求通过本 Provider 进入聆音管线。

    注意：load_provider() 创建实例时不传 context/provider_manager 参数。
    为了确保这些实例也能路由到真实引擎，使用类级全局变量保存上下文引用。
    """

    # 类级全局引用（供 load_provider() 创建的无上下文实例使用）
    _global_context = None
    _global_provider_manager = None

    @classmethod
    def set_global_context(cls, context, provider_manager):
        cls._global_context = context
        cls._global_provider_manager = provider_manager

    def __init__(self, provider_config: dict, provider_settings: dict,
                 context=None, provider_manager=None) -> None:
        super().__init__(provider_config, provider_settings)
        self._real_providers: list[TTSProvider] = []
        self._default_umo = ""
        self._context = context
        self._provider_manager = provider_manager
        # 如果创建时传了引用，也同步到类级（供其他实例兜底）
        if context is not None:
            LingYinTTSProvider._global_context = context
        if provider_manager is not None:
            LingYinTTSProvider._global_provider_manager = provider_manager

    def set_real_providers(self, providers: list[TTSProvider], default_umo: str = "") -> None:
        """设置真实 TTS 引擎列表（启动时由 main.py 传入）"""
        self._real_providers = providers
        self._default_umo = default_umo

    # ── 核心方法（AstrBot 框架调用） ────────────────────────────

    async def get_audio(self, text: str) -> str:
        """★ 所有插件透明调用的入口

        PC、框架 TtsHandler、其他插件调 get_using_tts_provider(umo)
        后，再调 get_audio(text) 时进入此处。

        兜底增强路径：当事件钩子主路径未处理时，做轻量增强。
        事件钩子已在 pipeline.py 中做完全量清洗/情感/语言处理后，
        最终也通过本方法走真实引擎。

        Args:
            text: 朗读文本（事件钩子路径：已增强；Provider 兜底路径：原始）

        Returns:
            音频文件绝对路径
        """
        # 兜底增强：先清洗再路由
        cleaned = self._fallback_sanitize(text)
        if not cleaned:
            raise ValueError("[聆音] 清洗后文本为空")
        enhanced = self._fallback_emotion(cleaned)
        return await self._route_to_real_engine(enhanced)

    # ── Provider 路由 ──────────────────────────────────────────

    async def _route_to_real_engine(self, text: str) -> str:
        """多 Provider 降级路由

        优先级：
        1. tts_provider_id（用户首选）
        2. tts_fallback_provider_id（降级）
        3. context.get_using_tts_provider()（系统默认，但跳过自身）
        4. 遍历 _real_providers 取第一个可用
        """
        config = getattr(self, "provider_config", {}) or {}

        # 1-2. 首选 + 降级（从插件配置 + 全局 provider_tts_settings 查找）
        for key in ("tts_provider_id", "tts_fallback_provider_id"):
            pid = config.get(key, "") or self._get_global_tts_provider_id(key)
            if not pid or pid == config.get("id", ""):
                continue  # 跳过指向自身的 ID
            provider = self._find_provider_by_id(pid)
            if provider is not None and provider is not self:
                return await provider.get_audio(text)

        # 3. 系统默认（跳过自身，避免循环）
        if self._default_umo:
            try:
                ctx = getattr(self, "_context", None)
                if ctx is not None:
                    provider = ctx.get_using_tts_provider(self._default_umo)
                    if provider is not None and provider is not self:
                        return await provider.get_audio(text)
            except Exception:
                pass

        # 4. 遍历真实列表
        found = self._resolve_any_real_provider()
        if found is not None:
            return await found.get_audio(text)

        # 5. 最终日志：打印所有可用 Provider 便于排查
        pm = self._get_provider_manager()
        if pm is not None:
            logger.error(f"[聆音] 路由失败：tts_provider_insts count={len(pm.tts_provider_insts)}")
            for p in pm.tts_provider_insts:
                try:
                    logger.error(f"[聆音]   tts_provider: {type(p).__name__} id={p.meta().id}")
                except Exception:
                    logger.error(f"[聆音]   tts_provider: {type(p).__name__} (meta unavailable)")
            logger.error(f"[聆音] 路由失败：inst_map keys={list(pm.inst_map.keys())}")
        else:
            logger.error("[聆音] 路由失败：无法获取 ProviderManager")
        raise RuntimeError("[聆音] 无可用的 TTS Provider，请检查 TTS 配置")

    def _get_global_tts_provider_id(self, key: str) -> str:
        """从全局 provider_tts_settings 读取 TTS Provider ID"""
        try:
            ctx = getattr(self, "_context", None)
            if ctx is None:
                return ""
            cfg = ctx.get_config(self._default_umo or "")
            tts = dict(cfg.get("provider_tts_settings", {}) or {})
            if key == "tts_provider_id":
                return str(tts.get("provider_id") or "")
        except Exception:
            pass
        return ""

    def _resolve_any_real_provider(self) -> Optional[TTSProvider]:
        """从所有可用路径查找任意一个非自身的 TTS Provider"""
        pm = self._get_provider_manager()

        # _real_providers
        for p in self._real_providers:
            if p is not self:
                return p

        # ProviderManager.tts_provider_insts
        if pm is not None:
            for p in pm.tts_provider_insts:
                if p is not self:
                    return p
            # ProviderManager.inst_map（排除自身）
            for pid, p in list(pm.inst_map.items()):
                if p is not self and isinstance(p, TTSProvider):
                    return p
        return None

    def _get_provider_manager(self):
        """获取 ProviderManager

        优先顺序：
        1. 实例级 _provider_manager（main.py 注入时传入）
        2. 实例级 _context.provider_manager（实时查找）
        3. 类级 _global_provider_manager（load_provider 创建的实例兜底）
        """
        if self._provider_manager is not None:
            return self._provider_manager
        try:
            ctx = getattr(self, "_context", None)
            if ctx is not None:
                pm = getattr(ctx, "provider_manager", None)
                if pm is not None:
                    return pm
        except Exception:
            pass
        if LingYinTTSProvider._global_provider_manager is not None:
            return LingYinTTSProvider._global_provider_manager
        return None

    def _find_provider_by_id(self, provider_id: str) -> Optional[TTSProvider]:
        """按 ID 查找 Provider 实例

        查找顺序：_real_providers → inst_map → tts_provider_insts
        """
        # _real_providers
        for p in self._real_providers:
            try:
                if p is not self and p.meta().id == provider_id:
                    return p
            except Exception:
                continue
        # inst_map
        try:
            mgr = getattr(self, "_provider_manager", None)
            if mgr is not None:
                inst = mgr.inst_map.get(provider_id)
                if inst is not None and inst is not self and isinstance(inst, TTSProvider):
                    return inst
        except Exception:
            pass
        # tts_provider_insts
        try:
            mgr = getattr(self, "_provider_manager", None)
            if mgr is not None:
                for p in mgr.tts_provider_insts:
                    if p is not self and p.meta().id == provider_id:
                        return p
        except Exception:
            pass
        return None

    # ── 兜底增强 ──────────────────────────────────────────────

    def _fallback_sanitize(self, text: str) -> str:
        """Provider 兜底清洗

        当事件钩子没处理到时直接调 get_audio，本方法做轻量清洗。
        使用 Sanitizer 的 fallback_clean 方法（不涉及情感标签）。
        """
        if not text:
            return ""
        try:
            from ..backend.sanitizer import Sanitizer
            return Sanitizer().fallback_clean(text)
        except Exception:
            return text

    def _fallback_emotion(self, text: str) -> str:
        """Provider 兜底情感标记

        只做韵律降级（对所有 Provider 生效，不需要原生情感标签支持）。
        """
        if not text:
            return ""
        try:
            from ..backend.emotion import EmotionEngine
            # 不带 tone_tag → 触发 keyword 检测 + 韵律降级
            return EmotionEngine().apply(text, provider_kind="generic")
        except Exception:
            return text
