"""本地 Web 控制台：填写并保存账号、一键触发自动登录。

启动： python app.py  （自动打开 http://127.0.0.1:5178）
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

import store
from automation import LoginRunner

BASE_DIR = store.app_dir()
RES_DIR = store.res_dir()
ASSETS_DIR = RES_DIR / "assets"
app = Flask(__name__, template_folder=str(RES_DIR / "templates"))
app.config["JSON_AS_ASCII"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

runner = LoginRunner()
DEFAULT_URL = store.DEFAULT_CONFIG["settings"]["url"]


@app.get("/")
def index():
    cfg = store.load_config()
    return render_template(
        "index.html",
        config=cfg,
        units=store.load_units(),
        url=cfg["settings"].get("url") or DEFAULT_URL,
    )


@app.get("/favicon.ico")
@app.get("/logo.png")
def logo_asset():
    """页面图标：源码运行读 assets/，打包后从解包目录读。"""
    name, mime = ("logo.ico", "image/x-icon") if request.path.endswith(".ico") else ("logo.png", "image/png")
    path = ASSETS_DIR / name
    if not path.exists():
        return "", 404
    return send_file(str(path), mimetype=mime)


@app.get("/api/state")
def api_state():
    cfg = store.load_config()
    state = runner.snapshot()
    return jsonify(
        {
            "ok": True,
            "state": state,
            "config": {
                "unit": cfg["unit"],
                "account": cfg["account"],
                "has_password": bool(cfg["password"]),
                "remember": cfg["remember"],
            },
        }
    )


NUMERIC_KEYS = ("max_attempts", "min_confidence", "slow_mo", "approve_delay", "approve_limit",
                "viewport_w", "viewport_h")
TEXT_KEYS = ("intern_text", "approve_score", "approve_comment", "browser")


def merge_settings(data, base):
    """把前端提交的设置合并到已有设置上。"""
    settings = dict(store.DEFAULT_CONFIG["settings"])
    settings.update(base or {})
    for key in NUMERIC_KEYS:
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            settings[key] = float(value) if key == "min_confidence" else int(value)
        except (TypeError, ValueError):
            continue
    for key in TEXT_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            settings[key] = value.strip()
    if data.get("approve_enabled") is not None:
        settings["approve_enabled"] = bool(data["approve_enabled"])
    # 指定分数允许清空
    if isinstance(data.get("score_overrides"), str):
        settings["score_overrides"] = data["score_overrides"].strip()
    return settings


@app.post("/api/config")
def api_save_config():
    data = request.get_json(force=True, silent=True) or {}
    unit = (data.get("unit") or "").strip()
    account = (data.get("account") or "").strip()
    password = data.get("password")
    remember = bool(data.get("remember", True))

    if not unit:
        return jsonify({"ok": False, "error": "请先选择单位"}), 400
    if not account:
        return jsonify({"ok": False, "error": "请填写账号"}), 400

    current = store.load_config()
    if data.get("clear_password"):
        store.save_config(unit, account, "", False, current.get("settings"))
        return jsonify({"ok": True, "message": "已清除本机保存的密码"})

    if password is None or password == "":
        # 没输入新密码时沿用已保存的密码
        password = current["password"]
    if not password:
        return jsonify({"ok": False, "error": "请填写密码"}), 400

    settings = merge_settings(data, current.get("settings"))
    store.save_config(unit, account, password, remember, settings)
    return jsonify({"ok": True, "message": "配置已保存（密码使用 Windows DPAPI 加密）"})


@app.post("/api/login")
def api_login():
    data = request.get_json(force=True, silent=True) or {}
    cfg = store.load_config()
    unit = (data.get("unit") or cfg["unit"] or "").strip()
    account = (data.get("account") or cfg["account"] or "").strip()
    password = data.get("password") or cfg["password"] or ""
    remember = bool(data.get("remember", cfg["remember"]))

    if not unit or not account or not password:
        return jsonify({"ok": False, "error": "单位 / 账号 / 密码 均不能为空"}), 400

    settings = merge_settings(data, cfg.get("settings"))

    if data.get("save", True):
        store.save_config(unit, account, password, remember, settings)

    payload = {
        "url": settings.get("url") or DEFAULT_URL,
        "unit": unit,
        "account": account,
        "password": password,
        "settings": settings,
    }
    result = runner.start(payload)
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    runner.stop()
    return jsonify({"ok": True})


@app.post("/api/close-browser")
def api_close_browser():
    runner.close_browser()
    return jsonify({"ok": True})


@app.post("/api/units/refresh")
def api_refresh_units():
    cfg = store.load_config()
    url = cfg["settings"].get("url") or DEFAULT_URL
    try:
        units = LoginRunner.fetch_units(url)
        if units:
            store.save_units(units)
            return jsonify({"ok": True, "units": units, "count": len(units)})
        return jsonify({"ok": False, "error": "未获取到单位列表"}), 502
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


def _open_browser(port: int) -> None:
    threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()


def _port_busy(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    port = int(os.environ.get("PORT", "5178"))
    url = f"http://127.0.0.1:{port}/"
    if _port_busy(port):
        print(f"* 已有实例在运行，直接打开：{url}")
        webbrowser.open(url)
        return 0

    print("=" * 56)
    print(" SyuanTask · 校企E站自动登录控制台")
    print(f" 地址：{url}")
    print(" 关掉这个窗口 = 退出程序")
    print("=" * 56)
    if "--no-browser" not in sys.argv:
        _open_browser(port)
    try:
        app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
