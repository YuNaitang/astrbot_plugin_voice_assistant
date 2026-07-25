"""聆音 — 音频合成工具。纯函数，不依赖 TtsHandler 实例。"""
import asyncio
import os
import random
import re
import tempfile

from astrbot.api import logger
from astrbot.core.provider.provider import TTSProvider

from ..errors import TTSProviderError


def segment_text(text: str, max_chars: int = 80) -> list[str]:
    """按换行→句号→逗号→强制切分优先级分段。"""
    if len(text) <= max_chars:
        return [text]
    blocks = re.split(r'\n+', text)
    segments = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if len(block) <= max_chars:
            segments.append(block)
        else:
            sub = re.split(r'(?<=[。？！])', block)
            for sub_seg in sub:
                sub_seg = sub_seg.strip()
                if not sub_seg:
                    continue
                if len(sub_seg) <= max_chars:
                    segments.append(sub_seg)
                else:
                    sub2 = re.split(r'(?<=[，；：])', sub_seg)
                    for s in sub2:
                        s = s.strip()
                        if not s:
                            continue
                        if len(s) <= max_chars:
                            segments.append(s)
                        else:
                            while len(s) > max_chars:
                                segments.append(s[:max_chars])
                                s = s[max_chars:]
                            if s:
                                segments.append(s)
    return segments


def merge_audio_files(audio_paths: list[str], target_duration: int = 0) -> str:
    """使用 pydub 合并音频。超过 target_duration(秒)停止追加，0=不限。"""
    from pydub import AudioSegment
    combined = AudioSegment.empty()
    total_ms = 0
    target_ms = target_duration * 1000 if target_duration > 0 else float("inf")
    for ap in audio_paths:
        seg = AudioSegment.from_file(ap)
        if total_ms + len(seg) > target_ms and total_ms > 0:
            break
        combined += seg
        total_ms += len(seg)
    merged_dir = tempfile.gettempdir()
    merged_path = os.path.join(merged_dir, f"tts_merged_{random.randint(100000, 999999)}.wav")
    combined.export(merged_path, format="wav")
    return merged_path


async def synthesize_segments(provider: TTSProvider, segments: list[str],
                               delay: float = 0.3, max_attempts: int = 2) -> list[str]:
    """逐段合成音频，支持段间延迟和指数退避重试。"""
    audio_paths = []
    for i, seg in enumerate(segments):
        if i > 0 and delay > 0:
            await asyncio.sleep(delay)
        for attempt in range(1 + max_attempts):
            try:
                logger.info(
                    f"[ai_speak] TTS合成 段{i+1}/{len(segments)}: "
                    f"text={seg!r}{'' if not attempt else f' (retry {attempt})'}"
                )
                audio_path = await provider.get_audio(seg)
                logger.debug(f"[ai_speak] TTS合成完成 段{i+1}: path={audio_path}")
                audio_paths.append(audio_path)
                break
            except Exception as e:
                pid = "?"
                try:
                    pid = provider.meta().id
                except Exception:
                    pass
                if attempt < max_attempts:
                    wait = 2 ** attempt
                    logger.warning(
                        f"[ai_speak] TTS合成 段{i+1} 失败，{wait}s 后重试 "
                        f"({attempt+1}/{max_attempts}) (provider={pid}): {e}"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"[ai_speak] TTS合成失败 段{i+1} (provider={pid}): {e}")
                    raise TTSProviderError(f"语音合成失败({pid})：{e!s}") from e
    return audio_paths


def format_bytes(size: int) -> str:
    """字节数格式化。"""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def format_file_size(file_path: str) -> str:
    """文件大小格式化。"""
    try:
        return format_bytes(os.path.getsize(file_path))
    except OSError:
        return "未知"


__all__ = ["segment_text", "merge_audio_files", "synthesize_segments",
           "format_bytes", "format_file_size"]
