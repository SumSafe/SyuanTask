"""识别结果的数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class CaptchaResult:
    """单张验证码的识别结果。

    text       识别出的原始式子（如 ``9-6=?``）
    expression 规范化后的式子
    answer     计算结果（如 ``3``），失败时为空串
    confidence 置信度，0~1
    """

    text: str = ""
    expression: str = ""
    answer: str = ""
    confidence: float = 0.0
    engine: str = ""
    strategy: str = ""
    error: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """识别并计算成功。"""
        return bool(self.answer) and not self.error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "expression": self.expression,
            "answer": self.answer,
            "confidence": round(self.confidence, 4),
            "engine": self.engine,
            "strategy": self.strategy,
            "error": self.error,
            "detail": self.detail,
        }

    def __str__(self) -> str:
        if not self.ok:
            return f"识别失败：{self.error or self.text or '未知原因'}"
        return f"{self.text} -> {self.answer}（置信度 {self.confidence:.2f}）"
