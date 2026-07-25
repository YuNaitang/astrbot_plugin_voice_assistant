"""WebUI API handlers for 聆音 — extracted from main.py into closure-based module.

All handlers are registered via `register_webui_handlers(context, main)` which
binds them to the `main` instance through closures, avoiding the need for a
class-based dispatch layer.
"""

import os

PLUGIN_NAME = "astrbot_plugin_voice_assistant"

from .._config import _CFG_MAP

def _detect_audio_mime(file_path: str) -> str:
    """根据文件扩展名推断音频 MIME 类型。"""
    ext = os.path.splitext(file_path)[1].lower()
    return {
        ".wav": "audio/wav", ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg", ".aac": "audio/aac",
        ".flac": "audio/flac", ".wma": "audio/x-ms-wma",
    }.get(ext, "audio/wav")


def _validate_archive_path(storage_dir, name):
    """Validate archive file path to prevent path traversal."""
    if not name or ".." in name or "/" in name or "\\" in name:
        return None
    fpath = os.path.normpath(os.path.join(storage_dir, name))
    if not fpath.startswith(os.path.normpath(storage_dir)):
        return None
    return fpath


def register_webui_handlers(context, main):
    """Register all WebUI API endpoints.

    `context` is the AstrBot Context (used for register_web_api).
    `main` is the Main class instance (provides access to config, tts, state, etc.).
    """
    from astrbot.api import logger
    from quart import jsonify, request, send_file
    import base64, json as _json
    from datetime import datetime, timedelta

    # Function-level imports to avoid circular dependency at module load time.
    from ..main import (
        SENSITIVE_FIELDS, ALLOWED_GROUPS, NUMERIC_FIELDS,
        CONFIG_FLOAT_FIELDS, PLUGIN_CONFIG_PATH,
    )

    prefix = f"/{PLUGIN_NAME}"

    # ── config accessor closure ──────────────────────────────────

    def _c(key, default=None):
        """读取嵌套配置（扁平键 → 自动路由到对应组）。"""
        mapped = _CFG_MAP.get(key)
        if mapped:
            return main.config.get(mapped[0], {}).get(mapped[1], default)
        return main.config.get(key, default)

    def _persist_config():
        """持久化配置到 JSON 文件。"""
        try:
            config_dir = os.path.dirname(PLUGIN_CONFIG_PATH)
            os.makedirs(config_dir, exist_ok=True)
            with open(PLUGIN_CONFIG_PATH, "w", encoding="utf-8") as f:
                _json.dump(main.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[WebUI] 配置持久化失败（非致命）: {e}")

    # ── handler: get_config ──────────────────────────────────────

    async def handle_get_config():
        """脱敏后返回完整配置。"""
        safe = {}
        for group_key, group_vals in main.config.items():
            if not isinstance(group_vals, dict):
                safe[group_key] = group_vals
                continue
            safe[group_key] = {}
            for k, v in group_vals.items():
                safe[group_key][k] = "***" if k in SENSITIVE_FIELDS else v
        return jsonify({"success": True, "config": safe})

    # ── handler: save_config ─────────────────────────────────────

    async def handle_save_config():
        """白名单过滤后持久化配置。"""
        try:
            data = await request.get_json()
            updates = data.get("config", {})
            if not isinstance(updates, dict):
                return jsonify({"success": False, "error": "格式错误"})
            safe: dict = {}
            for group_name, group_fields in updates.items():
                allowlist = ALLOWED_GROUPS.get(group_name)
                if allowlist is None or not isinstance(group_fields, dict):
                    continue
                filtered = {}
                for k, v in group_fields.items():
                    if k not in allowlist:
                        continue
                    # 类型强制转换
                    if k in NUMERIC_FIELDS:
                        try:
                            filtered[k] = float(v) if k in CONFIG_FLOAT_FIELDS else int(v)
                        except (ValueError, TypeError):
                            continue
                    else:
                        filtered[k] = v
                if filtered:
                    safe[group_name] = filtered
            # 校验：首选与备用 Provider 相同时清空备用
            basic = safe.get("basic_settings", {})
            if basic.get("basic_tts_provider_id") and \
               basic.get("basic_tts_fallback_id") == basic["basic_tts_provider_id"]:
                basic["basic_tts_fallback_id"] = ""
            # 校验：不能选聆音自身
            if basic.get("basic_tts_provider_id") == "lingyin_tts":
                return jsonify({"success": False, "error": "不能选择聆音自身作为 TTS 引擎"})
            if basic.get("basic_tts_fallback_id") == "lingyin_tts":
                return jsonify({"success": False, "error": "备用引擎不能选择聆音自身"})
            main.config.update(safe)
            _persist_config()
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    # ── handler: get_status ──────────────────────────────────────

    async def handle_get_status():
        """运行时状态摘要。"""
        today = datetime.now().date()
        recent = getattr(main.tts, "_recent_calls", []) or []
        today_count = sum(
            1 for r in recent
            if r.get("time", "").startswith(str(today))
        )
        active_sessions = len(main.tts.density._voice_timeline)
        is_limited = any(
            main.tts.density.is_over_density_limit(sid)
            for sid in main.tts.density._voice_timeline
        )
        perm_entries = _c("trigger_session_overrides", []) or []
        return jsonify({
            "success": True,
            "status": {
                "voice_enabled": _c("basic_voice_enabled", True),
                "provider_id": _c("basic_tts_provider_id", "") or "系统默认",
                "today_count": today_count,
                "density_limited": is_limited,
                "active_sessions": active_sessions,
                "default_permission_level": _c("trigger_default_permission", 1),
                "custom_permissions_count": len(perm_entries),
                "recent_calls": list(reversed((getattr(main.tts, "_recent_calls", []) or [])[-10:])),
                "voice_routing": main._voice_routing,
                "bridge_status": main._bridge_status,
                "hooks_enabled": main._enable_event_hooks,
            },
        })

    # ── handler: get_permissions ─────────────────────────────────

    async def handle_get_permissions():
        """权限列表 + 默认等级。"""
        from ..backend.permissions import PERM_LABELS
        entries = _c("trigger_session_overrides", []) or []
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
                "default_level": _c("trigger_default_permission", 1),
                "levels": levels,
            },
        })

    # ── handler: set_permission ──────────────────────────────────

    async def handle_set_permission():
        """添加或删除单条权限。"""
        try:
            data = await request.get_json()
            action = data.get("action", "")
            session_id = data.get("session_id", "").strip()
            if not session_id:
                return jsonify({"success": False, "error": "缺少 session_id"})
            if action == "set":
                level = int(data.get("level", 1))
                main.tts.perms.set_level(session_id, level)
            elif action == "del":
                main.tts.perms.remove_level(session_id)
            else:
                return jsonify({"success": False, "error": f"未知操作: {action}"})
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    # ── handler: get_density_stats ───────────────────────────────

    async def handle_get_density_stats():
        """实时密度统计数据。"""
        d = main.tts.density
        session_stats = []
        for sid, timeline in list(getattr(d, "_voice_timeline", {}).items()):
            now = datetime.now()
            window = _c("trigger_density_window", 10)
            cutoff = now - timedelta(minutes=window)
            recent = [t for t in timeline if t > cutoff]
            session_stats.append({
                "session_id": sid[:20] + "..." if len(sid) > 20 else sid,
                "count": len(recent),
                "max": _c("trigger_density_max_count", 3),
                "window_minutes": window,
            })
        return jsonify({
            "success": True,
            "stats": {
                "sessions": session_stats,
                "config": {
                    "trigger_session_interval": _c("trigger_session_interval", 5),
                    "trigger_density_window": _c("trigger_density_window", 10),
                    "trigger_density_max_count": _c("trigger_density_max_count", 3),
                    "trigger_user_density_window": _c("trigger_user_density_window", 60),
                    "trigger_user_threshold": _c("trigger_user_threshold", 5),
                    "trigger_curve_steepness": _c("trigger_curve_steepness", 0.7),
                },
            },
        })

    # ── handler: get_archive_list ────────────────────────────────

    async def handle_get_archive_list():
        """归档文件列表。"""
        storage_dir = main.tts.archive._storage_dir
        if not storage_dir or not os.path.isdir(storage_dir):
            return jsonify({"success": True, "files": [], "path": "", "total": 0, "retention_days": 0})
        files = []
        AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".aac", ".flac", ".wma"}
        for fname in sorted(os.listdir(storage_dir), reverse=True):
            if os.path.splitext(fname)[1].lower() not in AUDIO_EXTS:
                continue
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
            "retention_days": _c("backup_local_retention_days", 7),
        })

    # ── handler: serve_archive ───────────────────────────────────

    async def handle_serve_archive():
        """返回归档音频二进制流，供 <audio> 播放。"""
        name = request.args.get("name", "")
        fpath = _validate_archive_path(main.tts.archive._storage_dir, name)
        if fpath is None:
            return jsonify({"success": False, "error": "无效文件名或路径越界"})
        if not os.path.isfile(fpath):
            return jsonify({"success": False, "error": "文件不存在"})
        logger.info(f"[serve_archive] 发送: {fpath}")
        return await send_file(fpath, mimetype=_detect_audio_mime(fpath))

    # ── handler: get_archive_file ────────────────────────────────

    async def handle_get_archive_file():
        """返回归档文件 base64（供 bridge API 在 iframe 内播放）。"""
        name = request.args.get("name", "")
        fpath = _validate_archive_path(main.tts.archive._storage_dir, name)
        if fpath is None:
            return jsonify({"success": False, "error": "无效文件名或路径越界"})
        if not os.path.isfile(fpath):
            return jsonify({"success": False, "error": "文件不存在"})
        with open(fpath, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return jsonify({
            "success": True,
            "data": data,
            "mime": _detect_audio_mime(fpath),
            "name": name,
        })

    # ── handler: delete_archive ──────────────────────────────────

    async def handle_delete_archive():
        """删除归档文件。"""
        try:
            data = await request.get_json()
            name = data.get("name", "")
            fpath = _validate_archive_path(main.tts.archive._storage_dir, name)
            if fpath is None:
                return jsonify({"success": False, "error": "无效文件名或路径越界"})
            if os.path.isfile(fpath):
                os.remove(fpath)
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    # ── handler: test_tts ────────────────────────────────────────

    async def handle_test_tts():
        """TTS 测试合成，返回音频 base64。"""
        import time
        try:
            data = await request.get_json()
            text = (data.get("text") or "").strip()
            if not text:
                return jsonify({"success": False, "error": "请输入文本"})
            provider_id = data.get("provider_id", "") or _c("basic_tts_provider_id", "")
            provider = None
            if provider_id:
                # 尝试通过注册 ID 查找
                provider = main.tts._resolve_provider(provider_id)
                if not provider:
                    # 可能 provider_id 是 meta.id（显示名），遍历所有 Provider 匹配
                    try:
                        all_providers = context.get_all_tts_providers()
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
                    provider = context.get_using_tts_provider(None)
                except Exception:
                    provider = None
            if not provider:
                # 最后兜底：从已注册的 Provider 列表中取第一个
                try:
                    all_providers = context.get_all_tts_providers()
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

            # 读文件为 base64（通过 bridge API 传回前端，iframe 不能直接 HTTP 请求）
            with open(audio_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode("utf-8")
            size = os.path.getsize(audio_path)
            mime = _detect_audio_mime(audio_path)

            # 归档（此时文件还在，save_file 内部会 move）
            archived_path = None
            try:
                archived_path = main.tts.archive.save_file(audio_path, text)
                if archived_path:
                    await main.tts._cloud_backup(archived_path, text)
            except Exception as arc_e:
                logger.warning(f"[test_tts] 归档失败（非致命）: {arc_e}")

            # 清理
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            except OSError:
                pass

            logger.info(
                f"[test_tts] 合成成功: text_len={len(text)} "
                f"elapsed={elapsed:.1f}s size={size}B"
                + (f" archived={archived_path}" if archived_path else "")
            )
            return jsonify({
                "success": True,
                "data": audio_b64,
                "mime": mime,
                "filename": os.path.basename(archived_path) if archived_path else "",
                "elapsed_seconds": round(elapsed, 2),
                "size_bytes": size,
            })
        except Exception as e:
            logger.error(f"[test_tts] 合成异常: {e}", exc_info=True)
            return jsonify({"success": False, "error": f"合成异常: {e}"})

    # ── handler: get_tts_providers ───────────────────────────────

    async def handle_get_tts_providers():
        """返回可用的 TTS Provider 列表 + LLM Provider 列表。"""
        providers = []
        llm_providers = []
        try:
            all_providers = context.get_all_tts_providers()
            for p in all_providers:
                try:
                    meta = p.meta()
                    if meta.id == "lingyin_tts":
                        continue
                    providers.append({
                        "id": meta.id,
                        "name": meta.id,
                        "model": meta.model or "",
                    })
                except Exception:
                    pass
        except Exception:
            pass
        try:
            pm = getattr(context, "provider_manager", None)
            if pm:
                for cfg in getattr(pm, "providers_config", []) or []:
                    ptype = cfg.get("provider_type", "") or cfg.get("type", "")
                    if ptype == "chat_completion" and cfg.get("enable", True):
                        model = cfg.get("model", "") or ""
                        llm_providers.append({
                            "id": cfg.get("id", ""),
                            "name": cfg.get("id", ""),
                            "model": model,
                        })
        except Exception:
            pass
        return jsonify({
            "success": True,
            "providers": providers,
            "llm_providers": llm_providers,
            "current_id": _c("basic_tts_provider_id", ""),
            "fallback_id": _c("basic_tts_fallback_id", ""),
        })

    # ── handler: get_recent_sessions ─────────────────────────────

    async def handle_get_recent_sessions():
        """返回最近活跃的会话列表。"""
        sessions = set()
        # From density controller
        for sid in getattr(main.tts.density, "_voice_timeline", {}):
            if sid:
                sessions.add(sid[:30])
        # From recent calls
        for call in getattr(main.tts, "_recent_calls", []):
            sid = call.get("session_id", "")
            if sid:
                sessions.add(sid[:30])
        # From permissions
        for entry in (_c("trigger_session_overrides", []) or []):
            entry = entry.strip()
            if ":" in entry:
                sid = entry.rsplit(":", 1)[0].strip()
                if sid:
                    sessions.add(sid[:30])
        return jsonify({
            "success": True,
            "sessions": sorted(sessions)[:50],
        })

    # ── handler: serve_panel ─────────────────────────────────────

    async def handle_serve_panel():
        """返回独立管理面板 HTML。"""
        if not _c("webui_enabled", True):
            return jsonify({"success": False, "error": "WebUI 面板已禁用"})
        # __file__ is api/handlers.py, go up two levels to plugin root
        plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        panel_path = os.path.join(plugin_root, "pages", "webui", "standalone.html")
        if not os.path.isfile(panel_path):
            return jsonify({"success": False, "error": "面板文件未找到"})
        return await send_file(panel_path, mimetype="text/html; charset=utf-8")

    # ── register all endpoints ───────────────────────────────────

    context.register_web_api(f"{prefix}/get_config", handle_get_config, ["GET"], "读取配置")
    context.register_web_api(f"{prefix}/save_config", handle_save_config, ["POST"], "保存配置")
    context.register_web_api(f"{prefix}/get_status", handle_get_status, ["GET"], "运行时状态")
    context.register_web_api(f"{prefix}/get_permissions", handle_get_permissions, ["GET"], "权限列表")
    context.register_web_api(f"{prefix}/set_permission", handle_set_permission, ["POST"], "设置/删除权限")
    context.register_web_api(f"{prefix}/get_density_stats", handle_get_density_stats, ["GET"], "密度统计数据")
    context.register_web_api(f"{prefix}/get_archive_list", handle_get_archive_list, ["GET"], "归档文件列表")
    context.register_web_api(f"{prefix}/serve_archive", handle_serve_archive, ["GET"], "获取归档音频文件")
    context.register_web_api(f"{prefix}/get_archive_file", handle_get_archive_file, ["GET"], "获取归档文件 base64")
    context.register_web_api(f"{prefix}/delete_archive", handle_delete_archive, ["POST"], "删除归档文件")
    context.register_web_api(f"{prefix}/test_tts", handle_test_tts, ["POST"], "TTS 测试合成")
    context.register_web_api(f"{prefix}/get_tts_providers", handle_get_tts_providers, ["GET"], "TTS 提供商列表")
    context.register_web_api(f"{prefix}/get_recent_sessions", handle_get_recent_sessions, ["GET"], "最近会话列表")
    context.register_web_api(f"{prefix}/panel", handle_serve_panel, ["GET"], "管理面板（独立页面，无沙箱）")

    logger.info(f"聆音 WebUI API 已注册（{prefix}）")
