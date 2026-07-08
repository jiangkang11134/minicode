"""查找算法 — 二分查找实现 (O(log n)).

要求输入列表必须是有序的（升序）。
"""


def binary_search(arr: list, target) -> int:
    """在有序列表中二分查找目标值（迭代版）。

    每次取中间元素比较，缩小一半查找范围。

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

    left, right = 0, len(arr) - 1
    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return -1


def binary_search_recursive(arr: list, target, left: int = 0, right: int | None = None) -> int:
    """在有序列表中二分查找目标值（递归版）。

    Args:
        arr: 已排序的升序列表。
        target: 要查找的目标值。
        left: 左边界索引（内部递归用）。
        right: 右边界索引（内部递归用）。

    Returns:
        目标值的索引，未找到返回 -1。

    Raises:
        TypeError: 如果 arr 不是列表。
        ValueError: 如果列表未排序。
    """
    if not isinstance(arr, list):
        raise TypeError("arr 必须是列表")
    if right is None:
        if not _is_sorted(arr):
            raise ValueError("arr 必须是有序的升序列表")
        right = len(arr) - 1

    if left > right:
        return -1
    mid = (left + right) // 2
    if arr[mid] == target:
        return mid
    elif arr[mid] < target:
        return binary_search_recursive(arr, target, mid + 1, right)
    else:
        return binary_search_recursive(arr, target, left, mid - 1)


def binary_search_first(arr: list, target) -> int:
    """查找目标值首次出现的索引（处理重复元素）。

    Args:
        arr: 已排序的升序列表。
        target: 要查找的目标值。

    Returns:
        目标值首次出现的索引，未找到返回 -1。
    """
    if not isinstance(arr, list):
        raise TypeError("arr 必须是列表")
    left, right = 0, len(arr) - 1
    result = -1
    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            result = mid
            right = mid - 1  # 继续向左搜索
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return result


def binary_search_last(arr: list, target) -> int:
    """查找目标值最后一次出现的索引（处理重复元素）。

    Args:
        arr: 已排序的升序列表。
        target: 要查找的目标值。

    Returns:
        目标值最后一次出现的索引，未找到返回 -1。
    """
    if not isinstance(arr, list):
        raise TypeError("arr 必须是列表")
    left, right = 0, len(arr) - 1
    result = -1
    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            result = mid
            left = mid + 1  # 继续向右搜索
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return result


def _is_sorted(arr: list) -> bool:
    """检查列表是否升序排列。"""
    for i in range(1, len(arr)):
        if arr[i - 1] > arr[i]:
            return False
    return True