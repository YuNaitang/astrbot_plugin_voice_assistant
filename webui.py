"""
聆音 — 独立 WebUI 服务器
=============================
使用 Quart + Hypercorn 在独立端口运行管理面板，
不受 AstrBot 仪表盘 iframe 沙箱限制。
"""
import asyncio
import os
import json as _json
from multiprocessing import Process

from hypercorn.config import Config as HConfig
from hypercorn.asyncio import serve
from quart import Quart, jsonify, request, send_file

from astrbot.api import logger

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.normpath(os.path.join(
    PLUGIN_ROOT, "..", "..", "data", "config",
    "astrbot_plugin_voice_assistant.json",
))

app = Quart(__name__)


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict):
    try:
        config_dir = os.path.dirname(CONFIG_PATH)
        os.makedirs(config_dir, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            _json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[WebUI] 配置持久化失败: {e}")


@app.route("/")
async def index():
    panel_path = os.path.join(PLUGIN_ROOT, "pages", "webui", "standalone.html")
    return await send_file(panel_path, mimetype="text/html; charset=utf-8")


@app.route("/api/get_config")
async def api_get_config():
    cfg = _load_config()
    sensitive = {"cloud_s3_secret_key", "cloud_webdav_passwd", "cloud_smb_passwd", "cloud_custom_headers"}
    safe = {k: ("***" if k in sensitive else v) for k, v in cfg.items()}
    return jsonify({"success": True, "config": safe})


@app.route("/api/save_config", methods=["POST"])
async def api_save_config():
    try:
        data = await request.get_json()
        updates = data.get("config", {})
        if not isinstance(updates, dict):
            return jsonify({"success": False, "error": "格式错误"})
        cfg = _load_config()
        cfg.update(updates)
        _save_config(cfg)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


async def _run(port: int):
    hconfig = HConfig()
    hconfig.bind = [f"0.0.0.0:{port}"]
    await serve(app, hconfig)


def run_server(port: int):
    asyncio.run(_run(port))
