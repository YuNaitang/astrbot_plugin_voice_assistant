"""
聆音 — 本地音频归档
===================================
包含初始化音频存储目录、文件持久化（move）、过期清理。
每个归档 WAV 附带同名的 .txt 元数据文件。
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
    """本地音频归档管理：目录初始化 → 文件移入 → 过期清理。"""

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
                f"聆音: 音频存储目录 {raw} "
                f"(保留 {retention} 天，本次清理 {cleaned} 个)"
            )
        except OSError as e:
            logger.warning(f"聆音: 无法创建音频存储目录 {raw}: {e}")
            self._enabled = False

    # ── 归档 ──────────────────────────────────────────────────

    def save_file(self, audio_path: str, text: str = "") -> Optional[str]:
        """将临时音频文件移到本地持久目录，同时写入元数据 .txt。

        Args:
            audio_path: 临时 WAV 文件路径。
            text: 语音对应的文本内容（可选），会写入同名的 .txt 侧车文件。

        Returns:
            持久化后的 WAV 文件路径，失败返回 None。
        """
        if not self._enabled or not self._storage_dir:
            return None
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            uid = random.randint(100000, 999999)
            basename = f"voice_{ts}_{uid}"
            dest_wav = os.path.join(self._storage_dir, f"{basename}.wav")
            counter = 1
            while os.path.exists(dest_wav):
                dest_wav = os.path.join(
                    self._storage_dir,
                    f"{basename}_{counter}.wav",
                )
                counter += 1

            # 移动音频文件
            shutil.move(audio_path, dest_wav)
            logger.info(f"[tts_storage] 已归档: {dest_wav}")

            # 写入侧车元数据文件
            dest_txt = dest_wav.rsplit(".", 1)[0] + ".txt"
            meta_lines = [
                f"File: {os.path.basename(dest_wav)}",
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"Size: {os.path.getsize(dest_wav)} bytes",
            ]
            if text:
                # 写两行：一行原始内容，一行截断摘要（方便扫一眼）
                meta_lines.append(f"Text: {text}")
                meta_lines.append(f"TextPreview: {text[:200]}")
            try:
                with open(dest_txt, "w", encoding="utf-8") as f:
                    f.write("\n".join(meta_lines) + "\n")
                logger.info(f"[tts_storage] 已写入元数据: {dest_txt}")
            except OSError as e:
                logger.warning(f"[tts_storage] 写入元数据失败（非致命）: {e}")

            return dest_wav
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
