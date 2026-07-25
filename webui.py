"""聆音 — 独立 WebUI 服务器。使用 Quart+Hypercorn 在独立端口运行管理面板。"""
import asyncio
import os
import json as _json

import aiohttp
from astrbot.api import logger
from hypercorn.config import Config as HConfig
from hypercorn.asyncio import serve
from quart import Quart, jsonify, request, send_file, Response

PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.normpath(os.path.join(
    PLUGIN_ROOT, "..", "..", "data", "config",
    "astrbot_plugin_voice_assistant.json",
))

app = Quart(__name__)

ASTRBOT_SERVER_URL = "http://localhost:6185"


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


# ── 本地路由（快速读写，不依赖 AstrBot） ──────────────────────

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


# ── API 反向代理（转发到 AstrBot 主服务） ──────────────────────

@app.route("/api/v1/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_api(path: str):
    target_url = f"{ASTRBOT_SERVER_URL}/api/v1/{path}"

    headers = {}
    for k, v in request.headers.items():
        if k.lower() not in ("host", "content-length", "transfer-encoding"):
            headers[k] = v

    try:
        async with aiohttp.ClientSession() as session:
            kwargs: dict = {"headers": headers}

            if request.method == "GET":
                kwargs["params"] = request.args
            elif request.method in ("POST", "PUT", "DELETE"):
                raw_body = await request.get_data()
                kwargs["data"] = raw_body
                if "Content-Type" not in headers and request.content_type:
                    headers["Content-Type"] = request.content_type

            async with session.request(request.method, target_url, **kwargs) as resp:
                body = await resp.read()
                resp_headers = {
                    k: v for k, v in resp.headers.items()
                    if k.lower() not in ("transfer-encoding", "content-length")
                }
                return Response(body, status=resp.status, headers=resp_headers)

    except aiohttp.ClientError as e:
        logger.warning(f"[WebUI] 代理请求失败: {e}")
        return jsonify({
            "success": False,
            "error": f"无法连接 AstrBot 服务 ({ASTRBOT_SERVER_URL})，请检查服务是否运行",
        }), 502
    except Exception as e:
        logger.error(f"[WebUI] 代理异常: {e}", exc_info=True)
        return jsonify({"success": False, "error": f"代理异常: {e}"}), 500


# ── 启动 ──────────────────────────────────────────────────────

async def _run(port: int):
    hconfig = HConfig()
    hconfig.bind = [f"0.0.0.0:{port}"]
    await serve(app, hconfig)


def run_server(port: int, astrbot_url: str = "http://localhost:6185"):
    global ASTRBOT_SERVER_URL
    ASTRBOT_SERVER_URL = astrbot_url
    asyncio.run(_run(port))
