"""
AI Voice Assistant — AstrBot 通用 TTS 编排插件

允许AI 通过工具自主调用 TTS 回复语音。
支持多 Provider 降级、三级权限管理、双层密度控制、长文本分段合并。
"""
import re

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .backend.permissions import PERM_BASIC, PERM_LABELS, PERM_RESTRICTED, PERM_UNLIMITED
from .backend.tts_handler import TtsHandler


class Main(Star):
    """AI Voice Assistant — 让 AI 主动调用 TTS 回复语音"""

    def __init__(self, context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # TTS 编排处理器（持有所有子模块引用）
        self.tts = TtsHandler(context, self.config)

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
