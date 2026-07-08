"""查找算法 — 跳跃查找实现 (O(√n)).

要求输入列表必须是有序的（升序）。跳跃查找将列表分块，
先跳跃定位目标可能存在的块，再在块内线性查找。
"""

import math


def jump_search(arr: list, target) -> int:
    """在有序列表中进行跳跃查找。

    每次跳 sqrt(n) 步定位目标可能存在的块，再线性查找。

    Args:
        arr: 已排序的升序列表。
        target: 要查找的目标值。

    Returns:
        目标值的索引，未找到返回 -1。

    Raises:
        TypeError: 如果 arr 不是列表。
        ValueError: 如果列表未排序。
    """
    if not isinstance(arr, list):
        raise TypeError("arr 必须是列表")
    if not _is_sorted(arr):
        raise ValueError("arr 必须是有序的升序列表")
    if not arr:
        return -1

    n = len(arr)
    step = int(math.sqrt(n))

    # 跳跃定位块
    prev = 0
    while prev < n and arr[min(step, n) - 1] < target:
        prev = step
        step += int(math.sqrt(n))
        if prev >= n:
            return -1

    # 块内线性查找
    for i in range(prev, min(step, n)):
        if arr[i] == target:
            return i
    return -1


def jump_search_all(arr: list, target) -> list[int]:
    """跳跃查找目标值的所有出现（块内全部扫描）。

    Args:
        arr: 已排序的升序列表。
        target: 要查找的目标值。

    Returns:
        所有匹配索引的列表（可能为空）。
    """
    if not isinstance(arr, list):
        raise TypeError("arr 必须是列表")
    if not arr:
        return []

    # 先找任意一个匹配位置
    idx = jump_search(arr, target)
    if idx == -1:
        return []

    # 向左扩散
    left = idx
    while left > 0 and arr[left - 1] == target:
        left -= 1

    # 向右扩散
    right = idx
    while right < len(arr) - 1 and arr[right + 1] == target:
        right += 1

    return list(range(left, right + 1))


def _is_sorted(arr: list) -> bool:
    """检查列表是否升序排列。"""
    for i in range(1, len(arr)):
        if arr[i - 1] > arr[i]:
            return False
    return True