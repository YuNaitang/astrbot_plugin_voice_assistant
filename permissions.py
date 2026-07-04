"""
聆音 — 权限管理
====================================
权限等级: 0=无限制, 1=基准限制, 2=完全限制。
支持按 session ID / QQ 号单独配置，支持 /voice_perm 指令管理。

注意: 此文件已迁移至 backend/permissions.py，保留作为向后兼容导入。
"""
import warnings
warnings.warn(
    "直接导入 permissions.py 已弃用，请改为 from .backend.permissions import ...",
    DeprecationWarning,
    stacklevel=2,
)

from .backend.permissions import *  # noqa: F401, F403
