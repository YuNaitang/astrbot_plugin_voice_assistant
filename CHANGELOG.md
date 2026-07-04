# Changelog

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
