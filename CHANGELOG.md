# Changelog

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
