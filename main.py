"""聆音 — AstrBot TTS 编排插件。支持多 Provider 降级、三级权限、双层密度、长文本分段合并。"""
import logging
import os
import random
from multiprocessing import Process

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import ProviderType, ProviderMetaData
from astrbot.core.provider.register import provider_cls_map, provider_registry

from ._config import _CFG_MAP

from .backend.permissions import PERM_BASIC, PERM_LABELS, PERM_RESTRICTED, PERM_UNLIMITED
from .backend.tts_handler import TtsHandler
from .backend.injector import Injector
from .backend.sanitizer import Sanitizer
from .backend.language import LanguageEngine
from .backend.emotion import EmotionEngine
from .backend.frequency import FrequencyGate
from .providers.lingyin_provider import LingYinTTSProvider, PROVIDER_TYPE, PROVIDER_DESC
from .bridge.pc_bridge import PCBridge
from .backend.pipeline import TtsPipeline
from .backend.tag_parser import TagParser
from .webui import run_server

# ── WebUI 常量 ──────────────────────────────────────────────
PLUGIN_NAME = "astrbot_plugin_voice_assistant"

SENSITIVE_FIELDS = {
    "cloud_s3_secret_key", "cloud_webdav_passwd",
    "cloud_smb_passwd", "cloud_custom_headers",
}

# ── 嵌套配置分组 → 允许的字段 ──────────────────────────────────
ALLOWED_GROUPS = {
    "basic_settings": {
        "basic_voice_enabled", "basic_tts_provider_id", "basic_tts_fallback_id",
        "basic_tts_by_session", "basic_voice_routing", "basic_install_as_provider",
    },
    "sending_effects": {
        "send_form", "send_language", "send_foreign_text_display",
        "send_tts_scope", "send_generation_method", "send_tts_text_model",
        "send_tts_extra_prompt", "send_llm_behavior_inject",
    },
    "trigger_probability": {
        "trigger_group_probability",
        "trigger_private_probability", "trigger_force_probability",
        "trigger_density_window", "trigger_density_max_count",
        "trigger_user_density_window", "trigger_user_threshold",
        "trigger_curve_steepness", "trigger_session_interval",
        "trigger_user_interval",
        "trigger_default_permission", "trigger_session_overrides",
    },
    "text_processing": {
        "text_min_length", "text_max_length", "text_segment_max_chars",
        "text_segment_delay", "text_retry_max_attempts",
        "text_merge_enabled", "text_merge_target_duration", "text_merge_timeout",
    },
    "post_processing": {
        "post_compatible_tags",
        "post_auto_convert",
    },
    "backup_settings": {
        "backup_chat_id", "backup_local_enabled",
        "backup_local_dir", "backup_local_retention_days",
        "backup_cloud_enabled", "backup_cloud_backend",
    },
    "cloud_storage": {
        "cloud_custom_url", "cloud_custom_headers", "cloud_custom_body",
        "cloud_custom_result_path",
        "cloud_s3_endpoint", "cloud_s3_region", "cloud_s3_bucket",
        "cloud_s3_access_key", "cloud_s3_secret_key", "cloud_s3_path_style",
        "cloud_webdav_url", "cloud_webdav_username", "cloud_webdav_passwd",
        "cloud_smb_share", "cloud_smb_username", "cloud_smb_passwd", "cloud_smb_domain",
    },
    "misc_settings": {
        "log_level", "webui_enabled", "webui_port", "astrbot_server_url",
    },
}

NUMERIC_FIELDS = {
    "trigger_density_window", "trigger_density_max_count",
    "trigger_user_density_window", "trigger_user_threshold",
    "trigger_curve_steepness",
    "trigger_session_interval", "trigger_user_interval",
    "trigger_group_probability", "trigger_private_probability",
    "text_min_length", "text_max_length", "text_segment_max_chars",
    "text_segment_delay", "text_retry_max_attempts",
    "text_merge_target_duration", "text_merge_timeout",
    "backup_local_retention_days", "webui_port",
}

CONFIG_FLOAT_FIELDS = {
    "trigger_group_probability", "trigger_private_probability",
    "trigger_curve_steepness", "text_segment_delay",
}

PLUGIN_CONFIG_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "..", "data", "config",
    "astrbot_plugin_voice_assistant.json",
))

