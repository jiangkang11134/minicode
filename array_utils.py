"""数组旋转工具函数。

提供数组的向右旋转和向左旋转操作。

Functions:
    rotate_array(arr, k): 将数组向右旋转 k 步
    rotate_array_left(arr, k): 将数组向左旋转 k 步
"""

from __future__ import annotations


def rotate_array(arr: list, k: int) -> list:
    """将数组向右旋转 k 步。

    右旋 k 步 = 将末尾 k 个元素移到数组开头。

    Args:
        arr: 输入数组（不会被修改，返回新列表）
        k: 旋转步数

    Returns:
        右旋后的新列表
    """
    if not arr:
        return []
    n = len(arr)
    k = k % n
    if k == 0:
        return list(arr)
    return arr[-k:] + arr[:-k]


def rotate_array_left(arr: list, k: int) -> list:
    """将数组向左旋转 k 步。

    左旋 k 步 = 将开头 k 个元素移到数组末尾。

    Args:
        arr: 输入数组（不会被修改，返回新列表）
        k: 旋转步数

    Returns:
        左旋后的新列表
    """
    if not arr:
        return []
    n = len(arr)
    k = k % n
    if k == 0:
        return list(arr)
    return arr[k:] + arr[:k]