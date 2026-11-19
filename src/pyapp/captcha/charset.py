"""候选字符集预设。"""
from __future__ import annotations

from typing import Optional

# 本项目真实字符集：数字 + 四则运算符 + 等号 + 未知数占位
PRESETS = {
    "arithmetic": "0123456789+-*/=?",
    "digits": "0123456789",
    "lower-alnum": "abcdefghijklmnopqrstuvwxyz0123456789",
    "upper-alnum": "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "alnum": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
}


def resolve_charset(name: Optional[str]) -> Optional[str]:
    """把预设名或字面字符集解析成字符集合。

    ``None`` / 空串 / ``none`` 表示不约束，返回 ``None``。
    """
    if not name:
        return None
    lowered = name.strip().lower()
    if lowered in ("none", "raw", "any"):
        return None
    if lowered in PRESETS:
        return PRESETS[lowered]
    return name
