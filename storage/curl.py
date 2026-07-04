"""curl 可执行文件路径解析。"""
import shutil
import subprocess
from typing import Optional


def find_curl() -> Optional[str]:
    """查找 curl 可执行文件路径，优先 shutil.which，兜底走系统 shell。"""
    path = shutil.which("curl")
    if path:
        return path
    try:
        result = subprocess.run(
            "command -v curl",
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None
