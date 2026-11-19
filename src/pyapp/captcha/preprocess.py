"""验证码图像预处理策略。

每个策略把原始图片转换成"白底黑字"的二值掩码，供切分与分类使用。
策略之间互为备份：单一策略在个别图片上失效时，其它策略仍可能成功。
"""
from __future__ import annotations

from typing import Callable, Dict, Tuple

import cv2
import numpy as np

# 蓝色墨迹判定阈值：B 通道相对 R/G 的差值下限
BLUE_SCORE_THRESHOLD = 40
# 小于该面积的连通域视为噪点
MIN_COMPONENT_AREA = 20
# 图片四周的装饰边框宽度，直接抹掉
BORDER_MARGIN = 2


def decode_image(data: bytes) -> np.ndarray:
    """把图片字节解码为 BGR 图像。"""
    array = np.frombuffer(data, np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("图片无法解码")
    return image


def _drop_small_components(mask: np.ndarray) -> np.ndarray:
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    clean = np.zeros_like(mask)
    for index in range(1, count):
        if stats[index, cv2.CC_STAT_AREA] >= MIN_COMPONENT_AREA:
            clean[labels == index] = 255
    return clean


def _drop_border(mask: np.ndarray) -> np.ndarray:
    mask[:BORDER_MARGIN, :] = 0
    mask[-BORDER_MARGIN:, :] = 0
    mask[:, :BORDER_MARGIN] = 0
    mask[:, -BORDER_MARGIN:] = 0
    return mask


def blue_mask(image: np.ndarray) -> np.ndarray:
    """提取蓝色墨迹：B 通道显著高于 R/G 的像素。"""
    blue, green, red = cv2.split(image.astype(np.int16))
    score = blue - np.maximum(red, green)
    mask = (score > BLUE_SCORE_THRESHOLD).astype(np.uint8) * 255
    _drop_border(mask)
    return _drop_small_components(mask)


def gray_otsu(image: np.ndarray) -> np.ndarray:
    """灰度 OTSU 二值化（蓝字在浅底上会变成前景）。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    _drop_border(mask)
    return _drop_small_components(mask)


STRATEGIES: Dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "blue": blue_mask,
    "gray": gray_otsu,
}

DEFAULT_STRATEGIES: Tuple[str, ...] = ("blue", "gray")


def prepare(data: bytes, strategy: str) -> Tuple[np.ndarray, np.ndarray]:
    """按策略返回 (原始 BGR 图, 二值掩码)。"""
    if strategy not in STRATEGIES:
        raise ValueError(f"未知预处理策略：{strategy}")
    image = decode_image(data)
    return image, STRATEGIES[strategy](image)
