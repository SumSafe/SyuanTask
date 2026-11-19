"""算术验证码的解析与求解。

验证码形态是 ``a op b = ?``，要求算出问号处的值；
为兼容题库变化，也支持问号出现在任意位置（解方程）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

OPERATOR_MAP = {
    "×": "*",
    "✕": "*",
    "÷": "/",
    "−": "-",
    "–": "-",
    "—": "-",
    "＋": "+",
    "＝": "=",
    "？": "?",
}

# 允许字符之间夹杂少量噪声字符，按整体匹配
EXPR_PATTERN = re.compile(
    r"(-?\d+|[?xX])\s*([+\-*/×÷−–—])\s*(-?\d+|[?xX])\s*=\s*(-?\d+|[?xX])"
)


@dataclass(frozen=True)
class ArithmeticExpression:
    """解析后的算术式子，未知数用 ``?`` 表示。"""

    left: str
    operator: str
    right: str
    result: str

    @property
    def text(self) -> str:
        return f"{self.left}{self.operator}{self.right}={self.result}"


def normalize_text(text: str) -> str:
    """把识别文本里的花体符号统一成 ASCII，并去掉空白。"""
    if not text:
        return ""
    normalized = []
    for char in text:
        if char.isspace():
            continue
        normalized.append(OPERATOR_MAP.get(char, char))
    return "".join(normalized)


def parse_expression(text: str) -> Optional[ArithmeticExpression]:
    """从识别文本中提取算术式子，失败返回 ``None``。"""
    normalized = normalize_text(text)
    match = EXPR_PATTERN.search(normalized)
    if not match:
        return None
    left, operator, right, result = match.groups()
    # 未知数位上可能出现 x/X，统一成 ?
    left = "?" if left in "xX" else left
    right = "?" if right in "xX" else right
    result = "?" if result in "xX" else result
    return ArithmeticExpression(left=left, operator=operator, right=right, result=result)


def _calculate(left: int, operator: str, right: int) -> Optional[Fraction]:
    if operator == "+":
        return Fraction(left + right)
    if operator == "-":
        return Fraction(left - right)
    if operator == "*":
        return Fraction(left * right)
    if operator == "/":
        if right == 0:
            return None
        return Fraction(left, right)
    return None


def _format(value: Fraction) -> str:
    """整除时输出整数，否则输出小数（最多 4 位，去尾零）。"""
    if value.denominator == 1:
        return str(value.numerator)
    decimal = value.numerator / value.denominator
    formatted = f"{decimal:.4f}".rstrip("0").rstrip(".")
    return formatted


def solve_expression(expression: ArithmeticExpression) -> Optional[str]:
    """求解式子中的未知数。"""
    left, operator, right, result = (
        expression.left,
        expression.operator,
        expression.right,
        expression.result,
    )
    unknown_positions = [
        name
        for name, value in (("left", left), ("right", right), ("result", result))
        if value == "?"
    ]
    if len(unknown_positions) != 1:
        return None

    try:
        if unknown_positions[0] == "result":
            value = _calculate(int(left), operator, int(right))
            return None if value is None else _format(value)
        if unknown_positions[0] == "left":
            # ? op right = result
            if operator == "+":
                return str(int(result) - int(right))
            if operator == "-":
                return str(int(result) + int(right))
            if operator == "*":
                return None if int(right) == 0 else _format(Fraction(int(result), int(right)))
            if operator == "/":
                return str(int(result) * int(right))
        if unknown_positions[0] == "right":
            # left op ? = result
            if operator == "+":
                return str(int(result) - int(left))
            if operator == "-":
                return str(int(left) - int(result))
            if operator == "*":
                return None if int(left) == 0 else _format(Fraction(int(result), int(left)))
            if operator == "/":
                return None if int(result) == 0 else _format(Fraction(int(left), int(result)))
    except (ValueError, ZeroDivisionError):
        return None
    return None


def solve(text: str) -> Optional[str]:
    """输入识别文本，返回未知数的值；无法求解时返回 ``None``。"""
    expression = parse_expression(text)
    if expression is None:
        return None
    return solve_expression(expression)
