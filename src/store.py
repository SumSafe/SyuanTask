"""本地配置读写：账号/密码/单位持久化，密码用 Windows DPAPI 加密。"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict


def app_dir() -> Path:
    """可写目录：源码运行时是脚本目录，打包成 exe 后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def res_dir() -> Path:
    """只读资源目录：打包后指向 PyInstaller 解包目录。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return Path(__file__).resolve().parent


BASE_DIR = app_dir()
RES_DIR = res_dir()
CONFIG_PATH = BASE_DIR / "config.json"
UNITS_PATH = BASE_DIR / "units.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "unit": "重庆安全技术职业学院",
    "account": "",
    "password": "",
    "remember": True,
    "settings": {
        "url": "https://syuantech.cn/login_1?redirect=%2FcontrolBoard%7D",
        "max_attempts": 6,
        "min_confidence": 0.35,
        "slow_mo": 120,
        "headless": False,
        "intern_text": "实习平台",
        # 默认关闭：批阅是真实写入且不可撤销，需要用户主动勾选
        "approve_enabled": False,
        "approve_score": "99",
        "approve_comment": "加油",
        "approve_delay": 800,
        "approve_limit": 200,
        "score_overrides": "",
        "browser": "auto",
        "viewport_w": 1440,
        "viewport_h": 900,
    },
}


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _make_blob(data: bytes):
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_in, _keep = _make_blob(data)
    blob_out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "syuantech-auto-login",
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_in, _keep = _make_blob(data)
    blob_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def encrypt_secret(text: str) -> str:
    if not text:
        return ""
    raw = text.encode("utf-8")
    try:
        return "dpapi:" + base64.b64encode(_dpapi_protect(raw)).decode("ascii")
    except Exception:
        # 兜底：仅做编码，避免明文落盘（安全性弱于 DPAPI）
        return "b64:" + base64.b64encode(raw).decode("ascii")


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    try:
        if token.startswith("dpapi:"):
            return _dpapi_unprotect(base64.b64decode(token[6:])).decode("utf-8")
        if token.startswith("b64:"):
            return base64.b64decode(token[4:]).decode("utf-8")
        return token
    except Exception:
        return ""


def load_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            stored = {}
        for key in ("unit", "account"):
            if stored.get(key):
                cfg[key] = stored[key]
        if "remember" in stored:
            cfg["remember"] = bool(stored["remember"])
        if stored.get("password"):
            cfg["password"] = decrypt_secret(stored["password"])
        if isinstance(stored.get("settings"), dict):
            cfg["settings"].update({k: v for k, v in stored["settings"].items() if v is not None})
    return cfg


def save_config(unit: str, account: str, password: str, remember: bool,
                settings: Dict[str, Any] | None = None) -> None:
    payload: Dict[str, Any] = {
        "unit": unit,
        "account": account,
        "remember": bool(remember),
        "password": encrypt_secret(password) if remember else "",
    }
    if settings is not None:
        payload["settings"] = settings
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def load_units() -> list[str]:
    for path in (UNITS_PATH, RES_DIR / "units.json"):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    return [str(x) for x in data]
            except Exception:
                continue
    return ["重庆安全技术职业学院"]


def save_units(units: list[str]) -> None:
    UNITS_PATH.write_text(
        json.dumps(units, ensure_ascii=False, indent=1), encoding="utf-8"
    )
