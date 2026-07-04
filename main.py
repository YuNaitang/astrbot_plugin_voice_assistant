"""
AI Voice Assistant — AstrBot 通用 TTS 编排插件

允许AI 通过工具自主调用 TTS 回复语音。
支持多 Provider 降级、三级权限管理、双层密度控制、长文本分段合并。
"""
import os
import re

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .backend.permissions import PERM_BASIC, PERM_LABELS, PERM_RESTRICTED, PERM_UNLIMITED
from .backend.tts_handler import TtsHandler

# ── WebUI 常量 ──────────────────────────────────────────────
PLUGIN_NAME = "astrbot_plugin_voice_assistant"

SENSITIVE_FIELDS = {
    "cloud_s3_secret_key", "cloud_webdav_password",
    "cloud_smb_password", "cloud_custom_headers",
}

CONFIG_SAVE_ALLOWLIST = {
    "voice_enabled", "tts_provider_id", "tts_fallback_provider_id",
    "min_text_length", "max_text_length", "tts_segment_max_chars",
    "tts_inter_segment_delay", "tts_retry_max_attempts",
    "tts_merge_enabled", "tts_merge_timeout_seconds",
    "rate_limit_seconds", "density_window_minutes", "density_max_count",
    "user_density_window_minutes", "user_density_threshold",
    "user_density_curve_steepness",
    "default_permission_level", "session_permissions",
    "voice_prompt_extra",
    "local_audio_dir", "local_audio_retention_days",
    "cloud_backup_enabled", "cloud_backend",
    "cloud_custom_url", "cloud_custom_headers", "cloud_custom_body",
    "cloud_custom_result_path",
    "cloud_s3_endpoint", "cloud_s3_region", "cloud_s3_bucket",
    "cloud_s3_access_key", "cloud_s3_secret_key", "cloud_s3_path_style",
    "cloud_webdav_url", "cloud_webdav_username", "cloud_webdav_password",
    "cloud_smb_share", "cloud_smb_username", "cloud_smb_password", "cloud_smb_domain",
    "log_level", "send_text_with_voice", "backup_session_id",
}

PLUGIN_CONFIG_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "..", "data", "config",
    "astrbot_plugin_voice_assistant.json",
))

NUMERIC_FIELDS = {
    "min_text_length", "max_text_length", "tts_segment_max_chars",
    "tts_inter_segment_delay", "tts_retry_max_attempts",
    "tts_merge_timeout_seconds", "rate_limit_seconds",
    "density_window_minutes", "density_max_count",
    "user_density_window_minutes", "user_density_threshold",
    "user_density_curve_steepness", "default_permission_level",
    "local_audio_retention_days",
}


