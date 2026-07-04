"""
AI Voice Assistant — 本地音频归档
==================================
包含初始化音频存储目录、文件持久化（move）、过期清理。
"""
import os
import random
import shutil
from datetime import datetime, timedelta
from typing import Optional

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from ..errors import ArchiveError


class LocalArchive:
    """本地音频归档管理。"""

    def __init__(self, config: dict):
        self.config = config
        self._storage_dir: Optional[str] = None
        self._enabled = False
        self._init()

    # ── 初始化 ────────────────────────────────────────────────

    def _init(self) -> None:
        """初始化本地音频归档目录。"""
        raw = (self.config.get("local_audio_dir") or "").strip()
        if not raw:
            raw = os.path.join(
                get_astrbot_plugin_data_path(),
                "astrbot_plugin_voice_assistant",
                "tts_archive",
            )
        raw = os.path.realpath(raw)
        try:
            os.makedirs(raw, exist_ok=True)
            self._storage_dir = raw
            self._enabled = True
            retention = self.config.get("local_audio_retention_days", 7)
            cleaned = self.cleanup_old(retention)
            logger.info(
                f"AI Voice Assistant: 音频存储目录 {raw} "
                f"(保留 {retention} 天，本次清理 {cleaned} 个)"
            )
        except OSError as e:
            logger.warning(f"AI Voice Assistant: 无法创建音频存储目录 {raw}: {e}")
            self._enabled = False

    # ── 归档 ──────────────────────────────────────────────────

    def save_file(self, audio_path: str) -> Optional[str]:
        """将临时音频文件移到本地持久目录。返回持久化路径，失败返回 None。"""
        if not self._enabled or not self._storage_dir:
            return None
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            uid = random.randint(100000, 999999)
            basename = f"voice_{ts}_{uid}.wav"
            dest = os.path.join(self._storage_dir, basename)
            counter = 1
            while os.path.exists(dest):
                dest = os.path.join(
                    self._storage_dir,
                    f"voice_{ts}_{uid}_{counter}.wav",
                )
                counter += 1
            shutil.move(audio_path, dest)
            logger.info(f"[tts_storage] 已归档: {dest}")
            return dest
        except OSError as e:
            logger.warning(f"[tts_storage] 归档失败: {e}")
            raise ArchiveError(f"归档失败: {e}") from e

    # ── 清理 ──────────────────────────────────────────────────

    def cleanup_old(self, retention_days: int = 7) -> int:
        """删除超过 retention_days 天的音频文件。返回删除数量。"""
        if not self._storage_dir:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)
        count = 0
        try:
            for fname in os.listdir(self._storage_dir):
                fpath = os.path.join(self._storage_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
                if mtime < cutoff:
                    os.remove(fpath)
                    count += 1
        except OSError as e:
            logger.warning(f"[tts_storage] 清理音频文件失败: {e}")
        return count
