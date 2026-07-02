# AI Voice Assistant — AstrBot Plugin

允许AI 通过 `ai_speak` 工具自主调用 TTS 回复语音。支持多 Provider 降级、双层密度控制、QQ 号白名单。

## 功能

- **LLM 工具 `ai_speak`** — AI 通过 function calling 自主决定何时发语音
- **多 Provider 降级** — 首选 → 兜底 → 系统默认三级自动切换
- **双层密度控制** — 会话级硬阻断 + 用户级 Logistic 概率降权
- **会话/QQ 号权限** — 完整 session ID / QQ 号 / 群号三种格式的白名单
- **速率限制** — 每会话可配置最小调用间隔
- **文字+语音双输出** — 同时发送文字和语音文件

## 前置条件

AstrBot 已注册至少一个 TTS Provider（如 [aliyun-minimax-tts](https://github.com/YuNaitang/aliyun-minimax-tts)）。

## 配置

在 WebUI 管理面板中配置以下项：

| 配置 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `tts_provider_id` | 下拉 | — | 首选 TTS Provider |
| `tts_fallback_provider_id` | 下拉 | — | 兜底 Provider |
| `voice_enabled` | bool | true | 总开关 |
| `sessions_whitelist` | list[str] | [] | 允许的完整会话 ID（/sid 格式） |
| `sessions_blacklist` | list[str] | [] | 禁止的完整会话 ID |
| `qq_users_whitelist` | list[str] | [] | 允许的 QQ 用户号（私聊） |
| `qq_users_blacklist` | list[str] | [] | 禁止的 QQ 用户号 |
| `qq_groups_whitelist` | list[str] | [] | 允许的 QQ 群号 |
| `qq_groups_blacklist` | list[str] | [] | 禁止的 QQ 群号 |
| `rate_limit_seconds` | int | 5 | TTS 调用最小间隔（秒） |
| `density_window_minutes` | int | 10 | 会话密度统计窗口（分） |
| `density_max_count` | int | 3 | 窗口内最大语音次数 |
| `user_density_window_minutes` | int | 60 | 用户触发统计窗口（分） |
| `user_density_threshold` | int | 5 | 用户触发频次阈值 |
| `user_density_curve_steepness` | float | 0.7 | 概率衰减陡峭度（0=关闭） |
| `voice_prompt_extra` | text | "" | 额外语音 Prompt（拟人化定制） |
| `log_level` | string | "info" | info / debug |

## 工作原理

```
用户输入 → LLM 思考 → LLM 调用 ai_speak(text)
                            ↓
           权限检查 → 密度检查 → Provider 获取 → TTS 合成
                            ↓
                    文字 + 语音同时发送
```

LLM 调用 `ai_speak` 后会收到执行结果（成功/失败原因），可据此调整后续行为。

## 密度控制

**会话级**（硬阻断）：短窗口内超限后完全阻止语音，注入提示给 LLM。
**用户级**（概率降权）：Logistic 曲线 `P=1/(1+exp(steepness×(count-threshold)))`，每个用户独立统计，静默降权。

| 触发次数 | 概率 |
|---------|------|
| 0 | ~97% |
| 3 | ~80% |
| 5（阈值） | 50% |
| 7 | ~20% |
| 10+ | ~3% |