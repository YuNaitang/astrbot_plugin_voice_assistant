"""
聆音 — 自定义异常层次
=====================================
基类 VoiceAssistantError，按模块细分异常类型，便于精确捕获和调试。
"""


class VoiceAssistantError(Exception):
    """插件通用异常基类。"""


# ── TTS 相关 ──────────────────────────────────────────────

class TTSProviderError(VoiceAssistantError):
    """TTS 提供商相关异常（提供商不可用、合成失败等）。"""


# ── 密度控制 ───────────────────────────────────────────────

class DensityLimitError(VoiceAssistantError):
    """密度/频率限制阻断。"""


# ── 存储 / 归档 / 云上传 ────────────────────────────────────

class StorageError(VoiceAssistantError):
    """存储相关异常。"""


class ArchiveError(StorageError):
    """本地归档异常（跨盘符移动失败、目录不可写等）。"""


class CloudUploadError(StorageError):
    """云存储上传异常。"""


class CurlNotFoundError(CloudUploadError):
    """未找到 curl 可执行文件。"""