class Main(Star):
    """AI Voice Assistant — 让 AI 主动调用 TTS 回复语音"""

    def __init__(self, context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # TTS 编排处理器（持有所有子模块引用）
        self.tts = TtsHandler(context, self.config)

        self._register_webui()

        self._log_available_tts_providers()
        logger.info(
            f"AI Voice Assistant 已加载 "
            f"(enabled={self.config.get('voice_enabled', True)}, "
            f"log_level={self.config.get('log_level', 'info')})"
        )

    # ── 生命周期 ───────────────────────────────────────────────

    async def terminate(self):
        """插件卸载时清理临时音频文件。"""
        self.tts.cleanup_temp_files()
        logger.info("AI Voice Assistant 已卸载")

    # ── Provider 发现 ──────────────────────────────────────────

    def _log_available_tts_providers(self, force: bool = False):
        """打印所有已注册 TTS Provider（仅第一次成功时输出）。"""
        if getattr(self, "_providers_logged", False) and not force:
            return
        try:
            providers = self.context.get_all_tts_providers()
        except Exception as e:
            logger.debug(f"获取 TTS Provider 列表失败（可能尚未初始化）: {e}")
            return
        if not providers:
            return
        logger.info(f"AI Voice Assistant: 发现 {len(providers)} 个 TTS Provider:")
        for p in providers:
            try:
                meta = p.meta()
                logger.info(f"  · id={meta.id}  type={meta.type}  model={meta.model or 'N/A'}")
            except Exception:
                logger.info(f"  · (无法获取元数据的 Provider: {type(p).__name__})")
        self._providers_logged = True

    # ── MIME 检测 ─────────────────────────────────────────────────

    @staticmethod
    def _detect_audio_mime(file_path: str) -> str:
        """根据文件扩展名推断音频 MIME 类型。"""
        ext = os.path.splitext(file_path)[1].lower()
        return {
            ".wav": "audio/wav", ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg", ".aac": "audio/aac",
            ".flac": "audio/flac", ".wma": "audio/x-ms-wma",
        }.get(ext, "audio/wav")

    # ── WebUI 注册 ──────────────────────────────────────────────

    def _register_webui(self):
        """注册 WebUI API 端点到 AstrBot。"""
        try:
            prefix = f"/{PLUGIN_NAME}"
            self.context.register_web_api(
                f"{prefix}/get_config", self.handle_get_config, ["GET"], "读取配置"
            )
            self.context.register_web_api(
                f"{prefix}/save_config", self.handle_save_config, ["POST"], "保存配置"
            )
            self.context.register_web_api(
                f"{prefix}/get_status", self.handle_get_status, ["GET"], "运行时状态"
            )
            self.context.register_web_api(
                f"{prefix}/get_permissions", self.handle_get_permissions, ["GET"], "权限列表"
            )
            self.context.register_web_api(
                f"{prefix}/set_permission", self.handle_set_permission, ["POST"], "设置/删除权限"
            )
            self.context.register_web_api(
                f"{prefix}/get_density_stats", self.handle_get_density_stats, ["GET"], "密度统计数据"
            )
            self.context.register_web_api(
                f"{prefix}/get_archive_list", self.handle_get_archive_list, ["GET"], "归档文件列表"
            )
            self.context.register_web_api(
                f"{prefix}/get_archive_file", self.handle_get_archive_file, ["GET"], "获取归档文件"
            )
            self.context.register_web_api(
                f"{prefix}/delete_archive", self.handle_delete_archive, ["POST"], "删除归档文件"
            )
            self.context.register_web_api(
                f"{prefix}/test_tts", self.handle_test_tts, ["POST"], "TTS 测试合成"
            )
            self.context.register_web_api(
                f"{prefix}/get_tts_providers", self.handle_get_tts_providers, ["GET"], "TTS 提供商列表"
            )
            self.context.register_web_api(
                f"{prefix}/get_recent_sessions", self.handle_get_recent_sessions, ["GET"], "最近会话列表"
            )
            logger.info(f"AI Voice Assistant WebUI API 已注册（{prefix}）")
        except Exception as e:
            logger.warning(f"WebUI API 注册失败（非致命）: {e}")

    # ── LLM 请求注入（密度提醒 + extra prompt）──────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """每次 LLM 请求前，注入语音相关系统提示。"""
        extra = self.config.get("voice_prompt_extra", "")
        if extra:
            req.system_prompt += f"\n\n[语音行为规则]\n{extra}"

        session_id = str(event.session)
        if self.tts.density.is_over_density_limit(session_id):
            if not self.tts.density.is_warned(session_id):
                req.system_prompt += (
                    "\n\n[注意] 你最近已经发送了很多语音消息。"
                    "在收到重置通知之前，请不要再使用 ai_speak 工具。"
                )
                self.tts.density.mark_warned(session_id)

    # ── LLM 工具 — ai_speak ────────────────────────────────────

    @filter.llm_tool(name="ai_speak")
    async def ai_speak(self, event: AstrMessageEvent, text: str):
        """把文字转成语音回复用户。当你觉得这段话用语音说更自然时调用。
        系统会自动发送文字+语音，无需在回复中再写一遍。

        调用示例：
          ai_speak(text="好的，我马上处理！")  → 用户收到文字 + 语音
          ai_speak(text="爱你哟")              → 用户收到文字 + 语音

        注意：不适合念长文（>300字）、不适合发纯文字通知、不需要逐句调用。

        Args:
            text(string): 想说出的文本，口语化、自然，不要加标点符号外的特殊标记
        """
        return await self.tts.speak(event, text)

    # ── 指令 — /voice_perm ─────────────────────────────────────

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
            await event.send(MessageChain([Plain("❌ 权限不足：仅管理员可管理语音权限")]))
            return

        raw_msg = event.get_message_str()
        parts = raw_msg.strip().split()

        if len(parts) < 2:
            await event.send(MessageChain([Plain(
                "📋 语音权限管理\n\n"
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
                "📋 语音权限管理\n\n"
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
            entries = config.get("session_permissions", []) or []
            if not entries:
                default_label = PERM_LABELS.get(config.get("default_permission_level", PERM_BASIC), "?")
                await event.send(MessageChain([Plain(
                    f"📋 暂无自定义权限配置\n全部会话使用默认等级: {default_label}"
                )]))
            else:
                lines = ["📋 自定义权限列表:"]
                for entry in sorted(entries):
                    entry = entry.strip()
                    if ':' in entry:
                        sid, lvl_str = entry.rsplit(":", 1)
                        try:
                            lvl = int(lvl_str)
                            label = PERM_LABELS.get(lvl, f"未知({lvl})")
                        except ValueError:
                            label = f"无效({lvl_str})"
                        lines.append(f"  {sid} → {label}")
                default_label = PERM_LABELS.get(config.get("default_permission_level", PERM_BASIC), "?")
                lines.append(f"\n默认等级: {default_label}")
                await event.send(MessageChain([Plain("\n".join(lines))]))
            return

        if action == "get":
            target_sid = parts[2] if len(parts) >= 3 else str(event.session)
            level = perms.cache.get(target_sid)
            if level is None:
                level = config.get("default_permission_level", PERM_BASIC)
                source = "默认"
            else:
                source = "自定义"
            label = PERM_LABELS.get(level, f"未知({level})")
            await event.send(MessageChain([Plain(
                f"📋 会话: {target_sid}\n等级: {label} (level={level})\n来源: {source}"
            )]))
            return

        if action == "set":
            if len(parts) < 4:
                await event.send(MessageChain([Plain("❌ 用法: /voice_perm set <session_id> <0|1|2>")]))
                return
            target_sid = parts[2]
            try:
                level = int(parts[3])
                if level not in (PERM_UNLIMITED, PERM_BASIC, PERM_RESTRICTED):
                    raise ValueError
            except ValueError:
                await event.send(MessageChain([Plain("❌ 等级必须为 0/1/2")]))
                return
            perms.set_level(target_sid, level)
            label = PERM_LABELS[level]
            await event.send(MessageChain([Plain(f"✅ 已设置: {target_sid} → {label} (level={level})")]))
            logger.info(f"[voice_perm] 管理员设置权限: {target_sid} → {label}")
            return

        if action == "del":
            if len(parts) < 3:
                await event.send(MessageChain([Plain("❌ 用法: /voice_perm del <session_id>")]))
                return
            target_sid = parts[2]
            perms.remove_level(target_sid)
            default_label = PERM_LABELS.get(config.get("default_permission_level", PERM_BASIC), "?")
            await event.send(MessageChain([Plain(
                f"✅ 已删除自定义权限: {target_sid}\n已恢复默认等级: {default_label}"
            )]))
            logger.info(f"[voice_perm] 管理员删除权限: {target_sid}")
            return

        await event.send(MessageChain([Plain(f"❌ 未知操作: {action}\n用法: /voice_perm set|get|list|del|help")]))

    # ── WebUI Handlers ──────────────────────────────────────────

    async def handle_get_config(self):
        """读取完整配置，敏感字段脱敏。"""
        from quart import jsonify
        safe = {}
        for k, v in self.config.items():
            safe[k] = "***" if k in SENSITIVE_FIELDS else v
        return jsonify({"success": True, "config": safe})

    async def handle_save_config(self):
        """保存配置（白名单过滤 → 更新 → 持久化）。"""
        from quart import jsonify, request
        try:
            data = await request.get_json()
            updates = data.get("config", {})
            if not isinstance(updates, dict):
                return jsonify({"success": False, "error": "格式错误"})
            # 白名单过滤
            safe = {k: v for k, v in updates.items() if k in CONFIG_SAVE_ALLOWLIST}
            # 类型验证：数字字段进行类型强制转换
            for k in NUMERIC_FIELDS:
                if k in safe:
                    try:
                        safe[k] = float(safe[k]) if k in ("user_density_curve_steepness", "tts_inter_segment_delay") else int(safe[k])
                    except (ValueError, TypeError):
                        safe.pop(k, None)
            self.config.update(safe)
            # 尝试持久化
            self._persist_config()
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

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

    async def handle_get_status(self):
        """运行时状态摘要。"""
        from quart import jsonify
        from datetime import datetime
        # 今日调用次数
        today = datetime.now().date()
        today_count = sum(
            1 for r in self.tts._recent_calls
            if r.get("time", "").startswith(str(today))
        )
        # 活跃会话数
        active_sessions = len(self.tts.density._voice_timeline)
        # 密度状态
        is_limited = any(
            self.tts.density.is_over_density_limit(sid)
            for sid in self.tts.density._voice_timeline
        )
        # 权限统计
        perm_entries = self.config.get("session_permissions", []) or []
        return jsonify({
            "success": True,
            "status": {
                "voice_enabled": self.config.get("voice_enabled", True),
                "provider_id": self.config.get("tts_provider_id", "") or "系统默认",
                "today_count": today_count,
                "density_limited": is_limited,
                "active_sessions": active_sessions,
                "default_permission_level": self.config.get("default_permission_level", 1),
                "custom_permissions_count": len(perm_entries),
                "recent_calls": list(reversed(self.tts._recent_calls[-10:])),
            },
        })

    async def handle_get_permissions(self):
        """权限列表 + 默认等级。"""
        from quart import jsonify
        from .backend.permissions import PERM_LABELS
        entries = self.config.get("session_permissions", []) or []
        levels = []
        for entry in entries:
            entry = entry.strip()
            if ":" in entry:
                sid, lvl_str = entry.rsplit(":", 1)
                try:
                    lvl = int(lvl_str)
                    levels.append({
                        "session_id": sid,
                        "level": lvl,
                        "label": PERM_LABELS.get(lvl, f"未知({lvl})"),
                    })
                except ValueError:
                    pass
        return jsonify({
            "success": True,
            "permissions": {
                "default_level": self.config.get("default_permission_level", 1),
                "levels": levels,
            },
        })

    async def handle_set_permission(self):
        """添加或删除单条权限。"""
        from quart import jsonify, request
        try:
            data = await request.get_json()
            action = data.get("action", "")
            session_id = data.get("session_id", "").strip()
            if not session_id:
                return jsonify({"success": False, "error": "缺少 session_id"})
            if action == "set":
                level = int(data.get("level", 1))
                self.tts.perms.set_level(session_id, level)
            elif action == "del":
                self.tts.perms.remove_level(session_id)
            else:
                return jsonify({"success": False, "error": f"未知操作: {action}"})
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    async def handle_get_density_stats(self):
        """实时密度统计数据。"""
        from quart import jsonify
        from datetime import datetime, timedelta
        d = self.tts.density
        # 会话级
        session_stats = []
        for sid, timeline in list(getattr(d, "_voice_timeline", {}).items()):
            now = datetime.now()
            window = self.config.get("density_window_minutes", 10)
            cutoff = now - timedelta(minutes=window)
            recent = [t for t in timeline if t > cutoff]
            session_stats.append({
                "session_id": sid[:20] + "..." if len(sid) > 20 else sid,
                "count": len(recent),
                "max": self.config.get("density_max_count", 3),
                "window_minutes": window,
            })
        # 配置参数
        return jsonify({
            "success": True,
            "stats": {
                "sessions": session_stats,
                "config": {
                    "rate_limit_seconds": self.config.get("rate_limit_seconds", 5),
                    "density_window_minutes": self.config.get("density_window_minutes", 10),
                    "density_max_count": self.config.get("density_max_count", 3),
                    "user_window_minutes": self.config.get("user_density_window_minutes", 60),
                    "user_threshold": self.config.get("user_density_threshold", 5),
                    "user_steepness": self.config.get("user_density_curve_steepness", 0.7),
                },
            },
        })

    async def handle_get_archive_list(self):
        """归档文件列表。"""
        from quart import jsonify
        import os
        from datetime import datetime
        storage_dir = self.tts.archive._storage_dir
        if not storage_dir or not os.path.isdir(storage_dir):
            return jsonify({"success": True, "files": [], "path": "", "total": 0, "retention_days": 0})
        files = []
        for fname in sorted(os.listdir(storage_dir), reverse=True):
            fpath = os.path.join(storage_dir, fname)
            if not os.path.isfile(fpath):
                continue
            stat = os.stat(fpath)
            files.append({
                "name": fname,
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        return jsonify({
            "success": True,
            "files": files,
            "path": storage_dir,
            "total": len(files),
            "retention_days": self.config.get("local_audio_retention_days", 7),
        })

    async def handle_get_archive_file(self):
        """返回归档文件的 base64 数据和 MIME 类型。"""
        from quart import jsonify, request
        import base64, os
        name = request.args.get("name", "")
        if not name or ".." in name or "/" in name:
            return jsonify({"success": False, "error": "无效文件名"})
        storage_dir = self.tts.archive._storage_dir
        if not storage_dir:
            return jsonify({"success": False, "error": "归档目录未初始化"})
        fpath = os.path.normpath(os.path.join(storage_dir, name))
        if not fpath.startswith(os.path.normpath(storage_dir)):
            return jsonify({"success": False, "error": "路径越界"})
        if not os.path.isfile(fpath):
            return jsonify({"success": False, "error": "文件不存在"})
        with open(fpath, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return jsonify({
            "success": True,
            "data": data,
            "mime": self._detect_audio_mime(fpath),
            "name": name,
        })

    async def handle_delete_archive(self):
        """删除归档文件。"""
        from quart import jsonify, request
        import os
        try:
            data = await request.get_json()
            name = data.get("name", "")
            if not name or ".." in name or "/" in name:
                return jsonify({"success": False, "error": "无效文件名"})
            storage_dir = self.tts.archive._storage_dir
            if not storage_dir:
                return jsonify({"success": False, "error": "归档目录未初始化"})
            fpath = os.path.normpath(os.path.join(storage_dir, name))
            if not fpath.startswith(os.path.normpath(storage_dir)):
                return jsonify({"success": False, "error": "路径越界"})
            if os.path.isfile(fpath):
                os.remove(fpath)
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    async def handle_test_tts(self):
        """TTS 测试合成，返回音频 base64。"""
        from quart import jsonify, request
        import base64, os, time
        try:
            data = await request.get_json()
            text = (data.get("text") or "").strip()
            if not text:
                return jsonify({"success": False, "error": "请输入文本"})
            provider_id = data.get("provider_id", "") or self.config.get("tts_provider_id", "")
            provider = None
            if provider_id:
                # 尝试通过注册 ID 查找
                provider = self.tts._resolve_provider(provider_id)
                if not provider:
                    # 可能 provider_id 是 meta.id（显示名），遍历所有 Provider 匹配
                    try:
                        all_providers = self.context.get_all_tts_providers()
                        for p in all_providers:
                            try:
                                if p.meta().id == provider_id:
                                    provider = p
                                    break
                            except Exception:
                                pass
                    except Exception:
                        pass
                if not provider:
                    logger.warning(f"[test_tts] 指定 Provider '{provider_id}' 未找到，尝试自动选择")
            if not provider:
                # 尝试获取系统默认 TTS Provider
                try:
                    provider = self.context.get_using_tts_provider(None)
                except Exception:
                    provider = None
            if not provider:
                # 最后兜底：从已注册的 Provider 列表中取第一个
                try:
                    all_providers = self.context.get_all_tts_providers()
                    for p in all_providers:
                        if hasattr(p, 'get_audio'):
                            provider = p
                            break
                except Exception:
                    pass
            if not provider:
                err_msg = (
                    "未找到可用的 TTS Provider。请先在「配置」页面选择 TTS 引擎并保存，"
                    "或确认 AstrBot 已注册至少一个 TTS Provider。"
                )
                logger.warning(f"[test_tts] {err_msg}")
                return jsonify({"success": False, "error": err_msg})
            start = time.time()
            audio_path = await provider.get_audio(text)
            elapsed = time.time() - start
            with open(audio_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode("utf-8")
            size = os.path.getsize(audio_path)
            # 清理临时文件
            try:
                os.remove(audio_path)
            except OSError:
                pass
            logger.info(
                f"[test_tts] 合成成功: text_len={len(text)} "
                f"elapsed={elapsed:.1f}s size={size}B"
            )
            return jsonify({
                "success": True,
                "data": audio_b64,
                "mime": self._detect_audio_mime(audio_path),
                "elapsed_seconds": round(elapsed, 2),
                "size_bytes": size,
            })
        except Exception as e:
            logger.error(f"[test_tts] 合成异常: {e}", exc_info=True)
            return jsonify({"success": False, "error": f"合成异常: {e}"})

    async def handle_get_tts_providers(self):
        """返回可用的 TTS Provider 列表。"""
        from quart import jsonify
        providers = []
        try:
            all_providers = self.context.get_all_tts_providers()
            for p in all_providers:
                try:
                    meta = p.meta()
                    providers.append({
                        "id": meta.id,
                        "name": meta.id,
                        "model": meta.model or "",
                    })
                except Exception:
                    pass
        except Exception:
            pass
        return jsonify({
            "success": True,
            "providers": providers,
            "current_id": self.config.get("tts_provider_id", ""),
            "fallback_id": self.config.get("tts_fallback_provider_id", ""),
        })

    async def handle_get_recent_sessions(self):
        """返回最近活跃的会话列表。"""
        from quart import jsonify
        sessions = set()
        # From density controller
        for sid in getattr(self.tts.density, "_voice_timeline", {}):
            if sid:
                sessions.add(sid[:30])  # truncate long IDs
        # From recent calls
        for call in getattr(self.tts, "_recent_calls", []):
            sid = call.get("session_id", "")
            if sid:
                sessions.add(sid[:30])
        # From permissions
        for entry in (self.config.get("session_permissions", []) or []):
            entry = entry.strip()
            if ":" in entry:
                sid = entry.rsplit(":", 1)[0].strip()
                if sid:
                    sessions.add(sid[:30])
        return jsonify({
            "success": True,
            "sessions": sorted(sessions)[:50],
        })
