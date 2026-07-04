"""
curl 可执行文件路径解析。
注意: 所有云上传 Provider 已改用 boto3 / aiohttp，不再依赖此模块。
保留作为 SMB 等外部命令调用的备用工具。
"""
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
