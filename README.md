# 聆音 — AI 语音助手

聆音是 AstrBot 的拟人化 TTS 行为系统，原生 TTS 调度的透明替换层。AI 人格通过它自主决定何时说话、用什么语气说。

## 核心特性

- **AI 人格驱动** — system prompt 注入语音规则 + 概率决策，AI 性格决定说话频率
- **情感控制** — AI 自标 `[tone:xxx]` > Keyword 检测 > 韵律降级
- **语言转换** — AI 直接输出目标语言 + 聆音质量校验 + LLM 兜底翻译
- **分层概率控制** — 群聊/私聊独立概率、强制概率模式、会话级覆盖
- **密度控制** — 会话级硬阻断 + 用户级概率降权，防止刷屏
- **缓存友好** — 语音规则模板固定注入，仅 15 字概率决策行随请求变化
- **文本清洗** — 合成前自动净化（emoji/颜文字/网络用语/去重/标点）
- **PC 共存** — auto/lingyin/other 三模式，自动检测 private_companion 状态
- **标签兼容** — 支持 `<lingyin>` / `<tts>` / `<pc_tts>`，可扩展自定义标签
- **品牌命令 /ly** — 权限、引擎、概率、开关、路由统一管理

## 架构

```
__init__.py          入口 re-export
core.py              Main 类：构造、Provider 注册、事件钩子、指令
│
├── api/handlers.py       WebUI API handler（闭包绑定，16 个端点）
│
├── providers/
│   └── lingyin_provider.py     TTSProvider — 注册 + 兜底增强 + 路由
│
├── backend/
│   ├── injector.py              system prompt 注入（固定模板，缓存友好）
│   ├── tag_parser.py            标签解析 & 归一化 & Token 保护
│   ├── pipeline.py              核心管线编排
│   ├── emotion.py               情感引擎（AI 自标 + Keyword + 韵律）
│   ├── language.py              语言检测 + 转换 + 质量校验
│   ├── sanitizer.py             文本清洗
│   ├── frequency.py             频率门控（概率采样 + 密度 + 速率）
│   ├── tts_handler.py           ai_speak 工具编排
│   ├── synthesizer.py           音频分段、合成、合并（纯函数）
│   ├── permissions.py           三级权限管理
│   └── density.py               双层密度控制
│
├── bridge/
│   └── pc_bridge.py             private_companion 共存桥接
│
├── storage/                     音频存储 & 云备份
├── _config.py                   共享配置映射表
└── pages/webui/standalone.html  独立管理面板
```

## 概率与语音决策流程

```
on_llm_request
  ├── 注入语音规则模板（固定文本 → 缓存命中）
  ├── [语音概率提醒] 本轮放行/不放行（概率判定结果）
  └── LLM 参考决策回复

on_decorating_result
  ├── 有 <lingyin> 标签 → 密度/速率检查 → 合成语音
  ├── 无标签 + 强制概率开 → 包裹全文转语音
  └── 无标签 + 强制概率关 → 纯文字
```

密度控制在输出层由 FrequencyGate 独立完成，不影响 system prompt 缓存。

## 指令

| 指令 | 说明 |
|------|------|
| `/ly perm set <ID> <0\|1\|2> [prob]` | 设置会话权限等级+可选概率 |
| `/ly perm list` | 列出所有自定义权限 |
| `/ly perm get [ID]` | 查询会话权限 |
| `/ly perm del <ID>` | 删除自定义权限 |
| `/ly engine set <ID> <engine>` | 设置会话 TTS 引擎 |
| `/ly engine get/list` | 查询/列出会话引擎 |
| `/ly prob set <ID> <0~1>` | 设置会话概率覆盖 |
| `/ly status` | 当前状态摘要 |
| `/ly on / off` | 启用/禁用语音 |
| `/ly route <auto\|lingyin\|other>` | 设置路由模式 |
| `/voice_perm ...` | 兼容别名，等同于 `/ly perm ...` |
| `/sid` | 获取当前会话 ID |

## 日志

| 关键词 | 含义 |
|--------|------|
| `[聆音]` | 加载/卸载/桥接/决策信息 |
| `[聆音] TTS决策` | 阶段决策日志（跳过/处理/强制概率） |
| `[ai_speak] >>>` | LLM 调用了 ai_speak 工具 |
| `[ai_speak] TTS合成` | 语音合成进度 |
| `[voice_perm]` | 权限管理操作 |

## 版本

- **v3.0** — 拟人化 TTS 行为系统：TTSProvider 注册、三层事件钩子、六大增强引擎、缓存友好重构
- **v2.x** — WebUI、云存储、模块化重构
- **v1.x** — ai_speak 工具、权限管理、长文本分段

## 致谢

- [astrbot_plugin_meme_manager](https://github.com/anka-afk/astrbot_plugin_meme_manager)
- [astrbot_plugin_private_companion](https://github.com/initencounter/astrbot_plugin_private_companion)
- [astrbot_plugin_group_chat_plus](https://github.com/Him666233/astrbot_plugin_group_chat_plus)
- [AstrBot](https://github.com/Soulter/AstrBot)
- [MaiBot](https://github.com/MaiM-with-u/MaiBot)