# ── 扁平旧配置 → 嵌套新配置 迁移映射 ──────────────────────────
# 每个条目: (旧键, 新组, 新键)
# 特例: send_text_with_voice → sending_effects.send_form (bool→string)
FLAT_TO_NESTED_MAP: list[tuple[str, str, str]] = [
    ("voice_enabled", "basic_settings", "basic_voice_enabled"),
    ("tts_provider_id", "basic_settings", "basic_tts_provider_id"),
    ("tts_fallback_provider_id", "basic_settings", "basic_tts_fallback_id"),
    ("voice_routing", "basic_settings", "basic_voice_routing"),
    ("send_text_with_voice", "sending_effects", "send_form"),
    ("tts_voice_language", "sending_effects", "send_language"),
    ("voice_prompt_extra", "sending_effects", "send_llm_behavior_inject"),
    ("rate_limit_seconds", "trigger_probability", "trigger_session_interval"),
    ("density_window_minutes", "trigger_probability", "trigger_density_window"),
    ("density_max_count", "trigger_probability", "trigger_density_max_count"),
    ("user_density_window_minutes", "trigger_probability", "trigger_user_density_window"),
    ("user_density_threshold", "trigger_probability", "trigger_user_threshold"),
    ("user_density_curve_steepness", "trigger_probability", "trigger_curve_steepness"),
    ("default_permission_level", "trigger_probability", "trigger_default_permission"),
    ("session_permissions", "trigger_probability", "trigger_session_overrides"),
    ("min_text_length", "text_processing", "text_min_length"),
    ("max_text_length", "text_processing", "text_max_length"),
    ("tts_segment_max_chars", "text_processing", "text_segment_max_chars"),
    ("tts_inter_segment_delay", "text_processing", "text_segment_delay"),
    ("tts_retry_max_attempts", "text_processing", "text_retry_max_attempts"),
    ("tts_merge_enabled", "text_processing", "text_merge_enabled"),
    ("tts_merge_timeout_seconds", "text_processing", "text_merge_timeout"),
    ("backup_session_id", "backup_settings", "backup_chat_id"),
    ("local_audio_dir", "backup_settings", "backup_local_dir"),
    ("local_audio_retention_days", "backup_settings", "backup_local_retention_days"),
    ("cloud_backend", "backup_settings", "backup_cloud_backend"),
    ("cloud_backup_enabled", "backup_settings", "backup_cloud_enabled"),
]


def _migrate_config(config: dict) -> dict:
    """旧扁平配置 → 新嵌套配置。已有嵌套结构则跳过。"""
    NESTED_GROUPS = {"basic_settings", "sending_effects", "trigger_probability",
                     "text_processing", "post_processing", "backup_settings",
                     "cloud_storage", "misc_settings"}
    if any(g in config for g in NESTED_GROUPS):
        return config

    migrated: dict = {}
    for old_key, group, new_key in FLAT_TO_NESTED_MAP:
        if old_key not in config:
            continue
        value = config[old_key]
        if old_key == "send_text_with_voice":
            value = "text_and_voice" if value else "voice_only"
        nested = migrated.setdefault(group, {})
        nested[new_key] = value

    # cloud_* 字段：保留原名，归入 cloud_storage 组
    for k, v in config.items():
        if k.startswith("cloud_"):
            nested = migrated.setdefault("cloud_storage", {})
            nested[k] = v

    # misc 字段
    for misc_key in ("log_level", "webui_enabled", "webui_port", "astrbot_server_url"):
        if misc_key in config:
            nested = migrated.setdefault("misc_settings", {})
            nested[misc_key] = config[misc_key]

    # 已存在的新嵌套键优先
    for k, v in config.items():
        if k in {e[0] for e in FLAT_TO_NESTED_MAP} or k.startswith("cloud_") or \
           k in ("log_level", "webui_enabled", "webui_port", "astrbot_server_url"):
            continue
        if isinstance(v, dict):
            # 可能已经是新嵌套结构
            migrated[k] = v

    return migrated


