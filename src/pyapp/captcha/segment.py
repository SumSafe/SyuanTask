"""验证码字符切分与归一化。

验证码布局固定为「数字 运算符 数字 等号 问号」五个等宽字符，
因此切分不依赖连通域（字符之间常有笔画粘连），而是对前景的水平投影
搜索整体相位，让四条分界线落在投影低谷上，再对每个窗口做局部微调。
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

# 归一化后的字符画布边长
CHAR_SIZE = 32
# 期望切出的字符个数
TARGET_CHARS = 5
# 网格相位搜索范围（像素）
PHASE_RANGE = 3.5
PHASE_STEP = 0.5
# 单窗口局部微调幅度
REFINE_RADIUS = 3
# 清理窗口内相邻字符碎片：低于主连通域面积该比例的丢弃
CLEAN_AREA_RATIO = 0.25
# 连通域中心偏离窗口中心超过该比例（相对窗口宽度）视为邻居
CLEAN_CENTER_RATIO = 0.5


def to_strip(mask: np.ndarray) -> Optional[Tuple[np.ndarray, int, int]]:
    """裁剪到前景包围盒，返回 (掩码条带, 左上角 x, 左上角 y)。"""
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    return mask[y0:y1 + 1, x0:x1 + 1], x0, y0


def split_segments(
    strip: np.ndarray,
    target: int = TARGET_CHARS,
    widths: Optional[Sequence[float]] = None,
) -> List[Tuple[int, int]]:
    """把条带切成 ``target`` 个字符窗口（左闭右闭）。

    ``widths`` 是各字符的相对宽度比例（默认等宽），用于验证码字符宽窄
    不一致时给出多种切分假设。
    """
    height, width = strip.shape[:2]
    if width == 0 or height == 0:
        return []
    if widths is None or len(widths) != target:
        widths = [1.0] * target
    total_weight = float(sum(widths))
    edges = [0.0]
    accumulated = 0.0
    for weight in widths:
        accumulated += weight
        edges.append(width * accumulated / total_weight)

    projection = strip.sum(axis=0).astype(np.float32)
    best_delta = 0.0
    best_score = None
    for delta in np.arange(-PHASE_RANGE, PHASE_RANGE + PHASE_STEP / 2, PHASE_STEP):
        score = 0.0
        for index in range(1, target):
            x = int(round(edges[index] + delta))
            if x <= 1 or x >= width - 2:
                score += 1e6
                continue
            score += projection[x - 1] + projection[x] + projection[x + 1]
        if best_score is None or score < best_score:
            best_score, best_delta = score, float(delta)
    segments: List[Tuple[int, int]] = []
    for index in range(target):
        start = max(0, int(round(edges[index] + best_delta)))
        end = min(width - 1, int(round(edges[index + 1] + best_delta)))
        if end < start:
            end = start
        segments.append((start, end))
    return segments


def refine_window(strip: np.ndarray, start: int, end: int, radius: int = REFINE_RADIUS) -> Tuple[int, int]:
    """在 ±radius 内平移窗口，使窗口内笔画能量最大、切边代价最小。"""
    projection = strip.sum(axis=0).astype(np.float32)
    width = strip.shape[1]
    best = None
    for shift in range(-radius, radius + 1):
        left, right = start + shift, end + shift
        if left < 0 or right >= width or right - left < 2:
            continue
        energy = projection[left + 2:right - 1].sum() - 0.5 * (projection[left] + projection[right])
        if best is None or energy > best[0]:
            best = (energy, left, right)
    if best is None:
        return start, end
    return best[1], best[2]


def normalize_char(part: np.ndarray, size: int = CHAR_SIZE) -> np.ndarray:
    """把单个字符裁剪到包围盒后等比缩放，居中放到正方形画布上。"""
    ys, xs = np.where(part > 0)
    if len(xs) == 0:
        return np.zeros((size, size), np.uint8)
    crop = part[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    height, width = crop.shape[:2]
    margin = size // 4
    scale = (size - margin) / float(max(height, width))
    new_h = max(1, int(round(height * scale)))
    new_w = max(1, int(round(width * scale)))
    resized = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((size, size), np.uint8)
    offset_y = (size - new_h) // 2
    offset_x = (size - new_w) // 2
    canvas[offset_y:offset_y + new_h, offset_x:offset_x + new_w] = resized
    return canvas


def clean_part(part: np.ndarray) -> np.ndarray:
    """清除窗口两侧夹带的相邻字符碎片，只保留居中的主要笔画。"""
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(part, 8)
    if count <= 1:
        return part
    areas = stats[1:, cv2.CC_STAT_AREA]
    main_area = float(areas.max())
    width = part.shape[1]
    center = width / 2.0
    clean = np.zeros_like(part)
    for index in range(1, count):
        area = float(stats[index, cv2.CC_STAT_AREA])
        if area < main_area * CLEAN_AREA_RATIO:
            continue
        if abs(float(centroids[index][0]) - center) > width * CLEAN_CENTER_RATIO:
            continue
        clean[labels == index] = part[labels == index]
    return clean if clean.any() else part


def split_chars(strip: np.ndarray, target: int = TARGET_CHARS) -> List[np.ndarray]:
    """切分并归一化，返回字符图列表。"""
    windows = [refine_window(strip, start, end) for start, end in split_segments(strip, target)]
    return [normalize_char(clean_part(strip[:, start:end + 1])) for start, end in windows]
