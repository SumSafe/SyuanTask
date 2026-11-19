"""验证码识别模块。

对外能力：
    recognize(image)  识别一张验证码并返回计算结果
    solve(text)       解析式子文本并求未知数

样本采集能力（harvest）属于研发工具，单独位于 ``pyapp.captcha.harvest``。
"""
from __future__ import annotations

from .classifier import CharClassifier
from .engine import recognize
from .preprocess import DEFAULT_STRATEGIES, STRATEGIES
from .solver import ArithmeticExpression, parse_expression, solve, solve_expression
from .types import CaptchaResult

__all__ = [
    "ArithmeticExpression",
    "CaptchaResult",
    "CharClassifier",
    "DEFAULT_STRATEGIES",
    "STRATEGIES",
    "parse_expression",
    "recognize",
    "solve",
    "solve_expression",
]