class Main(Star):
    """聆音 — 让 AI 主动调用 TTS 回复语音"""

    def __init__(self, context, config: dict = None):
        super().__init__(context)
        self.config = _migrate_config(config or {})

        # 根据配置设置日志级别
        level_map = {"debug": logging.DEBUG, "info": logging.INFO}
        log_level_str = self._c("log_level", "info")
        logger.setLevel(level_map.get(log_level_str, logging.INFO))

        # ── PC 共存桥接 ──────────────────────────────────────
        self._voice_routing = self._c("basic_voice_routing", "auto")
        self._pc_bridge = PCBridge(self)
        self._bridge_status = self._pc_bridge.detect(voice_routing=self._voice_routing)

        # 注册 TTS Provider
        if self._c("basic_install_as_provider", True):
            self._register_tts_provider()
            self._set_default_tts()
        else:
            logger.info("[聆音] 未注册为 TTS Provider（basic_install_as_provider=false）")

        # ── 管线 & 引擎 ──────────────────────────────────────
        self._injector = Injector(
            target_language=self._c("send_language", "auto")
        )
        self._sanitizer = Sanitizer()
        self._language_engine = LanguageEngine()
        self._emotion_engine = EmotionEngine()
        self._tag_parser = TagParser()

        # TTS 编排处理器（持有密度、权限、归档等子模块）
        self.tts = TtsHandler(context, self.config, persist_callback=self._persist_config)

        self._frequency_gate = FrequencyGate(self.config, density_controller=self.tts.density)
        self._pipeline = TtsPipeline(
            self,
            sanitizer=self._sanitizer,
            emotion=self._emotion_engine,
            language=self._language_engine,
            tag_parser=self._tag_parser,
        )
        self._tts_tone = ""
        self._enable_event_hooks = PCBridge.should_enable_hooks(
            self._voice_routing,
            self._bridge_status.get("tts_enabled", False),
        )

        # 独立 WebUI 服务器
        self._webui_process: Process | None = None
        self._start_webui_server()

        self._register_webui()

        self._log_available_tts_providers()
        logger.info(
            f"聆音 已加载 "
            f"(enabled={self._c('voice_enabled', True)}, "
            f"log_level={log_level_str}, "
            f"bridge={self._pc_bridge.description(self._voice_routing)})"
        )

    # ── TTS Provider 注册 ─────────────────────────────────────

    def _register_tts_provider(self):
        """注册聆音为 AstrBot TTS Provider

        AstrBot 的 Provider 机制要求 Provider 实例必须通过
        ProviderManager.load_provider() 加载，但该方法只处理
        cmd_config.json 中的 provider 列表。插件注册的 Provider
        需要手动将实例注入 ProviderManager 的内部结构。

        完成后：
        - 其他插件调 context.get_using_tts_provider(umo) 能获取到聆音实例
        - context.get_all_tts_providers() 能列出聆音
        """
        try:
            # Step 1: 注册 class 到 provider_cls_map（框架发现用）
            metadata = ProviderMetaData(
                id="default",
                model=None,
                type=PROVIDER_TYPE,
                provider_type=ProviderType.TEXT_TO_SPEECH,
                desc=PROVIDER_DESC,
                cls_type=LingYinTTSProvider,
            )
            if PROVIDER_TYPE in provider_cls_map:
                provider_cls_map[PROVIDER_TYPE] = metadata
            else:
                provider_cls_map[PROVIDER_TYPE] = metadata
                provider_registry.append(metadata)

            # Step 2: 创建实例并注入 ProviderManager（不依赖 cmd_config.json）
            provider_mgr = getattr(self.context, "provider_manager", None)
            if provider_mgr is None:
                logger.warning("[聆音] 无法获取 ProviderManager，跳过实例注入")
                return

            lingyin_id = f"tts-{PROVIDER_TYPE}"

            # 设置类级全局引用（供 load_provider() 创建的无上下文实例兜底）
            LingYinTTSProvider.set_global_context(self.context, provider_mgr)

            if lingyin_id not in provider_mgr.inst_map:
                # 构造 provider 配置（与框架期望的格式一致）
                prov_config = {
                    "id": lingyin_id,
                    "type": PROVIDER_TYPE,
                    "provider_type": "text_to_speech",
                    "enable": True,
                    # 允许用户配置真实 TTS 引擎的字段
                    "tts_provider_id": self._c("basic_tts_provider_id", ""),
                    "tts_fallback_provider_id": self._c("basic_tts_fallback_id", ""),
                }
                instance = LingYinTTSProvider(
                    prov_config, {},
                    context=self.context,
                    provider_manager=provider_mgr,
                )

                # 注入到 ProviderManager
                provider_mgr.inst_map[lingyin_id] = instance
                provider_mgr.tts_provider_insts.append(instance)
                if provider_mgr.curr_tts_provider_inst is None:
                    provider_mgr.curr_tts_provider_inst = instance

                logger.info(
                    f"[聆音] TTS Provider '{PROVIDER_TYPE}' 已注册并实例化 "
                    f"(id={lingyin_id})"
                )
            else:
                logger.info(f"[聆音] TTS Provider '{lingyin_id}' 已存在，跳过")

            # Step 3: 写入 cmd_config.json（让 WebUI Provider 列表能看到聆音）
            self._ensure_cmd_config_entry()

            # Step 4: 设为默认（仅首次安装时）
            self._set_default_tts(lingyin_id)

        except Exception as e:
            logger.warning(f"[聆音] Provider 注册失败（非致命）: {e}")

    def _ensure_cmd_config_entry(self):
        """将聆音的 provider 配置写入 cmd_config.json

        这样 AstrBot WebUI 的 Provider 设置下拉列表能显示聆音。
        load_provider() 会因此创建一个额外的 LingYinTTSProvider 实例，
        但该类已通过 set_global_context 设置了类级兜底引用。
        """
        try:
            config_path = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "..", "..", "data", "cmd_config.json",
            ))
            if not os.path.exists(config_path):
                return
            import json as _json
            with open(config_path, encoding="utf-8-sig") as f:
                cfg = _json.load(f)
            provider_list = cfg.get("provider", [])
            for p in provider_list:
                if p.get("type") == PROVIDER_TYPE:
                    return  # 已存在
            default_config = {
                "type": PROVIDER_TYPE,
                "id": f"tts-{PROVIDER_TYPE}",
                "provider_type": "text_to_speech",
                "enable": True,
            }
            cfg.setdefault("provider", []).append(default_config)
            with open(config_path, "w", encoding="utf-8") as f:
                _json.dump(cfg, f, ensure_ascii=False, indent=2)
            logger.info(f"[聆音] 已添加 TTS 配置到 cmd_config.json (type={PROVIDER_TYPE})")
        except Exception as e:
            logger.debug(f"[聆音] 写入 cmd_config.json 失败（非致命）: {e}")

    def _set_default_tts(self, provider_id: str = ""):
        """安装时强制设为默认 TTS Provider"""
        try:
            astrbot_config = getattr(self.context, "get_config", lambda: {})()
            if not astrbot_config:
                return
            tts_settings = astrbot_config.setdefault("provider_tts_settings", {})
            if not getattr(self, "_lingyin_installed", False):
                pid = provider_id or f"tts-{PROVIDER_TYPE}"
                tts_settings["provider_id"] = pid
                tts_settings["enable"] = True
                # 禁用 AstrBot 原生自动 TTS（由聆音管线自行决策频率）
                tts_settings["trigger_probability"] = 0
                self._lingyin_installed = True
                logger.info(f"[聆音] TTS 默认 Provider 已设为 {pid}")
        except Exception as e:
            logger.debug(f"[聆音] 设置默认 TTS Provider 失败（非致命）: {e}")

    # ── 独立 WebUI 服务器 ──────────────────────────────────────────

    def _start_webui_server(self):
        """启动独立 WebUI 面板服务器（子进程）。"""
        if not self._c("webui_enabled", True):
            return
        port = self._c("webui_port", 11180)
        astrbot_url = self._c("astrbot_server_url", "http://localhost:6185")
        try:
            self._webui_process = Process(target=run_server, args=(port, astrbot_url))
            self._webui_process.start()
            logger.info(f"聆音 WebUI 面板已启动: http://localhost:{port} (代理 → {astrbot_url})")
        except Exception as e:
            logger.warning(f"聆音 WebUI 面板启动失败（非致命）: {e}", exc_info=True)

    def _stop_webui_server(self):
        """停止独立 WebUI 服务器。"""
        if self._webui_process and self._webui_process.is_alive():
            try:
                self._webui_process.terminate()
                self._webui_process.join(timeout=5)
            except Exception:
                pass
        self._webui_process = None

    # ── 生命周期 ───────────────────────────────────────────────

    async def terminate(self):
        """插件卸载时清理资源。"""
        self._stop_webui_server()
        self.tts.cleanup_temp_files()
        logger.info("聆音 已卸载")

    # ── Provider 发现 ──────────────────────────────────────────

    def _log_available_tts_providers(self, force: bool = False):
        """打印所有已注册 TTS Provider（仅第一次成功时输出）。"""
        if getattr(self, "_providers_logged", False) and not force:
            return
        try:
            providers = self.context.get_all_tts_providers()
        except Exception as e:
            logger.debug(f"获取 TTS Provider 列表失败（可能尚未初始化）: {e}", exc_info=True)
            return
        if not providers:
            return
        logger.info(f"聆音: 发现 {len(providers)} 个 TTS Provider:")
        for p in providers:
            try:
                meta = p.meta()
                logger.info(f"  · id={meta.id}  type={meta.type}  model={meta.model or 'N/A'}")
            except Exception:
                logger.info(f"  · (无法获取元数据的 Provider: {type(p).__name__})", exc_info=True)
        self._providers_logged = True

    @staticmethod
    def _is_group_event(event) -> bool:
        """判断事件是否为群聊消息"""
        try:
            from astrbot.core.platform.message_type import MessageType
            msg_type = getattr(event, "session", None)
            if msg_type is not None:
                mt = getattr(msg_type, "message_type", None)
                return mt is MessageType.GROUP_MESSAGE
        except Exception:
            pass
        return False

    # ── WebUI 注册 ──────────────────────────────────────────────

    def _register_webui(self):
        """注册 WebUI API 端点到 AstrBot。"""
        try:
            from .api.handlers import register_webui_handlers
            register_webui_handlers(self.context, self)
        except Exception as e:
            logger.warning(f"WebUI API 注册失败（非致命）: {e}")

    # ── LLM 请求注入（密度提醒 + extra prompt + 语音规则）────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """注入语音规则 + 概率决策行。规则模板固定（缓存命中），决策行 15 字随概率变化。"""

        # 语音规则（始终注入，模板固定 → 缓存命中的）
        if self._enable_event_hooks:
            inject_text = self._injector.inject(
                str(getattr(req, "system_prompt", "") or ""),
                target_language=self._c("send_language", "auto"),
            )
            if inject_text:
                req.system_prompt = (
                    f"{getattr(req, 'system_prompt', '') or ''}{inject_text}"
                ).strip()

        # extra prompt（不受概率影响，配置稳定则缓存命中）
        extra = self._c("send_llm_behavior_inject", "")
        if extra:
            req.system_prompt += f"\n\n[语音行为规则]\n{extra}"

        # 概率决策行（15 字，追加在末尾，不影响前缀缓存）
        voice_round = self._c("trigger_force_probability", False)
        if not voice_round:
            is_group = self._is_group_event(event)
            prob_key = "trigger_group_probability" if is_group else "trigger_private_probability"
            prob = float(self._c(prob_key, 1.0))
            voice_round = prob <= 0 or prob >= 1.0 or random.random() < prob
        req.system_prompt += "\n" + self._injector.build_decision_line(voice_round)

    # ── LLM 响应归一化（TTS 标签保护）────────────────────────

    @filter.on_llm_response()
    async def on_llm_response_normalize(self, event: AstrMessageEvent, resp, *args, **kwargs):
        """保护 TTS 标签不被框架拆分，归一化标签格式。

        同时做语言质量校验（layer 2）：检查 <lingyin> 内的文本
        是否像目标语种，若含大量中文但目标是日语则触发 LLM 翻译。
        """
        if not self._enable_event_hooks:
            return
        text = str(getattr(resp, "completion_text", "") or "")
        if not text:
            return
        # 归一化标签格式：<pc_tts>/<tts> → <lingyin>
        extra_tags = self._c("post_compatible_tags", []) or []
        normalized = self._tag_parser.normalize(text, extra_tags=extra_tags)
        if normalized == text:
            return  # 无 TTS 标签，无需处理

        # 提取情感标注 [tone:xxx]
        tone, clean_text = self._tag_parser.extract_tone(normalized)
        if tone:
            self._tts_tone = tone  # 暂存，供 pipeline 使用

        # Layer 2: 语言质量校验 + 可见文本补充
        target_lang = self._c("send_language", "auto")
        if target_lang not in ("", "auto", "zh"):
            segments = self._tag_parser.parse(clean_text)
            for seg in segments:
                if not seg.lingyin_text:
                    continue
                passed, suggestion = self._language_engine.check_quality(
                    seg.lingyin_text, target_lang
                )
                if not passed:
                    # 质量不达标 → 尝试 LLM 翻译补充
                    logger.info(
                        f"[聆音] 语言质量校验未通过: {suggestion}, "
                        f"尝试 LLM 翻译兜底"
                    )
                    # 将原文替换为翻译结果（异步触发，不能阻塞 on_llm_response）
                    # 此处仅记录标记，翻译由 pipeline 的 language.convert 兜底
                    try:
                        setattr(event, "_lingyin_needs_translate", True)
                    except Exception:
                        pass

        # 用 Token 保护 <lingyin> 块，防止框架拆分
        protected_text, tokens = self._tag_parser.protect_blocks(clean_text)
        if tokens:
            try:
                setattr(event, "_lingyin_tts_tokens", tokens)
            except Exception:
                pass

        resp.completion_text = protected_text
        if normalized != text:
            logger.info(
                f"[聆音] TTS 标签已归一化并保护: session={getattr(event, 'unified_msg_origin', '')}"
            )

    # ── 发送前编排（TTS 标签 → Record）───────────────────────

    @filter.on_decorating_result(priority=20000)
    async def on_decorating_result_pipeline(self, event: AstrMessageEvent, *args, **kwargs):
        """发送前检查文本并处理语音标签。

        核心管道：接收到 LLM 生成的文本后，检查是否含有 TTS 标签，
        若有则走聆音增强管线（清洗→情感→语言→合成→Record）。
        """
        umo = str(getattr(event, "unified_msg_origin", ""))
        # 模式门控
        if not self._enable_event_hooks:
            logger.debug(f"[聆音] TTS决策|跳过|路由模式非聆音 umo={umo}")
            return
        # 防重复处理
        if getattr(event, "_lingyin_voice_done", False):
            logger.debug(f"[聆音] TTS决策|跳过|已处理 umo={umo}")
            return
        if getattr(event, "_private_companion_skip_tts_enhancement", False):
            logger.debug(f"[聆音] TTS决策|跳过|PC已处理 umo={umo}")
            return
        # 全局开关
        if not self._c("basic_voice_enabled", True):
            logger.debug(f"[聆音] TTS决策|跳过|voice_enabled=false umo={umo}")
            return

        result = event.get_result()
        chain = list(getattr(result, "chain", []) or []) if result is not None else []
        if not chain:
            logger.debug(f"[聆音] TTS决策|跳过|空链 umo={umo}")
            return
        # 如果链中已有 Record（ai_speak 已处理），跳过
        if any(isinstance(comp, Record) for comp in chain):
            try:
                setattr(event, "_lingyin_voice_done", True)
            except Exception:
                pass
            logger.debug(f"[聆音] TTS决策|跳过|已有Record(ai_speak) umo={umo}")
            return

        # 提取文本
        plain_parts = [
            str(getattr(comp, "text", "") or "")
            for comp in chain if isinstance(comp, Plain)
        ]
        if not plain_parts:
            logger.debug(f"[聆音] TTS决策|跳过|无Plain组件 umo={umo}")
            return
        text = "".join(plain_parts).strip()
        if not text:
            logger.debug(f"[聆音] TTS决策|跳过|空文本 umo={umo}")
            return

        # 检查是否有 TTS 标签或 Token 保护的块
        has_lingyin = "<lingyin>" in text.lower() or "<tts>" in text.lower()
        has_tokens = "[[LINGYIN:" in text
        if not has_lingyin and not has_tokens:
            # 强制概率模式：无标签时强制包裹 <lingyin>
            if self._c("trigger_force_probability", False):
                is_group = self._is_group_event(event)
                prob_key = "trigger_group_probability" if is_group else "trigger_private_probability"
                prob = float(self._c(prob_key, 1.0))
                if prob <= 0 or prob >= 1.0 or random.random() < prob:
                    text = f"<lingyin>{text}</lingyin>"
                    has_lingyin = True
                    logger.info(f"[聆音] TTS决策|强制概率|包裹全文为语音 umo={umo}")
            if not has_lingyin:
                logger.debug(f"[聆音] TTS决策|跳过|无TTS标签 umo={umo}")
                return  # 无标签，不处理

        # 恢复被 Token 保护的块（on_llm_response 阶段保护的标签）
        tokens = getattr(event, "_lingyin_tts_tokens", None)
        if isinstance(tokens, dict) and tokens:
            text = self._tag_parser.restore_blocks(text, tokens)

        logger.info(
            f"[聆音] TTS决策|处理|检测到TTS标签 umo={umo} "
            f"text={text[:80]}..."
        )

        from .backend.pipeline import TtsContext
        tts_lang = self._c("send_language", "auto")
        ctx = TtsContext(
            segments=self._tag_parser.parse(text),
            event=event,
            target_language=tts_lang if tts_lang not in ("", "auto") else "",
            foreign_text_display=self._c("send_foreign_text_display", "foreign_first"),
            tone_tag=getattr(self, "_tts_tone", ""),
            fallback_plain=self._tag_parser.strip_any_tts_markup(text),
        )

        # (Phase 2: 注入真实 Provider、语言、情感等上下文)
        try:
            provider = self.context.get_using_tts_provider(
                str(getattr(event, "unified_msg_origin", "") or "")
            )
            if provider is not None:
                ctx.provider = provider
        except Exception:
            pass

        # ===== 频率门控（工具路径与非工具路径统一） =====
        session_id = str(getattr(event, "session", ""))
        user_id = str(getattr(event, "get_sender_id", lambda: "")())
        gate = self._frequency_gate.check(
            event,
            via_tool=False,
            session_id=session_id,
            user_id=user_id,
        )
        if not gate.allowed:
            logger.info(
                f"[聆音] 频率门控拒绝|{gate.reason} "
                f"umo={umo} session={session_id}"
            )
            fallback = self._tag_parser.strip_any_tts_markup(text)
            if fallback:
                res = event.get_result()
                if res is not None:
                    res.chain = [Plain(fallback)]
            return

        logger.info(f"[聆音] 管线开始|text_len={len(text)} segments={len(ctx.segments)}")

        # 执行管线
        components = await self._pipeline.execute(ctx)
        if not components:
            logger.info(f"[聆音] 管线结果|无输出 umo={umo}")
            return

        has_voice = any(isinstance(c, Record) for c in components)
        logger.info(
            f"[聆音] 管线结果|components={len(components)} "
            f"has_voice={has_voice} umo={umo}"
        )

        res = event.get_result()
        if res is not None:
            res.chain = components
        try:
            setattr(event, "_lingyin_voice_done", True)
        except Exception:
            pass

        # 通知频率门控记录本次发送
        try:
            self._frequency_gate.mark_sent(event, session_id=session_id)
        except Exception:
            pass

        # ===== 非工具路径也调用备份 =====
        if has_voice:
            try:
                audio_paths = []
                fallback_text = ctx.fallback_plain or text
                for c in components:
                    if isinstance(c, Record):
                        fp = str(getattr(c, "file", "") or getattr(c, "url", ""))
                        if fp:
                            audio_paths.append(fp)
                if audio_paths:
                    await self.tts._send_backup(
                        text=fallback_text,
                        final_audio=audio_paths[0],
                        segments=[],
                        audio_paths=audio_paths,
                        event=event,
                    )
            except Exception as exc:
                logger.warning(f"[聆音] 备份发送异常（非致命）: {exc}")

    # ── LLM 工具 — ai_speak ────────────────────────────────────

    @filter.llm_tool(name="ai_speak")
    async def ai_speak(self, event: AstrMessageEvent, text: str):
        """把文字转成语音回复用户。当你觉得这段话用语音说更自然时调用。

        调用示例：
          ai_speak(text="好的，我马上处理！")

        Args:
            text: 想说出的文本，不要加特殊标记
        """
        logger.info(
            f"[聆音] TTS决策|ai_speak工具调用 umo={event.unified_msg_origin} "
            f"text={text[:60]}..."
        )
        result = await self.tts.speak(event, text)
        try:
            setattr(event, "_lingyin_voice_done", True)
        except Exception:
            pass
        return result

    # ── 指令 — /ly（品牌命令） ──────────────────────────────

    @filter.command("ly")
    async def cmd_ly(self, event: AstrMessageEvent):
        """聆音管理命令。

        用法:
          /ly perm set <session> <0|1|2> [prob]  — 设置权限等级+可选概率
          /ly perm get [session]                 — 查询权限
          /ly perm list                          — 列出所有自定义权限
          /ly perm del <session>                 — 删除自定义权限
          /ly engine set <session> <id>          — 设置会话引擎
          /ly engine get [session]               — 查询会话引擎
          /ly engine list                        — 列出所有会话引擎
          /ly prob set <session> <0~1>           — 设置会话概率覆盖
          /ly prob get [session]                 — 查询会话概率
          /ly status                             — 显示当前状态
          /ly on                                 — 启用语音
          /ly off                                — 禁用语音
          /ly route <auto|lingyin|other>         — 设置路由模式
        """
        if not event.is_admin():
            await event.send(MessageChain([Plain("权限不足：仅管理员可执行此操作")]))
            return

        raw = event.get_message_str()
        parts = raw.strip().split()
        if len(parts) < 2:
            await event.send(MessageChain([Plain(
                "聆音管理命令\n\n"
                "权限: /ly perm set|get|list|del\n"
                "引擎: /ly engine set|get|list\n"
                "概率: /ly prob set|get\n"
                "状态: /ly status\n"
                "开关: /ly on|off\n"
                "路由: /ly route <auto|lingyin|other>\n\n"
                "用 /ly <子命令> help 查看详细用法"
            )]))
            return

        sub = parts[1].lower()
        config = self.config

        # ── /ly perm ──────────────────────────────────────────
        if sub == "perm":
            return await self.cmd_voice_perm(event)

        # ── /ly engine ────────────────────────────────────────
        elif sub == "engine":
            if len(parts) < 3:
                await event.send(MessageChain([Plain(
                    "/ly engine set <session> <engine_id>\n"
                    "/ly engine get [session]\n"
                    "/ly engine list"
                )]))
                return
            action = parts[2].lower()
            if action == "list":
                entries = self._c("basic_tts_by_session", []) or []
                if not entries:
                    await event.send(MessageChain([Plain("暂无会话引擎指定")]))
                else:
                    lines = ["会话引擎指定:"]
                    for e in entries:
                        lines.append(f"  {e}")
                    await event.send(MessageChain([Plain("\n".join(lines))]))
                return
            if len(parts) < 4:
                await event.send(MessageChain([Plain("用法: /ly engine set <session> <engine_id>")]))
                return
            sid = parts[3]
            if action == "get":
                entries = self._c("basic_tts_by_session", []) or []
                found = [e for e in entries if e.startswith(f"{sid}:")]
                if found:
                    await event.send(MessageChain([Plain(f"会话 {sid}: {found[0]}")]))
                else:
                    await event.send(MessageChain([Plain(f"会话 {sid}: 使用默认引擎")]))
                return
            if action == "set" and len(parts) >= 5:
                eid = parts[4]
                entries = self._c("basic_tts_by_session", []) or []
                prefix = f"{sid}:"
                entries = [e for e in entries if not e.startswith(prefix)]
                entries.append(f"{sid}:{eid}")
                bs = config.setdefault("basic_settings", {})
                bs["basic_tts_by_session"] = entries
                self._persist_config()
                await event.send(MessageChain([Plain(f"已设置会话 {sid} 的引擎为 {eid}")]))
                return
            await event.send(MessageChain([Plain(f"未知操作: {action}")]))

        # ── /ly prob ─────────────────────────────────────────
        elif sub == "prob":
            if len(parts) < 3:
                await event.send(MessageChain([Plain(
                    "/ly prob set <session> <0~1>\n"
                    "/ly prob get [session]"
                )]))
                return
            action = parts[2].lower()
            if action == "get":
                target = parts[3] if len(parts) >= 4 else str(event.session)
                ov = self._c("trigger_session_overrides", []) or []
                found = [e for e in ov if e.startswith(f"{target}:")]
                if found:
                    await event.send(MessageChain([Plain(f"会话 {target}: {found[0]}")]))
                else:
                    await event.send(MessageChain([Plain(f"会话 {target}: 使用默认概率")]))
                return
            if action == "set" and len(parts) >= 5:
                target = parts[3]
                try:
                    prob_val = float(parts[4])
                    if prob_val < 0 or prob_val > 1:
                        raise ValueError
                except ValueError:
                    await event.send(MessageChain([Plain("概率值必须为 0~1 之间的数字")]))
                    return
                ov = self._c("trigger_session_overrides", []) or []
                prefix = f"{target}:"
                ov = [e for e in ov if not e.startswith(prefix)]
                ov.append(f"{target}:{self.perms.cache.get(target, 1)}:{prob_val}")
                tg = config.setdefault("trigger_probability", {})
                tg["trigger_session_overrides"] = ov
                self._persist_config()
                await event.send(MessageChain([Plain(f"已设置会话 {target} 的概率为 {prob_val}")]))
                return
            await event.send(MessageChain([Plain(f"未知操作: {action}")]))

        # ── /ly status ───────────────────────────────────────
        elif sub == "status":
            enabled = self._c("basic_voice_enabled", True)
            routing = self._c("basic_voice_routing", "auto")
            engine = self._c("basic_tts_provider_id", "") or "系统默认"
            await event.send(MessageChain([Plain(
                f"语音: {'已启用' if enabled else '已禁用'}\n"
                f"路由: {routing}\n"
                f"引擎: {engine}\n"
                f"钩子: {'已启用' if self._enable_event_hooks else '已禁用'}"
            )]))

        # ── /ly on / off ─────────────────────────────────────
        elif sub in ("on", "off"):
            enabled = sub == "on"
            bs = config.setdefault("basic_settings", {})
            bs["basic_voice_enabled"] = enabled
            self._persist_config()
            await event.send(MessageChain([Plain(f"语音已{'启用' if enabled else '禁用'}")]))

        # ── /ly route ────────────────────────────────────────
        elif sub == "route":
            if len(parts) < 3:
                await event.send(MessageChain([Plain("用法: /ly route <auto|lingyin|other>")]))
                return
            mode = parts[2].lower()
            if mode not in ("auto", "lingyin", "other"):
                await event.send(MessageChain([Plain("路由模式必须为 auto/lingyin/other")]))
                return
            bs = config.setdefault("basic_settings", {})
            bs["basic_voice_routing"] = mode
            self._persist_config()
            await event.send(MessageChain([Plain(f"路由模式已设为 {mode}，重启后生效")]))

        else:
            await event.send(MessageChain([Plain(f"未知子命令: {sub}\n用 /ly 查看帮助")]))

    # ── 指令 — /voice_perm ──────────────────────────────────

    @filter.command("voice_perm")
    async def cmd_voice_perm(self, event: AstrMessageEvent):
        """管理语音权限等级。

        用法:
          /voice_perm set <session_id> <0|1|2>  — 设置权限
          /voice_perm get [session_id]           — 查询权限
          /voice_perm list                       — 列出所有自定义权限
          /voice_perm help                       — 显示帮助
          /voice_perm del <session_id>           — 删除自定义权限
        """
        perms = self.tts.perms
        config = self.config

        if not event.is_admin():
            await event.send(MessageChain([Plain("权限不足：仅管理员可管理语音权限")]))
            return

        raw_msg = event.get_message_str()
        parts = raw_msg.strip().split()

        if len(parts) < 2:
            await event.send(MessageChain([Plain(
                "语音权限管理\n\n"
                "/voice_perm set <session_id> <0|1|2>\n"
                "/voice_perm get [session_id]\n"
                "/voice_perm list\n"
                "/voice_perm del <session_id>\n"
                "/voice_perm help\n\n"
                "用 /sid 获取当前会话 ID"
            )]))
            return

        action = parts[1].lower()

        if action == "help":
            await event.send(MessageChain([Plain(
                "语音权限管理\n\n"
                "/voice_perm set <session_id> <0|1|2>\n"
                "  0 = 无限制（不进行任何限制）\n"
                "  1 = 基准限制（速率+密度控制，默认）\n"
                "  2 = 完全限制（禁止语音，即黑名单）\n\n"
                "/voice_perm get [session_id]\n"
                "  查询会话的权限等级（不传=当前会话）\n\n"
                "/voice_perm list\n"
                "  列出所有自定义权限配置\n\n"
                "/voice_perm del <session_id>\n"
                "  删除自定义权限，恢复默认等级\n\n"
                "用 /sid 获取当前会话的完整 ID\n"
                "管理员的私聊会话默认为无限制等级"
            )]))
            return

        if action == "list":
            entries = config.get("trigger_session_overrides", []) or []
            if not entries:
                default_label = PERM_LABELS.get(config.get("trigger_default_permission", PERM_BASIC), "?")
                await event.send(MessageChain([Plain(
                    f"暂无自定义权限配置\n全部会话使用默认等级: {default_label}"
                )]))
            else:
                lines = ["自定义权限列表:"]
                for entry in sorted(entries):
                    entry = entry.strip()
                    if ':' in entry:
                        sid, lvl_str = entry.rsplit(":", 1)
                        try:
                            lvl = int(lvl_str)
                            label = PERM_LABELS.get(lvl, f"未知({lvl})")
                        except ValueError:
                            label = f"无效({lvl_str})"
                        lines.append(f"  {sid} -> {label}")
                default_label = PERM_LABELS.get(config.get("trigger_default_permission", PERM_BASIC), "?")
                lines.append(f"\n默认等级: {default_label}")
                await event.send(MessageChain([Plain("\n".join(lines))]))
            return

        if action == "get":
            target_sid = parts[2] if len(parts) >= 3 else str(event.session)
            level = perms.cache.get(target_sid)
            if level is None:
                level = config.get("trigger_default_permission", PERM_BASIC)
                source = "默认"
            else:
                source = "自定义"
            label = PERM_LABELS.get(level, f"未知({level})")
            await event.send(MessageChain([Plain(
                f"会话: {target_sid}\n等级: {label} (level={level})\n来源: {source}"
            )]))
            return

        if action == "set":
            if len(parts) < 4:
                await event.send(MessageChain([Plain("用法: /voice_perm set <session_id> <0|1|2>")]))
                return
            target_sid = parts[2]
            try:
                level = int(parts[3])
                if level not in (PERM_UNLIMITED, PERM_BASIC, PERM_RESTRICTED):
                    raise ValueError
            except ValueError:
                await event.send(MessageChain([Plain("等级必须为 0/1/2")]))
                return
            perms.set_level(target_sid, level)
            label = PERM_LABELS[level]
            await event.send(MessageChain([Plain(f"已设置: {target_sid} -> {label} (level={level})")]))
            logger.info(f"[voice_perm] 管理员设置权限: {target_sid} -> {label}")
            return

        if action == "del":
            if len(parts) < 3:
                await event.send(MessageChain([Plain("用法: /voice_perm del <session_id>")]))
                return
            target_sid = parts[2]
            perms.remove_level(target_sid)
            default_label = PERM_LABELS.get(config.get("trigger_default_permission", PERM_BASIC), "?")
            await event.send(MessageChain([Plain(
                f"已删除自定义权限: {target_sid}\n已恢复默认等级: {default_label}"
            )]))
            logger.info(f"[voice_perm] 管理员删除权限: {target_sid}")
            return

        await event.send(MessageChain([Plain(f"未知操作: {action}\n用法: /voice_perm set|get|list|del|help")]))

    # ── 嵌套配置访问（扁平键 → 自动路由到嵌套结构） ──────────

    _CFG_MAP = _CFG_MAP

    def _c(self, key, default=None):
        """读取嵌套配置（扁平键 → 自动路由到对应组）"""
        mapped = self._CFG_MAP.get(key)
        if mapped:
            return self.config.get(mapped[0], {}).get(mapped[1], default)
        return self.config.get(key, default)

    def _persist_config(self):
        """持久化配置到 JSON 文件。"""
        import json as _json, os
        try:
            config_path = PLUGIN_CONFIG_PATH
            config_dir = os.path.dirname(config_path)
            os.makedirs(config_dir, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                _json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[WebUI] 配置持久化失败（非致命）: {e}")
