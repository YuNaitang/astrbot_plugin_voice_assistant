# AI Voice Assistant — AstrBot Plugin

让 AI 能主动调用 TTS 回复语音。不绑定特定供应商，用户可在管理面板选择首选/兜底 TTS Provider。

## 功能

- **LLM 工具 `ai_speak`**：AI 通过 function calling 自主决定何时发语音
- **多 Provider 支持**：首选 → 兜底 → 系统默认三级降级
- **会话级权限控制**：白名单/黑名单 + AstrBot 管理员豁免
- **速率限制**：每个会话可配置最小调用间隔
- **文字+语音双输出**：同时发送文字消息和语音消息

## 前置条件

需要 AstrBot 已注册至少一个 TTS Provider（如 [aliyun-minimax-tts](https://github.com/YuNaitang/aliyun-minimax-tts)）。

## 配置

在 AstrBot WebUI 管理面板中配置：

| 配置项 | 说明 |
|--------|------|
| `tts_provider_id` | 首选 TTS Provider ID（留空使用默认） |
| `tts_fallback_provider_id` | 兜底 TTS Provider ID |
| `sessions_whitelist` | 允许语音的会话 ID 列表 |
| `sessions_blacklist` | 禁止语音的会话 ID 列表 |
| `rate_limit_seconds` | 每个会话 TTS 调用最小间隔（秒） |

可用 Provider ID 见 AstrBot 启动日志。

## 工作原理

```
用户输入 → LLM 思考 → LLM 调用 ai_speak(text)
                            ↓
                    权限检查 → Provider 获取 → TTS 合成
                            ↓
                    文字 + 语音同时发送
```

LLM 不需要关注 TTS 细节，只需提供 `text` 参数，插件自动完成语音合成。

## 开发

```bash
# 安装依赖
pip install astrbot

# 启动测试
cd <astrbot-root>
astrbot run
```
