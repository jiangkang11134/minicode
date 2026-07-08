"""查找算法 — 线性查找实现 (O(n))."""


def linear_search(arr: list, target) -> int:
    """在有序或无序列表中线性查找目标值。

    从第一个元素开始逐个比较，直到找到目标或遍历完整个列表。

    Args:
        arr: 待查找的列表。
        target: 要查找的目标值。

    Returns:
        目标值首次出现的索引，未找到返回 -1。

    Raises:
        TypeError: 如果 arr 不是列表。
    """
    if not isinstance(arr, list):
        raise TypeError("arr 必须是列表")
    for i, val in enumerate(arr):
        if val == target:
            return i
    return -1


def linear_search_all(arr: list, target) -> list[int]:
    """查找目标值在列表中的所有出现位置。

    Args:
        arr: 待查找的列表。
        target: 要查找的目标值。

    Returns:
        所有匹配索引的列表（可能为空列表）。

    Raises:
        TypeError: 如果 arr 不是列表。
    """
    if not isinstance(arr, list):
        raise TypeError("arr 必须是列表")
    return [i for i, val in enumerate(arr) if val == target]