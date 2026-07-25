# Changelog

## 3.0.0
- **重构: 缓存友好化** — 密度提醒移出 system prompt，语音规则模板固定注入，概率决策行化（15 字/次），缓存状态从 7 种降至 2 种
- **重构: 文件拆分** — WebUI handlers 拆至 `api/handlers.py`；`backend/synthesizer.py` 纯函数提取；`_config.py` 共享映射表
- **重构: 架构清理** — 删除 `storage/curl.py`、permissions.py shim、TtsPipeline 冗余参数、StubGateResult、死 import
- **新增: 输入概率决策** — `on_llm_request` 阶段按群聊/私聊概率追加决策行，控制 AI 是否考虑语音
- **新增: 强制概率模式** — 无标签时强制包裹 `<lingyin>` 转语音
- **新增: 品牌命令 `/ly`** — 权限/引擎/概率/状态/开关/路由 6 子命令，`/voice_perm` 保留为别名
- **新增: 标签兼容项** — 可添加自定义标签格式，自动归一化到 `<lingyin>`
- **新增: TTS 文本模型** — 支持从系统 LLM Provider 列表选择独立后处理模型
- **新增: 合并目标时长** — `text_merge_target_duration` 控制合并长度
- **增强: 提供者自选防护** — 聆音不出现在 Provider 下拉列表
- **增强: 概率体系重构** — 移除全局概率，群聊/私聊独立配置
- **增强: 密度警告内存安全** — `_density_warned` 改为 dict，超限自动清理
- **重构: 拟人化 TTS 行为系统** — 从单工具插件升级为完整 TTS 编排系统
- **新增: TTSProvider 注册** — 聆音注册为 AstrBot 标准 `TTSProvider`（`lingyin_tts`），所有插件通过 `context.get_using_tts_provider()` 透明走聆音管线
- **新增: 三层事件钩子管线**
  - `on_llm_request` — 注入 AI 人格驱动的语音规则（无声硬概率）
  - `on_llm_response` — 归一化 `<lingyin>/<tts>/<pc_tts>` 标签 + Token 保护 + 语言质量校验
  - `on_decorating_result`(priority=-21000) — 发送前完整编排
- **新增: 六大增强引擎**
  - `sanitizer.py` — 文本清洗（移植 PC 规则集：emoji/颜文字/网络用语/去重/标点）
  - `emotion.py` — 分层情感引擎（AI 自标 [tone:xxx] > Keyword 检测 > 韵律降级）
  - `language.py` — 语言检测 + 质量校验 + LLM 翻译兜底
  - `injector.py` — 精简 system prompt 注入（AI 人格驱动频率使用）
  - `frequency.py` — AI 人格频率门控 + DensityController 对接
  - `tag_parser.py` — 标签解析 + Token 保护 + 格式归一化
- **新增: PC 共存桥接** — `bridge/pc_bridge.py` 三模式（auto/lingyin/pc）
- **新增: Engine 接口契约** — `pipeline.py` 顶部定义所有引擎输入/输出签名
- **新增: 单元测试** — 37 个测试覆盖 sanitizer/language/emotion 核心模块
- **增强: ai_speak 防双重处理** — `_lingyin_voice_done` 标记
- **增强: 可见文本管理** — 外语语音块后自动补中文可见文本
- **增强: 多 TTS Provider 降级路由** — 首选 > 降级 > 系统默认 > 遍历 <li></li>
- 修复: 密码字段 AstrBot WebUI 自动脱敏覆盖 — 改名 `_passwd` 避开关键词
- 新增: 独立 WebUI 服务器 — `webui.py` (Quart + Hypercorn)，端口 `webui_port` 可配置（默认 11180）
- 新增: 云存储全链路 Python 化 — S3(boto3) + Custom(aiohttp) + WebDAV(aiohttp)，移除 curl 依赖
- 增强: TTS 合成限流防护 — 段间间隔(`tts_inter_segment_delay`) + 指数退避重试(`tts_retry_max_attempts`)
- 增强: 备份发送重建 — 每段信息/语音/文件三条独立消息 + 汇总 + 仅群号
- 增强: `send_text_with_voice` 配置 — 控制是否同时发送文字（默认 false 仅语音）

## 2.1.0-beta
- 品牌命名: 中文名正式定为「聆音」
- 新增: 独立 WebUI 面板（`/panel` 端点，不受 AstrBot iframe 沙箱限制）
- 新增: 配置管理、权限管理、密度统计、归档浏览、TTS 测试等 6 个管理页面
- 新增: 归档音频侧车元数据文件（同名 .txt）
- 新增: `webui_enabled` 配置项
- 增强: `handle_get_tts_providers` — Provider 选择下拉列表
- 增强: `handle_get_recent_sessions` — 会话 ID 自动补全
- 架构: 原生 AstrBot WebUI 集成（`register_web_api` + bridge SDK）
- 修复: iframe sandbox 兼容 — 移除 `confirm()`，Web Audio API 直播放音
- 修复: TTS 测试音频通过 base64 桥接传输，绕过 HTTP 直连认证限制

## 2.0.0
- 重构: 模块化拆包 — 单文件 main.py (603行) + storage.py (410行) 拆分为 backend/ + storage/ 子包
- 重构: 新增 errors.py — 8 个自定义异常类层次，用精确捕获替换裸 `except Exception`
- 重构: 云存储抽象 — `CloudProvider` ABC + Custom / S3 / WebDAV 三个独立 Provider 实现
- 重构: 密度控制抽取 — `DensityController` 独立类替代 main.py 中散落的 6 个方法
- 重构: `_find_curl()` 独立为 `storage/curl.py`，`LocalArchive` 独立为 `storage/local.py`
- 兼容: `permissions.py` 保留向后兼容的 re-export shim
- 增强: 备份发送 — 未配置时默认发往 bot 自身，消息包含文字信息 + 语音 + 原始 WAV 文件（File 组件）

## 1.5.3
- 修复: curl 查找兜底 — `shutil.which` 失败时走 `command -v curl` 系统 shell 路径

## 1.5.2
- 修复: 备份发送失败 — `send_by_session` → `send_message`（Context 无此方法）
- 修复: 云存储上传异常 — curl PATH 解析兼容性（`execvp` ENOTDIR 跨平台修复）
- 修复: 本地归档跨盘符失败 — `os.rename` → `shutil.move`

## 1.5.1
- 新增 CHANGELOG.md，版本历史迁移至独立文件
- 补充 metadata.yaml 字段（tags / category）

## 1.4.0
- 模块化设置面板，配置项分组管理
- 备份会话功能，ai_speak 同时转发语音到指定会话
- 配置面板简化，QQ 号统一 + 标题精简
- 合并开关 + 超时修复

## 1.3.0
- ai_speak 返回值修复，LLM 可知执行结果
- 添加 support_platforms 字段，适配插件市场

## 1.2.0
- 权限监控，三级权限管理
- 文本描述修正
- 更新权限判定逻辑

## 1.1.0
- 精简文本描述，提升信息密度

## 1.0.0
- 初始版本
- AI 主动调用 TTS 回复语音
- 双层密度控制（会话级硬阻断 + 用户级概率降权）
- 长文本分段合并
- 多 Provider 降级机制
