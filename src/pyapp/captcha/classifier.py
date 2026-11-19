"""字符分类器：基于标注样本模板的 kNN。

模板库是包内的 ``data/arithmetic.npz``（由 ``scripts/build_templates.py``
从人工标注的样本生成），随模块一起分发，不依赖项目其它目录。
分类前对模板做旋转/平移增强，以覆盖验证码里随机的手写倾斜。
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from .segment import CHAR_SIZE

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_TEMPLATE_PATH = PACKAGE_ROOT / "data" / "arithmetic.npz"

# 数字位与运算符位的候选集合
DIGIT_LABELS = "0123456789"
OPERATOR_LABELS = "+-*/"
OP_SLOT = "op"
DIGIT_SLOT = "digit"
EQ_SLOT = "eq"
QUESTION_SLOT = "question"
# 固定槽位 -> 唯一候选字符
FIXED_SLOTS = {EQ_SLOT: "=", QUESTION_SLOT: "?"}

# 模板增强：旋转角度 × 平移偏移
AUGMENT_ANGLES = (-10, -5, 0, 5, 10)
AUGMENT_SHIFTS = ((0, 0), (-2, 0), (2, 0))

# kNN 投票的邻居个数
NEIGHBOR_COUNT = 3


def char_features(image: np.ndarray) -> np.ndarray:
    """字符图的组合特征：像素 + 行列投影 + 8x8 网格密度。"""
    size = CHAR_SIZE
    blur = cv2.GaussianBlur(image, (3, 3), 0).astype(np.float32) / 255.0
    pixels = blur.flatten()
    row_profile = blur.sum(axis=1) / size
    col_profile = blur.sum(axis=0) / size
    cells = cv2.resize(blur, (8, 8), interpolation=cv2.INTER_AREA).flatten()
    return np.concatenate([pixels, row_profile * 2.0, col_profile * 2.0, cells * 1.5])


def _rotate(image: np.ndarray, angle: float) -> np.ndarray:
    matrix = cv2.getRotationMatrix2D((CHAR_SIZE / 2, CHAR_SIZE / 2), angle, 1.0)
    return cv2.warpAffine(
        image, matrix, (CHAR_SIZE, CHAR_SIZE),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


def _shift(image: np.ndarray, dx: int, dy: int) -> np.ndarray:
    matrix = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(image, matrix, (CHAR_SIZE, CHAR_SIZE))


def augment_variants(image: np.ndarray) -> List[np.ndarray]:
    """生成用于匹配的旋转/平移变体。"""
    variants = []
    for angle in AUGMENT_ANGLES:
        rotated = _rotate(image, angle)
        for dx, dy in AUGMENT_SHIFTS:
            variants.append(_shift(rotated, dx, dy) if (dx or dy) else rotated)
    return variants


class CharClassifier:
    """加载模板库并提供单字符识别。"""

    def __init__(self, template_path: Optional[Path] = None) -> None:
        path = Path(template_path) if template_path else DEFAULT_TEMPLATE_PATH
        if not path.is_file():
            raise FileNotFoundError(
                f"字符模板库不存在：{path}；请先运行 python scripts/build_templates.py"
            )
        data = np.load(str(path), allow_pickle=False)
        chars = data["chars"]
        labels = [str(item) for item in data["labels"]]
        slots = [str(item) for item in data["slots"]]

        features = []
        feature_labels: List[str] = []
        feature_slots: List[str] = []
        for image, label, slot in zip(chars, labels, slots):
            for variant in augment_variants(image):
                features.append(char_features(variant))
                feature_labels.append(label)
                feature_slots.append(slot)
        self._features = np.vstack(features).astype(np.float32)
        self._labels = np.array(feature_labels)
        self._slots = np.array(feature_slots)
        self.template_path = path
        self.template_count = len(chars)

    def _candidate_mask(self, slot: Optional[str], charset: Optional[Sequence[str]]) -> np.ndarray:
        mask = np.ones(len(self._labels), dtype=bool)
        if slot in FIXED_SLOTS:
            mask &= self._labels == FIXED_SLOTS[slot]
        elif slot == OP_SLOT:
            mask &= np.isin(self._labels, list(OPERATOR_LABELS))
        elif slot == DIGIT_SLOT:
            mask &= np.isin(self._labels, list(DIGIT_LABELS))
        if charset and slot not in FIXED_SLOTS:
            mask &= np.isin(self._labels, list(charset))
        if not mask.any():
            if slot in FIXED_SLOTS:
                mask = self._labels == FIXED_SLOTS[slot]
            elif slot == OP_SLOT:
                mask = np.isin(self._labels, list(OPERATOR_LABELS))
            elif slot == DIGIT_SLOT:
                mask = np.isin(self._labels, list(DIGIT_LABELS))
            else:
                mask = np.ones(len(self._labels), dtype=bool)
        return mask

    def classify(
        self,
        image: np.ndarray,
        slot: Optional[str] = None,
        charset: Optional[Sequence[str]] = None,
    ) -> Tuple[str, float, Dict[str, float]]:
        """返回 (最佳字符, 置信度, 各类最小距离)。"""
        query = char_features(image)
        candidates = self._candidate_mask(slot, charset)
        features = self._features[candidates]
        labels = self._labels[candidates]
        distances = np.linalg.norm(features - query, axis=1)

        order = np.argsort(distances)[:NEIGHBOR_COUNT]
        neighbor_labels = labels[order]
        votes = Counter(neighbor_labels.tolist())
        top_label, top_votes = votes.most_common(1)[0]
        vote_ratio = top_votes / float(NEIGHBOR_COUNT)

        best_by_label: Dict[str, float] = {}
        for label, distance in zip(labels.tolist(), distances.tolist()):
            if label not in best_by_label or distance < best_by_label[label]:
                best_by_label[label] = float(distance)
        best_distance = best_by_label[top_label]
        others = [value for key, value in best_by_label.items() if key != top_label]
        if others:
            second_distance = min(others)
            margin = max(0.0, second_distance - best_distance) / (second_distance + 1e-6)
        else:
            margin = 1.0
        confidence = 0.5 * vote_ratio + 0.5 * margin
        return top_label, float(min(1.0, confidence)), best_by_label
