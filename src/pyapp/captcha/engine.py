"""识别编排：预处理 -> 切分 -> 分类 -> 求解。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import cv2
import numpy as np

from .charset import resolve_charset
from .classifier import (
    CharClassifier,
    DIGIT_SLOT,
    EQ_SLOT,
    OP_SLOT,
    QUESTION_SLOT,
)
from .preprocess import DEFAULT_STRATEGIES, STRATEGIES, prepare
from .segment import clean_part, normalize_char, refine_window, split_segments, to_strip
from .solver import solve
from .types import CaptchaResult

ImageInput = Union[str, Path, bytes, bytearray]

# 切分假设：各字符的相对宽度（数字宽、运算符窄时更贴合实际布局）
SPLIT_PROFILES: Tuple[Tuple[float, ...], ...] = (
    (1.0, 1.0, 1.0, 1.0, 1.0),
    (1.15, 0.85, 1.0, 0.9, 1.0),
    (1.25, 0.8, 1.1, 0.85, 0.95),
    (1.0, 0.8, 1.05, 0.9, 1.0),
    (1.1, 0.75, 1.15, 0.9, 1.0),
    (0.95, 0.9, 1.0, 1.0, 1.0),
)
SLOTS = (DIGIT_SLOT, OP_SLOT, DIGIT_SLOT, EQ_SLOT, QUESTION_SLOT)
# 前三个字符决定答案，等号/问号只用于校验切分假设
MAIN_WEIGHT = 0.75
CHECK_WEIGHT = 0.25
# 预处理策略的投票权重：蓝色墨迹提取比通用 OTSU 更可靠
STRATEGY_WEIGHTS = {"blue": 1.0, "gray": 0.5}

_CLASSIFIER: Optional[CharClassifier] = None
_DDDDOCR_ENGINES: Dict[Tuple[Any, ...], Any] = {}


def _get_classifier() -> CharClassifier:
    global _CLASSIFIER
    if _CLASSIFIER is None:
        _CLASSIFIER = CharClassifier()
    return _CLASSIFIER


def _read_bytes(image: ImageInput) -> bytes:
    if isinstance(image, (bytes, bytearray)):
        return bytes(image)
    return Path(image).read_bytes()


def _get_ddddocr(charset: Optional[str], engine_kwargs: Optional[Dict[str, Any]]):
    import ddddocr

    kwargs = dict(engine_kwargs or {})
    kwargs.setdefault("show_ad", False)
    key = tuple(sorted((name, repr(value)) for name, value in kwargs.items())) + (charset or "",)
    if key not in _DDDDOCR_ENGINES:
        ocr = ddddocr.DdddOcr(**kwargs)
        if charset:
            ocr.set_ranges(charset)
        _DDDDOCR_ENGINES[key] = ocr
    return _DDDDOCR_ENGINES[key]


def _save_debug(debug_dir: str, tag: str, strategy: str, mask: np.ndarray,
                strip: Optional[np.ndarray], chars: Sequence[np.ndarray]) -> None:
    directory = Path(debug_dir)
    directory.mkdir(parents=True, exist_ok=True)
    prefix = f"{tag}_{strategy}" if tag else strategy
    cv2.imwrite(str(directory / f"{prefix}_mask.png"), mask)
    if strip is not None:
        cv2.imwrite(str(directory / f"{prefix}_strip.png"), strip)
    for index, char in enumerate(chars):
        cv2.imwrite(str(directory / f"{prefix}_char{index}.png"), char)


def _evaluate_profile(
    classifier: CharClassifier,
    strip: np.ndarray,
    profile: Sequence[float],
    charset: Optional[Sequence[str]],
) -> Tuple[float, List[str], List[float], List[np.ndarray]]:
    """按一种宽度假设切分并分类 5 个字符，返回 (综合得分, 字符, 置信度, 字符图)。"""
    windows = [refine_window(strip, start, end) for start, end in split_segments(strip, widths=profile)]
    chars = [normalize_char(clean_part(strip[:, start:end + 1])) for start, end in windows]
    labels: List[str] = []
    confidences: List[float] = []
    for char, slot in zip(chars, SLOTS):
        label, confidence, _ = classifier.classify(char, slot=slot, charset=charset)
        labels.append(label)
        confidences.append(confidence)
    main_score = min(confidences[:3])
    check_score = min(confidences[3:]) if len(confidences) >= 5 else 0.0
    score = MAIN_WEIGHT * main_score + CHECK_WEIGHT * check_score
    return score, labels, confidences, chars


def _recognize_knn(
    data: bytes,
    charset: Optional[Sequence[str]],
    strategies: Sequence[str],
    min_confidence: float,
    debug_dir: Optional[str],
    debug_tag: str,
) -> CaptchaResult:
    classifier = _get_classifier()
    attempts: List[str] = []
    # 每个候选式子的加权票数与平均置信度
    votes: Dict[str, float] = {}
    confidence_sums: Dict[str, float] = {}
    confidence_counts: Dict[str, int] = {}
    best_strategy: Dict[str, str] = {}

    for strategy in strategies:
        try:
            _, mask = prepare(data, strategy)
        except Exception as exc:  # 预处理失败直接换下一个策略
            attempts.append(f"{strategy}: 预处理失败（{exc}）")
            continue

        strip_info = to_strip(mask)
        if strip_info is None:
            attempts.append(f"{strategy}: 没有前景")
            continue
        strip, _, _ = strip_info
        weight = STRATEGY_WEIGHTS.get(strategy, 1.0)

        for index, profile in enumerate(SPLIT_PROFILES):
            score, labels, confidences, chars = _evaluate_profile(
                classifier, strip, profile, charset
            )
            if debug_dir:
                _save_debug(debug_dir, f"{debug_tag}_p{index}", strategy, mask, strip, chars)
            if len(labels) < 3:
                continue
            text = f"{labels[0]}{labels[1]}{labels[2]}=?"
            main_confidence = min(confidences[:3])
            votes[text] = votes.get(text, 0.0) + weight * max(score, 1e-3)
            confidence_sums[text] = confidence_sums.get(text, 0.0) + main_confidence
            confidence_counts[text] = confidence_counts.get(text, 0) + 1
            if text not in best_strategy or weight >= STRATEGY_WEIGHTS.get(best_strategy[text], 0.0):
                best_strategy[text] = strategy
            attempts.append(f"{strategy}/p{index}: {text}（{score:.2f}）")

    if not votes:
        return CaptchaResult(
            engine="knn",
            strategy=",".join(strategies),
            error="所有预处理策略均失败",
            detail={"attempts": attempts},
        )

    text = max(
        votes,
        key=lambda item: (votes[item], confidence_sums[item] / confidence_counts[item]),
    )
    confidence = confidence_sums[text] / confidence_counts[text]
    answer = solve(text)
    error = "" if answer is not None else "式子无法求解"
    if answer is not None and min_confidence > 0 and confidence < min_confidence:
        error = f"置信度 {confidence:.2f} 低于下限 {min_confidence:.2f}"
    return CaptchaResult(
        text=text,
        expression=text,
        answer=answer or "",
        confidence=confidence,
        engine="knn",
        strategy=best_strategy.get(text, ""),
        error=error,
        detail={
            "votes": {key: round(value, 3) for key, value in sorted(votes.items(), key=lambda kv: -kv[1])},
            "attempts": attempts,
        },
    )


def _recognize_ddddocr(
    data: bytes,
    charset: Optional[str],
    engine_kwargs: Optional[Dict[str, Any]],
) -> CaptchaResult:
    ocr = _get_ddddocr(charset, engine_kwargs)
    text = ocr.classification(data)
    answer = solve(text)
    return CaptchaResult(
        text=text,
        expression=text,
        answer=answer or "",
        confidence=1.0 if answer is not None else 0.0,
        engine="ddddocr",
        strategy="raw",
        error="" if answer is not None else "式子无法求解",
    )


def recognize(
    image: ImageInput,
    engine: str = "knn",
    charset: Optional[str] = "arithmetic",
    strategies: Optional[Sequence[str]] = None,
    min_confidence: float = 0.0,
    engine_kwargs: Optional[Dict[str, Any]] = None,
    debug_dir: Optional[str] = None,
    debug_tag: str = "",
) -> CaptchaResult:
    """识别一张验证码图片并返回计算结果。

    engine 支持 ``knn``（默认，模板库分类）与 ``ddddocr``（通用 OCR）。
    """
    data = _read_bytes(image)
    resolved_charset = resolve_charset(charset)
    if engine == "ddddocr":
        return _recognize_ddddocr(data, resolved_charset, engine_kwargs)
    if engine != "knn":
        raise ValueError(f"未知识别引擎：{engine}")
    selected = tuple(strategies) if strategies else DEFAULT_STRATEGIES
    unknown = [name for name in selected if name not in STRATEGIES]
    if unknown:
        raise ValueError(f"未知预处理策略：{', '.join(unknown)}")
    if not debug_tag and not isinstance(image, (bytes, bytearray)):
        debug_tag = Path(str(image)).stem
    return _recognize_knn(
        data,
        resolved_charset,
        selected,
        min_confidence,
        debug_dir,
        debug_tag,
    )
